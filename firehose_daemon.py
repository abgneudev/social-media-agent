"""Firehose Daemon: background Jetstream consumer for network intelligence.

Connects to Bluesky's Jetstream (JSON-over-WebSocket firehose) and tracks
two signals the main engine cannot get from polling:

1. **Velocity tracking**: posts across the network that receive rapid
   engagement (likes) within minutes of creation. The engine's analyzer
   can later dissect these high-velocity posts for linguistic patterns.

2. **Audience mapping**: likes and reposts targeting OUR posts, capturing
   exactly who is engaging so the engine can weight reward signals by
   audience quality and route high-value engagers into curated lists.

Architecture:
  - Runs in a daemon thread started by run.py. The thread is marked
    daemon=True so it dies with the main process; no orphan risk.
  - Writes to NETWORK_TELEMETRY_FILE via atomic_write_json on a periodic
    flush (every FLUSH_INTERVAL_SECONDS). The main engine reads this file
    at tick boundaries; there is no shared memory or locking.
  - If the WebSocket drops, the daemon reconnects with exponential backoff.
    A crash in this thread NEVER affects the main engine loop.
  - No authentication required: Jetstream is a public, read-only stream.

Jetstream endpoint docs:
  wss://jetstream2.us-east.bsky.network/subscribe
  Query params: wantedCollections (comma-sep collection NSIDs)
"""
import json
import time
import threading
import asyncio
import logging
logger = logging.getLogger("kiloforge.firehose")

# How often (seconds) the daemon flushes its in-memory state to disk.
FLUSH_INTERVAL_SECONDS = 120
# How many high-velocity posts to keep in the telemetry blob.
MAX_VELOCITY_POSTS = 50
# Minimum likes within the tracking window to qualify as "high velocity".
VELOCITY_THRESHOLD = 3
# Rolling window for velocity tracking (seconds).
VELOCITY_WINDOW_SECONDS = 600  # 10 minutes
# Max engagers to track per flush cycle.
MAX_ENGAGERS = 200
# Reconnect backoff constants.
INITIAL_BACKOFF = 2
MAX_BACKOFF = 120

# Jetstream public endpoint (no auth needed, free, JSON format).
JETSTREAM_URI = (
    "wss://jetstream2.us-east.bsky.network/subscribe"
    "?wantedCollections=app.bsky.feed.like"
    "&wantedCollections=app.bsky.feed.post"
)


class FirehoseDaemon:
    """In-memory state accumulator flushed periodically to disk."""

    def __init__(self, our_did, telemetry_file, atomic_write_fn, is_relevant_fn):
        self.our_did = our_did
        self.telemetry_file = telemetry_file
        self.atomic_write = atomic_write_fn
        self.is_relevant_fn = is_relevant_fn
        # post_uri -> {text, author_did, created_at, like_count}
        self._post_tracker = {}
        # list of {did, handle, ts} for users who engaged with our posts
        self._our_engagers = []
        # high-velocity posts that crossed the threshold
        self._velocity_hits = []
        self._last_flush = time.time()
        self._lock = threading.Lock()

    def handle_event(self, event):
        """Process a single Jetstream event (already parsed from JSON)."""
        try:
            kind = event.get("kind")
            if kind == "commit":
                self._handle_commit(event)
        except Exception as e:
            # Never let a malformed event crash the daemon.
            logger.debug(f"[FIREHOSE] event parse error: {e}")

    def _handle_commit(self, event):
        commit = event.get("commit", {})
        collection = commit.get("collection", "")
        operation = commit.get("operation", "")
        record = commit.get("record", {})
        repo_did = event.get("did", "")

        if collection == "app.bsky.feed.post" and operation == "create":
            self._track_post(repo_did, commit, record)
        elif collection == "app.bsky.feed.like" and operation == "create":
            self._track_like(repo_did, record)

    def _track_post(self, author_did, commit, record):
        """Register a new post for velocity tracking."""
        text = record.get("text", "")
        if not text or len(text) < 20:
            return
            
        if not self.is_relevant_fn(text):
            return
            
        # Build the AT URI from repo DID + rkey
        rkey = commit.get("rkey", "")
        if not rkey:
            return
        uri = f"at://{author_did}/app.bsky.feed.post/{rkey}"
        with self._lock:
            self._post_tracker[uri] = {
                "text": text[:300],
                "author_did": author_did,
                "ts": time.time(),
                "like_count": 0,
            }
            # Evict old posts to bound memory
            self._evict_old_posts()

    def _track_like(self, liker_did, record):
        """Increment like count on a tracked post and detect audience engagement."""
        subject = record.get("subject", {})
        target_uri = subject.get("uri", "")
        if not target_uri:
            return

        with self._lock:
            # Track velocity on network posts
            if target_uri in self._post_tracker:
                entry = self._post_tracker[target_uri]
                entry["like_count"] += 1
                age = time.time() - entry["ts"]
                if (entry["like_count"] >= VELOCITY_THRESHOLD
                        and age <= VELOCITY_WINDOW_SECONDS
                        and not any(v["uri"] == target_uri for v in self._velocity_hits)):
                    self._velocity_hits.append({
                        "uri": target_uri,
                        "text": entry["text"],
                        "author_did": entry["author_did"],
                        "likes_in_window": entry["like_count"],
                        "window_seconds": int(age),
                        "ts": time.time(),
                    })
                    if len(self._velocity_hits) > MAX_VELOCITY_POSTS:
                        self._velocity_hits = self._velocity_hits[-MAX_VELOCITY_POSTS:]

            # Track engagement on OUR posts
            if self.our_did and f"at://{self.our_did}/" in target_uri:
                self._our_engagers.append({
                    "did": liker_did,
                    "action": "like",
                    "target_uri": target_uri,
                    "ts": time.time(),
                })
                if len(self._our_engagers) > MAX_ENGAGERS:
                    self._our_engagers = self._our_engagers[-MAX_ENGAGERS:]

    def _evict_old_posts(self):
        """Remove posts older than the velocity window to bound memory."""
        cutoff = time.time() - VELOCITY_WINDOW_SECONDS * 2
        stale = [uri for uri, p in self._post_tracker.items() if p["ts"] < cutoff]
        for uri in stale:
            del self._post_tracker[uri]

    def maybe_flush(self):
        """Write accumulated state to disk if enough time has passed."""
        if time.time() - self._last_flush < FLUSH_INTERVAL_SECONDS:
            return
        with self._lock:
            blob = {
                "ts": time.time(),
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "velocity_posts": list(self._velocity_hits[-MAX_VELOCITY_POSTS:]),
                "our_engagers": list(self._our_engagers[-MAX_ENGAGERS:]),
                "tracked_posts_count": len(self._post_tracker),
            }
            # Reset engagers after flush so we don't re-process them.
            self._our_engagers = []
        try:
            self.atomic_write(self.telemetry_file, blob)
            logger.info(f"[FIREHOSE] flushed: {len(blob['velocity_posts'])} velocity hits, "
                        f"{len(blob['our_engagers'])} engagers, "
                        f"{blob['tracked_posts_count']} tracked posts")
        except Exception as e:
            logger.warning(f"[FIREHOSE] flush failed: {e}")
        self._last_flush = time.time()


async def _run_websocket(daemon):
    """Connect to Jetstream and feed events into the daemon. Reconnects
    with exponential backoff on any failure."""
    try:
        import websockets
    except ImportError:
        logger.error("[FIREHOSE] 'websockets' package not installed. "
                     "Run: pip install websockets")
        return

    backoff = INITIAL_BACKOFF
    while True:
        try:
            logger.info(f"[FIREHOSE] connecting to Jetstream...")
            async with websockets.connect(
                JETSTREAM_URI,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
                max_size=1024 * 1024,
            ) as ws:
                logger.info("[FIREHOSE] connected. Streaming events.")
                backoff = INITIAL_BACKOFF  # Reset on successful connect
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                        daemon.handle_event(event)
                    except json.JSONDecodeError:
                        pass
                    daemon.maybe_flush()
        except asyncio.CancelledError:
            logger.info("[FIREHOSE] cancelled, shutting down.")
            break
        except Exception as e:
            logger.warning(f"[FIREHOSE] connection lost: {e}. "
                           f"Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


def _thread_main(our_did, telemetry_file, atomic_write_fn, is_relevant_fn):
    """Entry point for the daemon thread. Creates its own event loop."""
    daemon = FirehoseDaemon(our_did, telemetry_file, atomic_write_fn, is_relevant_fn)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_websocket(daemon))
    except Exception as e:
        logger.error(f"[FIREHOSE] daemon thread crashed: {e}")
    finally:
        loop.close()


def start(our_did, telemetry_file, atomic_write_fn, is_relevant_fn):
    """Launch the firehose daemon as a background thread. Returns the
    thread object (caller does not need to join; it is a daemon thread).

    Arguments:
        our_did: the agent's DID, so we can detect engagement on our posts.
        telemetry_file: Path to the network_telemetry.json file.
        atomic_write_fn: callable(path, data) for crash-safe JSON writes.
        is_relevant_fn: callable(text) to detect if text matches the niche.
    """
    t = threading.Thread(
        target=_thread_main,
        args=(our_did, telemetry_file, atomic_write_fn, is_relevant_fn),
        name="firehose-daemon",
        daemon=True,
    )
    t.start()
    logger.info(f"[FIREHOSE] daemon thread started (tid={t.ident})")
    return t
