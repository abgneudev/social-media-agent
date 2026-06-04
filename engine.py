"""Engine: FollowerEngine plus the heartbeat/status writer and small
ranking helpers (wilson_lower_bound, hook_strength).

This module is the orchestrator. It composes Store, BlueskyAdapter,
CircuitBreaker, and RateBudget; runs the sense -> learn -> decide -> act
loop; and exposes update_stall_counter for the runtime to call after every
tick (including ticks that raised).
"""
import os
import re
import json
import math
import time
import uuid
import random

from atproto import exceptions
from groq import Groq

import config
from config import (
    logger, content_hash, is_relevant_text,
    NAME_TEXT, BIO_TEXT, PERSONA,
    SECTORS, POST_HOOKS, REPLY_HOOKS,
    POST_HOOK_GUIDANCE, REPLY_HOOK_GUIDANCE,
    KEYWORD_MAP, RELEVANCE_RE,
    SENSITIVE_PHRASES, SENSITIVE_WORDS, SPAM_PHRASES,
    RATE_BUDGETS, GROWTH_PHASES,
    FOLLOWER_TARGET, MAX_LIKES_PER_TICK,
    ANCHOR_POST_TARGET, PROFILE_OPT_MIN_TRIALS, PROFILE_OPT_COOLDOWN_TICKS,
    PENDING_GRACE_SECONDS, STALL_THRESHOLD,
    TRACTION_REWARD_CAP,
    ANALYZER_CADENCE_TICKS, EXPLORATION_NUDGE_MAX, TOPIC_ANGLES_PER_PROMPT,
)
from store import Store, atomic_write_json, load_json
from governance import RateBudget, CircuitBreaker
from adapter import BlueskyAdapter
import analyzer
import klipy
import serper


# ==========================================
# HONEST RANKING
# ==========================================
def wilson_lower_bound(successes, trials, z=1.96):
    if trials == 0:
        return 0.0
    p = max(0.0, min(1.0, successes / trials))
    denom = 1 + z * z / trials
    center = p + z * z / (2 * trials)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials)
    return (center - margin) / denom


def hook_strength(text, archetype=None):
    """Cheap proxy for hook quality so we can pick among generated variants
    without an extra API call. Rewards a short, curious, concrete first line.
    Archetype adjusts the length expectation: one_line_provocation should be
    very short, single_question should end in '?', mini_thread judges only
    the first part."""
    if not text:
        return -1.0
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0]
    score = 0.0
    fl = len(first)
    if fl <= 90:
        score += 2.0
    elif fl > 140:
        score -= 1.5
    low = first.lower()
    if "?" in first:
        score += 1.0
    if re.match(r"^\s*\d", first) or low.startswith(("most ", "the ", "why ", "here's", "everyone ")):
        score += 1.0
    if any(low.startswith(g) for g in ("in this", "today i", "let's talk", "i want to", "so i")):
        score -= 1.5
    total = len(text)
    if archetype == "one_line_provocation":
        if total <= 120:
            score += 1.5
        elif total > 160:
            score -= 1.5
    elif archetype == "single_question":
        if text.strip().endswith("?"):
            score += 1.5
        else:
            score -= 1.0
    elif archetype == "mini_thread":
        if total <= 200:
            score += 1.0
    elif archetype == "before_after":
        if 80 <= total <= 240:
            score += 1.0
    else:
        if 120 <= total <= 280:
            score += 1.0
    return score


# ==========================================
# HEARTBEAT
# ==========================================
def write_status(engine):
    """Heartbeat written at the end of every loop iteration. Atomic via the
    existing helper, so an operator running `cat status.json` or a monitoring
    scrape never observes a partial write. Captures just the fields you'd
    need at 3am to decide whether the daemon is healthy: when it last
    ticked, who it thinks it is, what it just did, and whether the breaker
    or stall counter are in trouble."""
    snap = engine.store.snapshots[-1] if engine.store.snapshots else None
    atomic_write_json(config.STATUS_FILE, {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tick": engine.store.tick,
        "phase": engine.store.phase,
        "followers": snap.get("followers") if snap else None,
        "last_action": getattr(engine, "_last_action", None),
        "breaker_state": engine.breaker.state,
        "consecutive_empty_ticks": engine.store.consecutive_empty_ticks,
        "pending_writes": len(engine.store.pending),
        "anchor_posts": engine.store.anchor_posts,
    })


# ==========================================
# THE ENGINE
# ==========================================
class FollowerEngine:
    def __init__(self, handle, password):
        self.store = Store()
        self.net = BlueskyAdapter(handle, password)
        self.ai = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        self.breaker = CircuitBreaker()
        self.rate = {k: RateBudget(v["capacity"], v["refill_per_sec"])
                     for k, v in RATE_BUDGETS.items()}
        self.sector_activity = {}
        self.sector_posts = {}      # cache of fetched posts per sector, reused in _act
        self.persona = PERSONA
        # niche_insights blob is re-read at tick boundaries (cheap, atomic
        # read). Cached here so the variant prompt and sampler do not race
        # with an in-flight analyzer write.
        self._insights = None
        # Network telemetry from the firehose daemon (background thread).
        # Read once per tick; None if the daemon has not flushed yet.
        self._network_telemetry = None
        # Registry of all lists the agent has created, loaded from disk.
        # Each entry: {name, description, uri, created_ts, member_count}
        self._list_registry = self._load_list_registry()
        self._list_members = set()  # DIDs already added to any list this session
        self.known_follows = self.net.get_all_follows()
        logger.info(f"      [NET] loaded {len(self.known_follows)} known follow(s) for deduplication.")
        if self._list_registry:
            logger.info(f"      [LIST] loaded {len(self._list_registry)} existing list(s) from registry.")
            
        self._rebuild_telemetry()
        self._bootstrap_taxonomy()

    def _rebuild_telemetry(self):
        """Restore config.KEYWORD_MAP and config.RELEVANCE_SIGNALS from telemetry
        so the agent doesn't forget its dynamically learned vocabulary on reboot."""
        rebuilt_count = 0
        for kw, stats in self.store.keyword_telemetry.items():
            if not stats.get("active", True):
                continue
            sector = stats["sector"]
            if sector in config.KEYWORD_MAP and kw not in config.KEYWORD_MAP[sector]:
                config.KEYWORD_MAP[sector].append(kw)
                rebuilt_count += 1
            if kw not in config.RELEVANCE_SIGNALS:
                config.RELEVANCE_SIGNALS.append(kw)
        
        if rebuilt_count > 0:
            config.RELEVANCE_RE = re.compile(
                r"\b(" + "|".join(re.escape(s) for s in config.RELEVANCE_SIGNALS) + r")s?\b",
                re.IGNORECASE,
            )
            logger.info(f"      [BOOTSTRAP] rebuilt {rebuilt_count} active keywords from local telemetry.")

    def _bootstrap_taxonomy(self):
        """If any sector has zero keywords in memory, autonomously ping the LLM
        to build a 10-keyword starting vocabulary using the Persona."""
        bootstrapped = False
        for sector in config.SECTORS:
            if not config.KEYWORD_MAP[sector]:
                logger.info(f"      [BOOTSTRAP] sector '{sector}' is empty. Generating taxonomy autonomously...")
                prompt = (
                    f"You are an autonomous taxonomy generator for a social media agent.\n"
                    f"Our persona is:\n{config.PERSONA}\n\n"
                    f"Generate exactly 10 highly specific, modern, and discoverable search keywords "
                    f"that intersect our persona with the topic of '{sector}'. "
                    f"CRITICAL RULE: Each keyword MUST be extremely short (1-3 words max). Do not generate long sentences or phrases, they will fail the search engine! "
                    f"At least 3 of these keywords MUST explicitly target top organizations, authoritative brands, or high-tier institutional pages. "
                    f"CRITICAL DIVERSITY: Do not anchor on obvious examples like Google or Figma. You must continuously explore the entire tech ecosystem (e.g., Stripe, Linear, Anthropic, NN/g, specialized research labs, etc). "
                    f"Do not return generic terms. Ensure they are phrases people actually use.\n"
                    f"Respond strictly as JSON:\n"
                    f'{{"keywords": ["kw1", "kw2", "kw3", "kw4", "kw5", "kw6", "kw7", "kw8", "kw9", "kw10"]}}'
                )
                raw = self._generate(prompt, dedup=False)
                if raw:
                    try:
                        kws = json.loads(raw).get("keywords", [])
                        added = 0
                        for kw in kws:
                            kw = kw.strip().lower()
                            if kw and kw not in config.KEYWORD_MAP[sector]:
                                config.KEYWORD_MAP[sector].append(kw)
                                config.RELEVANCE_SIGNALS.append(kw)
                                self.store.keyword_telemetry[kw] = {
                                    "sector": sector, "trials": 0, "successes": 0,
                                    "total_engagement": 0.0, "active": True
                                }
                                added += 1
                        if added > 0:
                            bootstrapped = True
                            logger.info(f"      [BOOTSTRAP] successfully injected {added} keywords for '{sector}'.")
                    except Exception as e:
                        logger.warning(f"      [FAULT] failed to parse bootstrap taxonomy for '{sector}': {e}")
        
        if bootstrapped:
            config.RELEVANCE_RE = re.compile(
                r"\b(" + "|".join(re.escape(s) for s in config.RELEVANCE_SIGNALS) + r")s?\b",
                re.IGNORECASE,
            )
            self.store.save_keyword_telemetry()

    # ---- kill switch ----
    def _halted(self) -> bool:
        try:
            return config.KILL_SWITCH_FILE.read_text(encoding="utf-8").strip().upper() == "HALTED"
        except FileNotFoundError:
            return False

    # ---- autonomous list management ----
    def _load_list_registry(self):
        """Load the list registry from disk. Returns a list of dicts, each
        describing a list the agent previously created."""
        data = load_json(config.CURATED_LIST_FILE, None)
        if isinstance(data, list):
            return data
        # Migrate from the old single-list format
        if isinstance(data, dict) and data.get("uri"):
            registry = [{
                "name": "Curated List",
                "description": "",
                "uri": data["uri"],
                "created_ts": data.get("created_ts", time.time()),
                "member_count": 0,
            }]
            self._save_list_registry(registry)
            return registry
        return []

    def _save_list_registry(self, registry=None):
        """Persist the list registry to disk."""
        atomic_write_json(config.CURATED_LIST_FILE, registry or self._list_registry)

    def _create_list(self, name, description):
        """Create a new list and register it. Returns the URI or None."""
        uri = self.net.create_list(name, description)
        if uri:
            entry = {
                "name": name,
                "description": description,
                "uri": uri,
                "created_ts": time.time(),
                "member_count": 0,
            }
            self._list_registry.append(entry)
            self._save_list_registry()
            logger.info(f"   [LIST] agent created new list: '{name}'")
        return uri

    def _add_to_list(self, list_uri, target_did):
        """Add a user to a list. Deduplicates in-session."""
        if target_did in self._list_members or target_did == self.net.did:
            return False
        if self.net.add_to_list(list_uri, target_did):
            self._list_members.add(target_did)
            # Update member count in registry
            for entry in self._list_registry:
                if entry["uri"] == list_uri:
                    entry["member_count"] = entry.get("member_count", 0) + 1
                    self._save_list_registry()
                    break
            return True
        return False

    # ---- network telemetry reader ----
    def _read_network_telemetry(self):
        """Read the firehose daemon's telemetry file. Graceful absence:
        returns None if the file does not exist or is malformed."""
        blob = load_json(config.NETWORK_TELEMETRY_FILE, None)
        if isinstance(blob, dict):
            return blob
        return None

    def _autonomous_curation(self):
        """LLM-driven list curation. The agent analyzes firehose telemetry
        and its own engagement data, then decides what lists to create or
        populate. This is the agent's full authority over its list strategy."""
        if not self._network_telemetry:
            return

        engagers = self._network_telemetry.get("our_engagers", [])
        velocity_posts = self._network_telemetry.get("velocity_posts", [])
        if not engagers and not velocity_posts:
            return

        # Build profiles for engagers (cap API calls)
        engager_profiles = []
        for eng in engagers[:8]:
            did = eng.get("did")
            if not did or did in self._list_members or did == self.net.did:
                continue
            try:
                profile = self.net.get_profile(did)
                if not profile:
                    continue
                followers = int(getattr(profile, "followers_count", 0) or 0)
                posts = int(getattr(profile, "posts_count", 0) or 0)
                if followers < 10 or posts < 5:
                    continue  # Skip bots/empty accounts
                bio = (getattr(profile, "description", "") or "")[:120]
                handle = getattr(profile, "handle", "unknown")
                engager_profiles.append({
                    "did": did, "handle": handle,
                    "followers": followers, "posts": posts, "bio": bio,
                })
            except Exception:
                continue

        if not engager_profiles:
            return

        # Build context for the LLM
        existing_lists_desc = "None yet." if not self._list_registry else "\n".join(
            f"- \"{entry['name']}\" ({entry.get('member_count', 0)} members): {entry.get('description', '')[:80]}"
            for entry in self._list_registry
        )
        velocity_desc = ""
        if velocity_posts:
            samples = velocity_posts[:5]
            velocity_desc = "Recent high-velocity posts on the network:\n" + "\n".join(
                f"- ({v.get('likes_in_window', 0)} likes in {v.get('window_seconds', 0)}s): {v.get('text', '')[:100]}"
                for v in samples
            )

        engager_desc = "\n".join(
            f"- @{p['handle']} ({p['followers']} followers, {p['posts']} posts): {p['bio']}"
            for p in engager_profiles
        )

        prompt = (
            f"You are the curation strategist for an autonomous social media agent focused on "
            f"UX/UI design, frontend engineering, and adjacent disciplines.\n\n"
            f"YOUR EXISTING LISTS:\n{existing_lists_desc}\n\n"
            f"PEOPLE WHO RECENTLY ENGAGED WITH OUR CONTENT:\n{engager_desc}\n\n"
            f"{velocity_desc}\n\n"
            f"AVAILABLE ACTIONS:\n"
            f"1. create_list: Create a brand new curated list with a name and description\n"
            f"2. add_to_list: Add a user (by handle) to an existing list\n"
            f"3. skip: Do nothing this cycle\n\n"
            f"RULES:\n"
            f"- Only create a new list if no existing list fits the users' profiles\n"
            f"- List names should be specific and discoverable (e.g., 'Motion Design Pioneers', "
            f"'Accessibility Advocates', 'Systems Thinkers in Design')\n"
            f"- Only add users who genuinely fit a list's theme\n"
            f"- You may issue multiple actions in one response\n"
            f"- Max 3 actions per cycle\n\n"
            f'Respond strictly as JSON: {{"actions": ['
            f'{{"type": "create_list", "name": "...", "description": "..."}}, '
            f'{{"type": "add_to_list", "list_name": "...", "handle": "..."}}, '
            f'{{"type": "skip"}}]}}'
        )

        raw = self._generate(prompt, dedup=False)
        if not raw:
            return

        try:
            actions = json.loads(raw).get("actions", [])
        except Exception as e:
            logger.warning(f"   [CURATE] LLM response malformed: {e}")
            return

        for action in actions[:3]:
            action_type = action.get("type")

            if action_type == "create_list":
                name = action.get("name", "").strip()
                desc = action.get("description", "").strip()
                if name and len(name) <= 64:
                    self._create_list(name, desc[:300])

            elif action_type == "add_to_list":
                list_name = action.get("list_name", "").strip()
                handle = action.get("handle", "").strip().lstrip("@")
                if not list_name or not handle:
                    continue
                # Find the list by name
                target_list = None
                for entry in self._list_registry:
                    if entry["name"].lower() == list_name.lower():
                        target_list = entry
                        break
                if not target_list:
                    logger.info(f"   [CURATE] list '{list_name}' not found, skipping add")
                    continue
                # Find the DID from our profiled engagers
                target_did = None
                for p in engager_profiles:
                    if p["handle"].lower() == handle.lower():
                        target_did = p["did"]
                        break
                if target_did:
                    if self._add_to_list(target_list["uri"], target_did):
                        logger.info(f"   [CURATE] added @{handle} to '{list_name}'")

            elif action_type == "skip":
                logger.info("   [CURATE] agent decided to skip curation this cycle")
                break

    # ---- bootstrap ----
    def bootstrap(self):
        if self.store.phase != "bootstrap":
            return
        logger.info("[BOOTSTRAP] setting profile conversion surface")
        self.net.set_profile(NAME_TEXT, BIO_TEXT)
        self.store.phase = "explore"
        self.store.save_engine()

    # ---- crash-safe publishing ----
    def _publish_with_reconcile(self, kind, text, sector, hook, write_fn,
                                 learnable=True, target_did=None, target_handle=None, keyword=None):
        """Crash-safe wrapper around a publishing write (post/reply/quote).

        Persists the intent BEFORE the network write. If the write raises but
        the post actually landed (response lost mid-flight, socket reset after
        the server committed, etc.), find_post recovers the URI from our
        author feed and we treat it as success. If the write raises and
        nothing landed, we leave the intent in place and re-raise so the
        caller counts a real failure; the next tick's _reconcile_pending will
        pick up any intent that turns out to have landed after all.

        Returns (intent_id, uri). The caller is responsible for the rest of
        the bookkeeping (log_action, mark_seen, save_engine) and for calling
        store.remove_pending(intent_id) once that bookkeeping has succeeded.
        """
        ch = content_hash(text)
        intent_id = uuid.uuid4().hex[:12]
        self.store.add_pending({
            "intent_id": intent_id, "kind": kind, "content_hash": ch,
            "text": text, "sector": sector, "hook": hook, "keyword": keyword,
            "target_did": target_did, "target_handle": target_handle,
            "learnable": learnable, "ts": time.time(),
        })
        try:
            uri = write_fn(text)
        except exceptions.AtProtocolError as e:
            found = self.net.find_post(ch)
            if found and found[0]:
                uri = found[0]
                logger.info(f"      [RECONCILE] {kind} raised {type(e).__name__} but "
                            f"post landed at {uri[:60]}; treating as success")
                return intent_id, uri
            raise
        return intent_id, uri

    def _reconcile_pending(self):
        """Resolve any unfinalized intents from earlier ticks or a prior
        process. For each, scan our author feed by content_hash. If found,
        record a ledger entry (unless one already exists for that URI) and
        clear the intent. Intents older than PENDING_GRACE_SECONDS that still
        cannot be located are dropped: we assume they never landed."""
        pendings = self.store.list_pending()
        if not pendings:
            return
        logger.info(f"[RECONCILE] checking {len(pendings)} pending write(s)")
        for p in pendings:
            ch = p.get("content_hash") or content_hash(p.get("text", "") or "")
            found = self.net.find_post(ch)
            if found and found[0]:
                uri = found[0]
                already = any(a.get("uri") == uri for a in self.store.ledger)
                if not already:
                    self.store.log_action(
                        p.get("kind"), p.get("sector"), p.get("hook"),
                        uri=uri,
                        target_did=p.get("target_did"),
                        target_handle=p.get("target_handle"),
                        text=p.get("text"),
                        learnable=p.get("learnable", True),
                        keyword=p.get("keyword")
                    )
                self.store.mark_seen(ch)
                self.store.remove_pending(p["intent_id"])
                logger.info(f"   [RECONCILE] {p.get('kind')} -> {uri[:60]}")
            elif (time.time() - p.get("ts", 0)) > PENDING_GRACE_SECONDS:
                logger.info(f"   [RECONCILE] dropping stale {p.get('kind')} intent "
                            f"({int(time.time() - p.get('ts', 0))}s old, never landed)")
                self.store.remove_pending(p["intent_id"])

    # ---- main tick ----
    def tick(self):
        self.store.tick += 1
        t = self.store.tick
        # Reset per-tick state up front so update_stall_counter (which runs in
        # the runtime loop even when tick() raises) sees consistent values.
        # _last_action carries across ticks so status.json keeps showing the
        # last meaningful action; it is only overwritten when something new
        # actually happens.
        self._tick_actions = 0
        self._tick_active = False
        if not hasattr(self, "_last_action"):
            self._last_action = None
        logger.info(f"\n{'='*60}\n[TICK {t}] {time.strftime('%Y-%m-%d %X')}\n{'='*60}")

        if self._halted():
            logger.info("[HALTED] kill switch on. No network calls.")
            return
        if self.breaker.is_open():
            logger.info("[BREAKER] open. Skipping tick.")
            return
        # Past the gates: this tick is genuinely trying to do work, so the
        # stall counter should evaluate it.
        self._tick_active = True

        # Resolve any pending intents from a prior tick or process before we
        # start generating new content, so the dedup gates see the right state.
        self._reconcile_pending()

        # Niche insights are a cheap atomic read; refresh once per tick so
        # any in-flight analyzer write is picked up by the next pass without
        # racing the read.
        self._insights = analyzer.load_insights()
        # Network telemetry from the firehose daemon (background thread).
        self._network_telemetry = self._read_network_telemetry()

        if not self._sense():
            return
        if t % 5 == 1:
            self._sense_trends()

        self._learn()
        self.store.decay()
        self._maybe_optimize_profile()

        # Analyzer pass on cadence. Runs late in the tick (after sense and
        # learn) so it has fresh search results to work with. First pass
        # fires early so we are not flying blind for ANALYZER_CADENCE_TICKS.
        if t == 1 or t % ANALYZER_CADENCE_TICKS == 0:
            self._run_analyzer()

        sector, post_hook, reply_hook = self._decide()
        self._act(sector, post_hook, reply_hook)

        if t % 4 == 0:
            self._courtesy_follow_back()
            
        if t % 15 == 0:
            self._run_evolution()

        # Autonomous list curation every 10 ticks (LLM-driven)
        if t % 10 == 0:
            try:
                self._autonomous_curation()
            except Exception as e:
                logger.warning(f"   [CURATE] autonomous curation failed: {e}")
            
        self.store.save_engine()

    def _run_analyzer(self):
        """One analyzer pass guarded by the same kill switch and breaker
        checks as the main tick. Failures are logged and swallowed: the
        analyzer is a luxury, never load-bearing for posting."""
        if self._halted() or self.breaker.is_open():
            return
        try:
            blob = analyzer.run(self.net, lambda prompt: self._generate(prompt, dedup=False))
            if blob is not None:
                self._insights = blob
        except Exception as e:
            logger.warning(f"   [ANALYZER] run raised: {e}")

    def _mark_action(self, kind, **details):
        """Single touch-point for every successful network action: bumps the
        per-tick action counter (drives the stall detector) and records the
        most recent action for status.json so an off-box monitor can see what
        the agent just did without parsing logs."""
        self._tick_actions += 1
        self._last_action = {"kind": kind, "ts": time.time(), **details}

    def update_stall_counter(self):
        """Runs from the runtime loop after every tick (including when the
        tick raised). If a genuinely-active tick produced zero successful
        network actions, increment the empty-tick counter; once it reaches
        STALL_THRESHOLD, force the breaker open so the daemon stops chewing
        cycles silently instead of looking alive while doing nothing.

        Skipped when the tick never became active (halted, breaker already
        open). On any action, the counter resets."""
        if not getattr(self, "_tick_active", False):
            return
        if getattr(self, "_tick_actions", 0) == 0:
            self.store.consecutive_empty_ticks += 1
            logger.warning(f"   [STALL] empty tick "
                           f"{self.store.consecutive_empty_ticks}/{STALL_THRESHOLD}")
            if self.store.consecutive_empty_ticks >= STALL_THRESHOLD:
                logger.error(f"[STALL] {STALL_THRESHOLD} consecutive empty ticks; "
                             f"tripping breaker to surface the problem")
                self.breaker.trip_open(reason=f"stall ({STALL_THRESHOLD} empty ticks)")
        else:
            if self.store.consecutive_empty_ticks:
                logger.info(f"   [STALL] action observed, resetting empty counter "
                            f"(was {self.store.consecutive_empty_ticks})")
            self.store.consecutive_empty_ticks = 0
        self.store.save_engine()

    # ---- SENSE ----
    def _sense(self) -> bool:
        try:
            followers = self.net.follower_count()
            self.breaker.record_success()
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] follower read failed: {e}")
            self.breaker.record_failure()
            return False

        prev = self.store.snapshots[-1]["followers"] if self.store.snapshots else followers
        delta = followers - prev
        self.store.ewma_growth = 0.5 * delta + 0.5 * self.store.ewma_growth
        self.store.snapshots.append({"ts": time.time(), "tick": self.store.tick,
                                     "followers": followers, "delta": delta})
        self.store.save_snapshots()
        self.store.save_engine()
        logger.info(f"[SENSE] followers={followers} (delta {delta:+d}) ewma={self.store.ewma_growth:+.2f}")
        if followers >= FOLLOWER_TARGET:
            logger.info(f"[GOAL] reached {followers} >= {FOLLOWER_TARGET}. Continuing to compound.")

        # Full sector scan every other tick; reuse the cache otherwise. Cuts API calls
        # and feeds both _decide (activity) and _act (candidate reuse).
        if self.store.tick % 2 == 1 or not self.sector_activity:
            logger.info("[SENSE] scanning network activity across sectors")
            self.sector_activity, self.sector_posts = {}, {}
            for sector in SECTORS:
                if not KEYWORD_MAP.get(sector):
                    logger.warning(f"   {sector}: no keywords available (bootstrapper failed or empty). Skipping.")
                    continue
                keyword = random.choice(KEYWORD_MAP[sector])
                try:
                    posts = self.net.search_posts(keyword, limit=12)
                    self.breaker.record_success()
                except exceptions.AtProtocolError as e:
                    logger.info(f"   {sector}: scan failed ({e})")
                    self.breaker.record_failure()
                    self.sector_activity[sector] = {"keyword": keyword, "total": 0}
                    self.sector_posts[sector] = []
                    continue
                self.sector_posts[sector] = posts
                self.sector_activity[sector] = {"keyword": keyword, "total": len(posts)}
                label = "hot" if len(posts) >= 7 else "warm" if len(posts) >= 3 else "quiet"
                logger.info(f"   {sector}: {len(posts)} posts for '{keyword}' [{label}]")
        return True

    def _sense_trends(self):
        hottest, best = None, -1
        for sector, data in self.sector_activity.items():
            if data.get("total", 0) > best:
                best, hottest = data["total"], sector
        if not hottest or best == 0:
            return
        posts = self.sector_posts.get(hottest, [])
        texts = [p.record.text for p in posts if getattr(p.record, "text", None)][:10]
        if not texts:
            return
        batch = "\n---\n".join(texts)
        prompt = (
            f"These are recent posts in the '{hottest}' space:\n{batch}\n\n"
            f"Extract exactly 3 specific, trending keywords or concepts people are "
            f"actively discussing. Respond strictly as JSON: "
            f'{{"keywords": ["kw1","kw2","kw3"]}}'
        )
        raw = self._generate(prompt, dedup=False)
        if not raw:
            return
        try:
            kws = json.loads(raw).get("keywords", [])
            if kws:
                self.store.trends[hottest] = kws
                self.store.save_engine()
                logger.info(f"   [TRENDS] {hottest}: {kws}")
        except Exception as e:
            logger.warning(f"   [FAULT] trend parsing failed: {e}")

    # ---- LEARN ----
    def _learn(self):
        due = self.store.mature_actions()
        if not due:
            logger.info("[LEARN] nothing matured yet.")
            return
        logger.info(f"[LEARN] scoring {len(due)} matured action(s)")
        for a in due:
            try:
                eng = 0
                if a["kind"] == "follow":
                    # Follow attribution stays binary: a follow-back either
                    # happened or it did not.
                    reward = 1.0 if self.net.followed_back_by(a["target_did"]) else 0.0
                    label = "FOLLOW-BACK" if reward >= 1.0 else "no follow-back"
                else:
                    # Content actions use binary reward for bandit math (Beta-Binomial invariant),
                    # but we extract EXACT engagement for reporting and Wilson lower bounds.
                    eng = float(self.net.post_engagement(a["uri"]))
                    reward = 1.0 if eng > 0 else 0.0
                    label = f"engagement={eng} (reward={reward})"
                self.breaker.record_success()
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] scoring failed, retry next tick: {e}")
                self.breaker.record_failure()
                continue
            
            self.store.update("sector", a["sector"], reward)
            # Add to engagement telemetry directly
            if "engagement" not in self.store.bandit["sector"][a["sector"]]:
                self.store.bandit["sector"][a["sector"]]["engagement"] = 0.0
            self.store.bandit["sector"][a["sector"]]["engagement"] += eng

            if a["kind"] in ("post", "quote"):
                self.store.update("post_hook", a["hook"], reward)
                if "engagement" not in self.store.bandit["post_hook"][a["hook"]]:
                    self.store.bandit["post_hook"][a["hook"]]["engagement"] = 0.0
                self.store.bandit["post_hook"][a["hook"]]["engagement"] += eng
            elif a["kind"] == "reply":
                self.store.update("reply_hook", a["hook"], reward)
                if "engagement" not in self.store.bandit["reply_hook"][a["hook"]]:
                    self.store.bandit["reply_hook"][a["hook"]]["engagement"] = 0.0
                self.store.bandit["reply_hook"][a["hook"]]["engagement"] += eng

            # Update granular keyword telemetry
            kw = a.get("keyword")
            if kw and kw in self.store.keyword_telemetry:
                self.store.keyword_telemetry[kw]["trials"] += 1
                if reward >= 1.0:
                    self.store.keyword_telemetry[kw]["successes"] += 1
                self.store.keyword_telemetry[kw]["total_engagement"] += eng
                self.store.save_keyword_telemetry()

            self.store.mark_matured(a["id"])
            logger.info(f"   -> {a['kind']} [{a['sector']}/{a['hook']}]: {label}")

    # ---- profile optimization (data-sufficiency trigger) ----
    def _maybe_optimize_profile(self):
        if (self.store.tick - self.store.last_profile_opt_tick) < PROFILE_OPT_COOLDOWN_TICKS:
            return
        best_sector, best_e = None, -1
        for sector, arm in self.store.bandit["sector"].items():
            trials = (arm["alpha"] - 1) + (arm["beta"] - 1)
            e = arm["alpha"] / (arm["alpha"] + arm["beta"])
            if trials >= PROFILE_OPT_MIN_TRIALS and e > best_e:
                best_e, best_sector = e, sector
        if not best_sector:
            return
        logger.info(f"[OPT] sector {best_sector} has enough trials ({trials}). Profiling bio...")
        # ... logic for opt (if any) ...
        self.store.last_profile_opt_tick = self.store.tick

    # ---- evolution ----
    def _run_evolution(self):
        """Safe EvolutionEngine: discover, expand, prune."""
        # 3A. Discover
        logger.info("[EVOLUTION] starting discovery cycle")
        try:
            timeline = self.net.fetch_timeline(limit=30)
            # Some platforms return an object where text is accessible, or record.text
            texts = []
            for p in timeline:
                if hasattr(p, "record") and hasattr(p.record, "text"):
                    texts.append(p.record.text)
                elif hasattr(p, "text"):
                    texts.append(p.text)
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] evolution timeline fetch failed: {e}")
            return
            
        if not texts:
            logger.info("   [EVOLUTION] timeline empty, skipping")
            return
            
        batch = "\n---\n".join([t for t in texts if t][:20])
        prompt = (
            f"You are an autonomous network analyst optimizing an agent's search engine.\n"
            f"The agent's persona is:\n{config.PERSONA}\n\n"
            f"These are recent posts from our timeline:\n{batch}\n\n"
            f"Your objective is to find 3 highly specific, novel search queries that expand our current niche. "
            f"Look for intersections between the persona's core focus and structural patterns in the timeline. "
            f"CRITICAL RULE: Each keyword MUST be extremely short (1-3 words max). Do not generate long phrases. "
            f"At least one keyword MUST explicitly target a top organization, authoritative brand, or high-tier institutional page. "
            f"CRITICAL DIVERSITY: Constantly rotate and discover new institutions. Do not anchor on the same major companies. Target specialized startups, diverse research labs, and different tech brands. "
            f"Do not return generic terms. Return specific intersections (e.g., 'Fitts law touch targets', "
            f"'latency in generative UI'). These terms must be discoverable on a social media network.\n"
            f"Respond strictly as JSON: "
            f'{{"keywords": ["kw1", "kw2", "kw3"]}}'
        )
        logger.info(f"   [EVOLUTION] expanding search taxonomy based on persona")
        raw = self._generate(prompt, dedup=False)
        if not raw:
            return
            
        try:
            kws = json.loads(raw).get("keywords", [])
        except Exception as e:
            logger.warning(f"   [FAULT] evolution JSON parsing failed: {e}")
            return

        # 3B. Expand (State Mutation)
        best_sector = max(self.store.bandit["sector"].items(), key=lambda x: x[1]["alpha"] / (x[1]["alpha"] + x[1]["beta"]))[0]
        added = False
        
        for kw in kws:
            kw = kw.strip().lower()
            if not kw or len(kw) < 3:
                continue
            if not self._passes_gates(kw):
                logger.info(f"   [EVOLUTION] keyword '{kw}' rejected by safety gates")
                continue
            
            if kw in config.KEYWORD_MAP[best_sector] or kw in self.store.keyword_telemetry:
                continue
            
            logger.info(f"   [EVOLUTION] discovered and accepted new keyword: '{kw}' for sector '{best_sector}'")
            self.store.keyword_telemetry[kw] = {
                "sector": best_sector, "trials": 0, "successes": 0,
                "total_engagement": 0.0, "active": True
            }
            config.KEYWORD_MAP[best_sector].append(kw)
            config.RELEVANCE_SIGNALS.append(kw)
            added = True
            
        if added:
            config.RELEVANCE_RE = re.compile(
                r"\\b(" + "|".join(re.escape(s) for s in config.RELEVANCE_SIGNALS) + r")s?\\b",
                re.IGNORECASE,
            )
            self.store.save_keyword_telemetry()

        # 3C. Prune (Garbage Collection)
        for kw, stats in list(self.store.keyword_telemetry.items()):
            if not stats.get("active", True):
                continue
            
            trials = stats["trials"]
            if trials >= 15:
                sector = stats["sector"]
                sector_arm = self.store.bandit["sector"][sector]
                core_trials = (sector_arm["alpha"] - 1) + (sector_arm["beta"] - 1)
                
                # We use total_engagement for continuous WLB as requested
                core_wlb = wilson_lower_bound(sector_arm.get("engagement", 0.0), core_trials) if core_trials > 0 else 0.0
                kw_wlb = wilson_lower_bound(stats["total_engagement"], trials)
                
                if kw_wlb < core_wlb * 0.5: # Clearly below baseline
                    logger.info(f"   [EVOLUTION] retiring keyword '{kw}' (trials={trials}, wlb={kw_wlb:.3f} < baseline={core_wlb:.3f})")
                    stats["active"] = False
                    if kw in config.KEYWORD_MAP.get(sector, []):
                        config.KEYWORD_MAP[sector].remove(kw)
                    self.store.save_keyword_telemetry()
        logger.info(f"[OPTIMIZE] rewriting bio around best sector '{best_sector}'")
        trends_info = ""
        if best_sector in self.store.trends:
            trends_info = f"Weave in these trends if natural: {', '.join(self.store.trends[best_sector])}. "
        prompt = (
            f"Write a bio (max 160 chars) for {NAME_TEXT}. Our strongest content is "
            f"in '{best_sector}'. {trends_info}Use clear keywords for that area, "
            f"explain complex things simply, warm and approachable. "
            f"CRITICAL DIVERSITY: Find a completely fresh angle. Do not reuse the exact same phrasing as your previous bios. "
            f"Must end with 'Boston based. https://abgneudev.github.io/Portfolio/ Automated account.' No hashtags. "
            f'Respond strictly as JSON: {{"bio": "..."}}'
        )
        raw = self._generate(prompt, dedup=False)
        if not raw:
            return
        try:
            bio = json.loads(raw)["bio"][:256]
            self.net.set_profile(NAME_TEXT, bio)
            self.store.last_profile_opt_tick = self.store.tick
            self.store.save_engine()
        except Exception as e:
            logger.info(f"   [OPTIMIZE] failed: {e}")

    # ---- DECIDE ----
    def _decide(self):
        logger.info("[DECIDE] Thompson sampling (dead sectors excluded)")
        sector_samples = []
        for sector in SECTORS:
            arm = self.store.bandit["sector"][sector]
            s = random.betavariate(arm["alpha"], arm["beta"])
            active = self.sector_activity.get(sector, {}).get("total", 0) > 0
            logger.info(f"   sector {sector}: Beta({arm['alpha']:.1f},{arm['beta']:.1f}) "
                        f"sample={s:.3f} active={active}")
            sector_samples.append((s, sector, active))
        live = [x for x in sector_samples if x[2]]
        pool = live if live else sector_samples
        sector = max(pool, key=lambda x: x[0])[1]

        def best_arm(dim, values):
            best, pick = -1.0, values[0]
            for v in values:
                arm = self.store.bandit[dim][v]
                s = random.betavariate(arm["alpha"], arm["beta"])
                if s > best:
                    best, pick = s, v
            return pick

        post_hook = best_arm("post_hook", POST_HOOKS)
        reply_hook = best_arm("reply_hook", REPLY_HOOKS)
        logger.info(f"   -> sector={sector} post_hook={post_hook} reply_hook={reply_hook}")
        return sector, post_hook, reply_hook

    # ---- ACT ----
    def _current_phase(self):
        followers = self.store.snapshots[-1].get("followers", 0) if self.store.snapshots else 0
        for max_f, name, weights in GROWTH_PHASES:
            if followers < max_f:
                return name, weights
        return GROWTH_PHASES[-1][1], GROWTH_PHASES[-1][2]

    def _act(self, sector, post_hook, reply_hook):
        phase_name, weights = self._current_phase()
        logger.info(f"[ACT] phase={phase_name} weights={ {k: round(v,2) for k,v in weights.items()} }")

        # 1. Seed anchor posts up front; keep posting in later phases.
        should_post = (self.store.anchor_posts < ANCHOR_POST_TARGET
                       or phase_name in ("compound", "community", "scaling")
                       or self.store.tick % 6 == 0)
        if should_post and self.rate["post"].try_consume():
            # _original_post samples its own distinct archetypes per variant;
            # the _decide-level post_hook is only used by _quote_best below.
            self._original_post(sector, keyword=sector)

        # 2. Candidates: reuse the sense-stage cache when possible; only search when
        #    a trend keyword overrides the cached sector keyword.
        candidates, keyword = self._candidates_for(sector)
        if not candidates:
            logger.info(f"[ACT] no relevant candidates for '{keyword}'. Retargeting next tick. Applying failure penalty.")
            self.store.update("sector", sector, 0.0)
            self.store.update("post_hook", post_hook, 0.0)
            self.store.update("reply_hook", reply_hook, 0.0)
            return
        logger.info(f"[ACT] {len(candidates)} relevant candidate(s) for '{keyword}'.")

        # 2b. Whale candidates: a wider pool that includes large creators
        #     for high-visibility replies. Separate from follow targets.
        whale_candidates, whale_kw = self._candidates_for(sector, allow_whales=True)

        # 3. Weighted action plan. Each action type fires with prob ~ its weight.
        plan = [a for a, w in weights.items() if random.random() < min(1.0, w * 2.5)]
        if post_hook == "amplify_and_praise":
            plan = ["quote"]
        if "like" not in plan:
            plan.append("like")
        random.shuffle(plan)
        for action in plan:
            if action == "follow" and self.rate["follow"].try_consume():
                self._strategic_follow(sector, post_hook, candidates, keyword)
            elif action == "reply" and self.rate["reply"].try_consume():
                reply_pool = whale_candidates if whale_candidates else candidates
                self._helpful_reply(sector, reply_hook, reply_pool, whale_kw if whale_candidates else keyword)
            elif action == "quote" and self.rate["quote"].try_consume():
                self._quote_best(sector, post_hook, candidates, keyword)
            elif action == "like":
                self._spray_likes(candidates)

    def _candidates_for(self, sector, allow_whales=False):
        trend_kw = (random.choice(self.store.trends[sector])
                    if sector in self.store.trends and self.store.trends[sector] else None)
        if trend_kw:
            keyword = trend_kw
            try:
                posts = self.net.search_posts(keyword)
                self.breaker.record_success()
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] market scan failed: {e}")
                self.breaker.record_failure()
                posts = []
        else:
            keyword = self.sector_activity.get(sector, {}).get("keyword", sector)
            posts = self.sector_posts.get(sector, [])
        cands = []
        for c in posts:
            did = getattr(c.author, "did", None)
            if did == self.net.did:
                continue
            if not allow_whales and did in self.known_follows:
                continue
            if not allow_whales and self.store.already_acted_on(did):
                continue
            if self._is_bot(c.author):
                continue
            if not self._is_relevant_content(c):
                continue
            
            # High-Volume Filter: Only operate in areas with existing traction
            eng = (getattr(c, "like_count", 0) or 0) + (getattr(c, "repost_count", 0) or 0) + (getattr(c, "reply_count", 0) or 0)
            if eng < 3:
                continue
                
            cands.append(c)
            
        # Sort so the absolute highest volume posts are acted on first
        cands.sort(key=lambda c: (getattr(c, "like_count", 0) or 0) + (getattr(c, "repost_count", 0) or 0) + (getattr(c, "reply_count", 0) or 0), reverse=True)
        return cands, keyword

    def _is_relevant_content(self, post) -> bool:
        text = ""
        if hasattr(post, "record") and hasattr(post.record, "text"):
            text = post.record.text or ""
        handle = getattr(post.author, "handle", "") if hasattr(post, "author") else ""
        display = getattr(post.author, "display_name", "") if hasattr(post, "author") else ""
        return is_relevant_text(f"{text} {handle} {display}")

    # ---- like ----
    def _spray_likes(self, candidates):
        liked = 0
        for c in candidates:
            if liked >= MAX_LIKES_PER_TICK or not self.rate["like"].try_consume():
                break
            uri, cid = getattr(c, "uri", None), getattr(c, "cid", None)
            if not uri or not cid or self.store.already_acted_on(f"like:{uri}"):
                continue
            try:
                self.net.like(uri, cid)
                self.store.mark_seen(f"like:{uri}")
                self.breaker.record_success()
                liked += 1
                self._mark_action("like", uri=uri)
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] like failed: {e}")
                self.breaker.record_failure()
                break
        if liked:
            logger.info(f"   [LIKE] liked {liked} relevant post(s)")

    # ---- quote (replaces the misattributed plain repost) ----
    def _quote_best(self, sector, hook, candidates, keyword=None):
        best, best_eng = None, -1
        for c in candidates[:5]:
            uri, cid = getattr(c, "uri", None), getattr(c, "cid", None)
            if not uri or not cid or self.store.already_acted_on(f"quote:{uri}"):
                continue
            eng = ((getattr(c, "like_count", 0) or 0) + (getattr(c, "repost_count", 0) or 0)
                   + (getattr(c, "reply_count", 0) or 0))
            if eng > best_eng:
                best_eng, best = eng, c
        if not best:
            return
        src = (best.record.text or "")[:200]
        constraint = (
            "If the hook is 'amplify_and_praise', you must act as an enthusiastic curator. "
            "Highlight a specific strength of the quoted post (e.g., typography, layout). "
            "You are strictly forbidden from adding any unsolicited critiques or technical friction. "
        ) if hook == "amplify_and_praise" else ""

        # Vision pipeline: extract image from the target post
        image_b64 = self.net.get_post_image_b64(best)
        vision_hint = (
            "An image is attached to this post. Analyze its structural design "
            "(e.g., layout, typography, code architecture, algorithmic patterns) "
            "and synthesize that into your response. Do not explicitly say "
            "'In this image', just integrate the analysis naturally. "
        ) if image_b64 else ""

        prompt = (
            f"This post is about '{sector}':\n\"{src}\"\n\n"
            f"Write one short comment (max 200 chars) to quote-post it, adding a "
            f"genuinely useful plain-language insight that builds on it. Use a "
            f"'{hook}' angle. {POST_HOOK_GUIDANCE.get(hook,'')} Never pitch anything. "
            f"{constraint}"
            f"{vision_hint}"
            f'Respond strictly as JSON: {{"comment": "..."}}'
        )
        raw = self._generate(prompt, dedup=True, image_b64=image_b64)
        quote_text = None
        if raw:
            try:
                quote_text = json.loads(raw)["comment"]
            except Exception:
                quote_text = None
        handle = getattr(best.author, "handle", "unknown")
        if quote_text and self._passes_gates(quote_text):
            try:
                intent_id, uri = self._publish_with_reconcile(
                    kind="quote", text=quote_text, sector=sector, hook=hook,
                    write_fn=lambda t: self.net.quote_post(t, best.uri, best.cid),
                    target_handle=handle, keyword=keyword
                )
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] quote failed: {e}")
                self.breaker.record_failure()
                return
            self.breaker.record_success()
            self.store.mark_seen(f"quote:{best.uri}")
            self.store.mark_seen(content_hash(quote_text))
            self.store.log_action("quote", sector, hook, uri=uri,
                                  target_handle=handle, text=quote_text, learnable=True, keyword=keyword)
            self.store.remove_pending(intent_id)
            self._mark_action("quote", target_handle=handle, uri=uri)
            logger.info(f"   [QUOTE] @{handle}: {quote_text[:70]}...")
            return
        # Fallback: plain repost as goodwill, NOT learnable (no attributable reward).
        try:
            self.net.repost(best.uri, best.cid)
            self.breaker.record_success()
            self.store.mark_seen(f"quote:{best.uri}")
            self.store.log_action("repost", sector, "n/a", uri=best.uri,
                                  target_handle=handle, learnable=False)
            self._mark_action("repost", target_handle=handle, uri=best.uri)
            logger.info(f"   [REPOST] reshared @{handle} (goodwill, non-learnable)")
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] repost failed: {e}")
            self.breaker.record_failure()

    # ---- follow ----
    def _verify_profile_quality(self, profile):
        bio = getattr(profile, "description", "") or ""
        handle = getattr(profile, "handle", "") or ""
        display = getattr(profile, "display_name", "") or ""
        
        prompt = (
            f"You are an autonomous network analyst evaluating a user profile for a strategic follow.\n"
            f"Our persona is:\n{config.PERSONA}\n\n"
            f"Target Profile Bio: {bio}\n"
            f"Target Name: {display} (@{handle})\n\n"
            f"Does this profile represent a highly credible, intellectual, or relevant practitioner "
            f"(e.g., engineer, researcher, scientist, designer) that aligns with our persona? "
            f"Reject generic tech influencers, crypto farmers, and random personal accounts.\n"
            f"Respond strictly as JSON:\n"
            f'{{"is_high_quality": true, "reason": "brief explanation"}}'
        )
        raw = self._generate(prompt, dedup=False)
        if raw:
            try:
                result = json.loads(raw)
                logger.info(f"      [VERIFY] @{handle} - {result.get('is_high_quality')}: {result.get('reason')}")
                return result.get("is_high_quality", False)
            except Exception as e:
                logger.warning(f"      [FAULT] verification failed: {e}")
        return False

    def _strategic_follow(self, sector, hook, candidates, keyword=None):
        logger.info("   [FOLLOW] scoring candidates for follow-back likelihood")
        scored = []
        for c in candidates[:5]:
            did = getattr(c.author, "did", None)
            if not did:
                continue
            profile = self.net.get_profile(did)
            if profile is None:
                continue
            score, reason = self._score_follow_target(profile)
            handle = getattr(c.author, "handle", did)
            logger.info(f"      @{handle}: {score:.2f} ({reason})")
            if score > 0:
                scored.append((score, c, handle))
        if not scored:
            logger.info("   [FOLLOW] nothing scored above threshold.")
            return
        scored.sort(key=lambda x: x[0], reverse=True)
        
        best_score, target, handle = None, None, None
        for score, cand, cand_handle in scored:
            profile = self.net.get_profile(cand.author.did)
            if self._verify_profile_quality(profile):
                best_score, target, handle = score, cand, cand_handle
                break
            else:
                logger.info(f"   [FOLLOW] LLM rejected @{cand_handle} as low quality. Trying next...")
                
        if not target:
            logger.info("   [FOLLOW] all candidates rejected by LLM.")
            return
            
        try:
            self.net.follow(target.author.did)
            self.breaker.record_success()
            self.store.mark_seen(target.author.did)
            self.known_follows.add(target.author.did)
            self.store.log_action("follow", sector, hook,
                                  target_did=target.author.did, target_handle=handle, keyword=keyword)
            self._mark_action("follow", target_handle=handle,
                              target_did=target.author.did)
            logger.info(f"   [FOLLOW] @{handle} (score={best_score:.2f}, awaiting follow-back)")
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] follow failed: {e}")
            self.breaker.record_failure()

    def _is_bot(self, profile):
        """Returns True if the profile looks like an automated account or engagement farmer."""
        followers = int(getattr(profile, "followers_count", 0) or 0)
        following = int(getattr(profile, "follows_count", 0) or 0)
        bio = getattr(profile, "description", "") or ""
        handle = getattr(profile, "handle", "") or ""
        display = getattr(profile, "display_name", "") or ""

        # Engagement farming ratio (follows massive numbers, nobody follows back)
        if following > 5000 and followers < 500:
            return True

        bot_markers = [
            "brid.gy", ".ap.", "activitypub", "awakari", "job-alert", "-bot", "trending", "job-",
            "bot", "rss", "automated", "feed", "aggregator"
        ]
        farming_markers = ["giveaway", "airdrop", "crypto", "web3", "nft", "follow back", "follow-back"]
        
        handle_lower = handle.lower()
        bio_lower = bio.lower()
        display_lower = display.lower()
        
        text_to_check = f"{handle_lower} {bio_lower} {display_lower}"
        
        if any(m in text_to_check for m in bot_markers + farming_markers):
            return True
            
        return False

    def _score_follow_target(self, profile):
        # Future externalization candidate: these thresholds live in code
        # for now because a weak soul could otherwise dial the agent to
        # follow anyone, and that is the easiest way to get rate-limited
        # or banned. Keep code-enforced until the soul-swap story is tested.
        followers = int(getattr(profile, "followers_count", 0) or 0)
        following = int(getattr(profile, "follows_count", 0) or 0)
        posts = int(getattr(profile, "posts_count", 0) or 0)
        bio = getattr(profile, "description", "") or ""
        handle = getattr(profile, "handle", "") or ""
        display = getattr(profile, "display_name", "") or ""

        viewer = getattr(profile, "viewer", None)
        if viewer and getattr(viewer, "followed_by", None):
            return (-1.0, "already follows us")
        if viewer and getattr(viewer, "following", None):
            return (-1.0, "we already follow them")
        if followers > 50000:
            return (-1.0, f"too large ({followers})")
        if posts < 3:
            return (-1.0, f"too few posts ({posts})")

        if self._is_bot(profile):
            return (-1.0, "automated feed/bot or farmer")

        profile_text = f"{bio} {display} {handle}"
        hits = RELEVANCE_RE.findall(profile_text)
        
        score, reasons = 0.0, []
        if not hits:
            reasons.append("contextual match only")
        elif len(hits) >= 3:
            score += 4.0; reasons.append(f"strong match ({len(hits)})")
        elif len(hits) >= 2:
            score += 3.0; reasons.append(f"good match ({len(hits)})")
        else:
            score += 1.5; reasons.append("weak match")

        if following > 0:
            ratio = followers / following
            if ratio < 0.3:
                score += 4.0; reasons.append("high reciprocity")
            elif ratio <= 3.0:
                score += 3.0; reasons.append("reciprocal")
            else:
                score += 0.5; reasons.append("low reciprocity")
        else:
            score += 1.0; reasons.append("new account")

        if 50 <= followers <= 2000:
            score += 2.0; reasons.append("right size")
        elif 10 <= followers < 50:
            score += 1.0; reasons.append("small but real")
        elif followers < 10:
            score += 0.5

        if posts >= 20:
            score += 1.5; reasons.append("active")
        elif posts >= 5:
            score += 0.5
        if len(bio.strip()) > 20:
            score += 1.0; reasons.append("has bio")
        return (score, ", ".join(reasons))

    def _courtesy_follow_back(self):
        """Follow back recent followers we do not yet follow. Aids retention."""
        followers = self.net.recent_followers(limit=25)
        done = 0
        for f in followers:
            did = getattr(f, "did", None)
            if not did or self.store.already_acted_on(f"fb:{did}"):
                continue
            viewer = getattr(f, "viewer", None)
            if viewer and getattr(viewer, "following", None):
                self.store.mark_seen(f"fb:{did}")
                continue
            if not self.rate["follow"].try_consume():
                break
            try:
                self.net.follow(did)
                self.store.mark_seen(f"fb:{did}")
                self.known_follows.add(did)
                self.breaker.record_success()
                done += 1
                self._mark_action("follow_back", target_did=did)
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] courtesy follow-back failed: {e}")
                self.breaker.record_failure()
                break
        if done:
            logger.info(f"   [RETAIN] followed back {done} new follower(s)")

    # ---- original post (multi-archetype divergent variants) ----
    def _sample_distinct_post_hooks(self, n=3):
        """Thompson-sample up to n DISTINCT post archetypes. Each draw is an
        independent Beta sample; we sort and take the top n distinct arms.
        Returns at most len(POST_HOOKS) archetypes. Used by _original_post
        to force structural diversity: each variant in a single generation
        call gets a different archetype, so the three drafts read as if
        written by three different people about the same idea.

        When niche_insights are available the hottest archetypes get a small
        alpha bump (capped at EXPLORATION_NUDGE_MAX) at sampling time only;
        the bandit state itself is not mutated. The cap is intentionally
        small so the nudge biases exploration without zeroing other arms:
        every archetype must still be sampled with non-trivial probability.
        """
        nudges = analyzer.archetype_nudges(self._insights, EXPLORATION_NUDGE_MAX)
        samples = []
        for v in POST_HOOKS:
            arm = self.store.bandit["post_hook"][v]
            samples.append(
                (random.betavariate(arm["alpha"] + nudges.get(v, 0.0), arm["beta"]),
                 v)
            )
        samples.sort(reverse=True)
        return [v for _, v in samples[:min(n, len(POST_HOOKS))]]

    # Per-slot length and opening-move shuffle. Layered on top of the
    # archetype-specific constraints. The archetype wins when there is a
    # conflict (one_line_provocation cannot also be "longer"), and the
    # prompt says so explicitly.
    _LENGTH_SLOTS = ("very short, well under ~120 chars",
                     "medium, around 150 to 230 chars",
                     "longer, up to ~280 chars")
    _OPENING_SLOTS = ("open with a question",
                      "open with a claim",
                      "open mid-scene or with a concrete detail")

    def _build_variant_prompt(self, sector, archetypes, length_slots, opening_slots,
                              trends_info=""):
        """Construct the divergent-variants prompt. Each slot pairs an
        archetype with a length and an opening move, and the prompt insists
        on three drafts that read as if written by three different people."""
        slots = []
        for i, arch in enumerate(archetypes):
            slots.append(
                f"[{i+1}] archetype = \"{arch}\"\n"
                f"    Archetype rule: {POST_HOOK_GUIDANCE.get(arch, '').strip()}\n"
                f"    Length slot: {length_slots[i]}.\n"
                f"    Opening move slot: {opening_slots[i]}.\n"
                f"    If the archetype rule conflicts with the slot, follow the archetype."
            )
        slots_block = "\n\n".join(slots)
        return (
            f"You are writing THREE short Bluesky posts about '{sector}', each in a "
            f"DIFFERENT format. The three drafts must read as if written by THREE "
            f"DIFFERENT PEOPLE about the same idea, NOT three rewordings of one draft. "
            f"Follow each slot's archetype STRICTLY.\n\n"
            f"{slots_block}\n\n"
            f"{trends_info}"
            f"CRITICAL DIVERSITY: Constantly invent entirely new angles, distinct phrasing, and unexplored ideas. Do not recycle the same vocabulary or structures from typical tech posts.\n"
            f"Constraints that apply to ALL drafts: plain language, no jargon left "
            f"unexplained, no pitch, no link, no emoji, no hashtag, no em dash. Skip "
            f"parenting, body image, mental health, religion, politics, money "
            f"struggles. Explain confusing UX, design, or frontend ideas in plain "
            f"words with everyday analogies.\n\n"
            f"Respond strictly as JSON with exactly three keys per variant. "
            f"The 'content' key must contain the raw text for the post. "
            f"The 'media_type' key MUST be either 'gif' or 'image'. "
            f"The 'media_query' key MUST contain a search query for that media.\n"
            f"CRITICAL RULES FOR MEDIA:\n"
            f"- MAXIMIZE MEDIA USAGE: You MUST attach media to almost every post.\n"
            f"- IF media_type='gif': media_query should be a 1-3 word human emotion (e.g., 'frustrated', 'mind blown').\n"
            f"- IF media_type='image': Make an educated guess on the best visual to complement the post. The media_query MUST be highly concrete (e.g. a diagram, mockup, or code structure) and you should append a relevant industry modifier (e.g., 'dribbble', 'architecture diagram', 'figma', 'github layout') to ensure high-quality search results. If discussing an abstract theory, search for a concrete UI application of it.\n"
            f'{{"variants": [{{"content": "...", "media_type": "...", "media_query": "..."}}]}}'
        )

    def _generate_variants(self, sector):
        """Sample distinct archetypes, ask the model for divergent drafts,
        parse them. Returns the raw parsed variant dicts (no gating, no
        ranking). Separated from publication so dry_run_post can reuse it."""
        archetypes = self._sample_distinct_post_hooks(3)
        length_slots = list(self._LENGTH_SLOTS)
        opening_slots = list(self._OPENING_SLOTS)
        random.shuffle(length_slots)
        random.shuffle(opening_slots)
        trends_info = ""
        if sector in self.store.trends and self.store.trends[sector]:
            trends_info = (f"Weave in these trends if they fit: "
                           f"{', '.join(self.store.trends[sector])}.\n\n")
        # Rotating topic angles from the niche analyzer. Picked at random
        # per call so different generation cycles see different angles;
        # this INCREASES variety. The instruction is permissive ("consider
        # one of these") so a draft can still pick a fresh angle entirely.
        angles = analyzer.topic_angles_for_prompt(self._insights, TOPIC_ANGLES_PER_PROMPT)
        if angles:
            trends_info += (
                f"Topic angles currently earning traction in this niche "
                f"(pick at most ONE if it fits, otherwise ignore and choose your "
                f"own angle): {', '.join(angles)}.\n\n"
            )
        prompt = self._build_variant_prompt(sector, archetypes, length_slots,
                                            opening_slots, trends_info)
        raw = self._generate(prompt, dedup=True, enable_tools=True)
        if not raw:
            return archetypes, []
            
        parsed = []
        try:
            data = json.loads(raw)
            if "variants" in data:
                parsed = data["variants"]
            elif "content" in data:
                parsed = [data]
        except Exception as e:
            logger.warning(f"   [GATE] post JSON malformed: {e}. Falling back to text-only.")
            parsed = [{"content": raw, "media_query": ""}]
            
        cleaned = []
        for i, v in enumerate(parsed):
            if not isinstance(v, dict):
                continue
            arch = v.get("archetype") or (archetypes[i] if i < len(archetypes) else None)
            text = v.get("content") or v.get("text") or ""
            media_type = v.get("media_type", "gif")
            media_query = v.get("media_query") or v.get("gifQuery") or ""
            cleaned.append({
                "archetype": arch, "text": text, "thread_parts": [],
                "media_type": media_type,
                "media_query": media_query,
            })
        return archetypes, cleaned

    def _variant_passes_gates(self, variant):
        """A variant is good only if its main text passes the gates AND, for
        a mini_thread, every continuation passes too. If any part fails the
        whole variant is dropped, so we never publish a half-thread."""
        if not self._passes_gates(variant.get("text", "")):
            return False
        for part in variant.get("thread_parts") or []:
            if not self._passes_gates(part):
                return False
        return True

    def _publish_thread_continuations(self, root_uri, root_cid, parts):
        """Best-effort post the rest of a mini_thread. If a continuation
        fails, log and stop. The first part stays published either way; the
        bandit reward is attributed to the first post regardless of whether
        the chain landed fully, so a half-posted thread is not a regression."""
        if not (root_uri and root_cid and parts):
            return
        parent_uri, parent_cid = root_uri, root_cid
        for i, part_text in enumerate(parts, start=1):
            try:
                child_uri = self.net.post_in_thread(
                    part_text, root_uri, root_cid, parent_uri, parent_cid,
                )
                self.breaker.record_success()
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] thread continuation #{i} failed: {e}")
                self.breaker.record_failure()
                return
            child_cid = self.net.get_post_cid(child_uri)
            logger.info(f"   [THREAD] +part {i}: {part_text[:60]}...")
            if not child_cid:
                # Without a cid we cannot chain further; stop early.
                return
            parent_uri, parent_cid = child_uri, child_cid

    def dry_run_post(self, sector):
        """Generate and rank variants for a slot WITHOUT publishing. Returns
        the parsed variants list with archetypes intact so a CLI dry-run can
        print three structurally different drafts side by side. Used by
        run.py --dry-run; no network writes occur."""
        archetypes, variants = self._generate_variants(sector)
        ranked = sorted(
            ((hook_strength(v["text"], v.get("archetype")), v) for v in variants),
            key=lambda x: x[0], reverse=True,
        )
        # Surface gif_query plus a resolved Klipy URL (when KLIPY_APP_KEY is
        # set) so an operator can verify the GIF that WOULD attach. No
        # bytes are fetched here; resolve is cheap and cached, and a dry
        # run should not consume bandwidth previewing an embed.
        variant_rows = []
        for score, v in ranked:
            media_type = v.get("media_type") or "gif"
            media_query = v.get("media_query") or ""
            resolved_url = None
            if media_query:
                if media_type == "image":
                    resolved_url = serper.search_images(media_query)
                else:
                    resolved_url = klipy.resolve(media_query)
                    
            variant_rows.append({
                "archetype": v.get("archetype"),
                "text": v.get("text"),
                "thread_parts": v.get("thread_parts") or [],
                "media_type": media_type,
                "media_query": media_query,
                "resolved_media_url": resolved_url,
                "hook_strength": round(score, 2),
                "passes_gates": self._variant_passes_gates(v),
            })
        return {
            "sector": sector,
            "sampled_archetypes": archetypes,
            "variants": variant_rows,
        }

    def _build_write_fn_with_optional_media(self, media_type, media_query, text_for_log):
        """Return a write_fn closure suitable for _publish_with_reconcile.

        Routes 'image' to SerpAPI and 'gif' to Klipy. Any failure silently degrades
        to a text-only post so publishing is never blocked by media errors.
        """
        def write_fn(text):
            if not media_query:
                return self.net.post(text)
            try:
                if media_type == "image":
                    media_url = serper.search_images(media_query)
                    if not media_url:
                        return self.net.post(text)
                    fetched = serper.fetch_image_bytes(media_url)
                else:
                    media_url = klipy.resolve(media_query)
                    if not media_url:
                        return self.net.post(text)
                    fetched = klipy.fetch_bytes(media_url)
                    
                if not fetched:
                    return self.net.post(text)
                    
                image_bytes, _mime = fetched
                if media_type == "image":
                    uri = self.net.post_with_image(
                        text, image_bytes, alt_text=media_query,
                    )
                else:
                    uri = self.net.post_with_video(
                        text, image_bytes, alt_text=media_query,
                    )
                logger.info(f"   [MEDIA] attached {media_type} '{media_query}' to: "
                            f"{text_for_log[:50]}...")
                return uri
            except Exception as e:
                logger.warning(f"   [MEDIA] {media_type} attachment failed ({e}); "
                               f"publishing text-only.")
                return self.net.post(text)
        return write_fn

    def _original_post(self, sector, keyword=None):
        """Generate three divergent variants (each a distinct archetype),
        filter via gates, publish the best by hook_strength, and record the
        WINNING archetype to the bandit so the bandit learns which shapes
        earn traction for THIS account."""
        archetypes, variants = self._generate_variants(sector)
        if not variants:
            return
        candidates = [v for v in variants if self._variant_passes_gates(v)]
        if not candidates:
            logger.warning("   [GATE] all post variants rejected. Skipping slot.")
            return
        winner = max(candidates,
                     key=lambda v: hook_strength(v["text"], v.get("archetype")))
        text = winner["text"]
        hook = winner.get("archetype") or archetypes[0]
        # Optional media attachment. Always a bonus, never required: any
        # failure in the resolve / fetch / upload path degrades silently to
        # a text-only post. Original posts only; replies stay text-only by
        # design (handled at call sites). For mini_thread, the media rides
        # on the anchor only, not on the continuations.
        write_fn = self._build_write_fn_with_optional_media(
            winner.get("media_type", "gif"), winner.get("media_query"), text,
        )
        try:
            intent_id, uri = self._publish_with_reconcile(
                kind="post", text=text, sector=sector, hook=hook,
                write_fn=write_fn, keyword=keyword
            )
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] post failed: {e}")
            self.breaker.record_failure()
            return
        cid = self.net.get_post_cid(uri)
        self.breaker.record_success()
        self.store.anchor_posts += 1
        self.store.mark_seen(content_hash(text))
        self.store.log_action("post", sector, hook, uri=uri, text=text, learnable=True, keyword=keyword)
        self.store.remove_pending(intent_id)
        self._mark_action("post", uri=uri, sector=sector, hook=hook)
        logger.info(f"   [POST] anchor #{self.store.anchor_posts} "
                    f"hook={hook} (hook_strength="
                    f"{hook_strength(text, hook):.1f}): {text[:70]}...")
        # mini_thread continuations land AFTER the anchor is recorded, so a
        # failure mid-chain never blocks the bookkeeping for the first post.
        if winner.get("thread_parts") and cid:
            self._publish_thread_continuations(uri, cid, winner["thread_parts"])
        if not self.store.pinned and cid:
            self.net.pin_post(uri, cid)
            self.store.pinned = True
        self.store.save_engine()

    # ---- reply ----
    def _helpful_reply(self, sector, hook, candidates, keyword=None):
        limit = min(5, len(candidates))
        batch = ""
        for i, c in enumerate(candidates[:limit]):
            preview = (c.record.text or "")[:200] if getattr(c.record, "text", None) else "(empty)"
            batch += f"[{i}] @{c.author.handle}: {preview}\n\n"

        # Whale constraint: if the target account is large, force yes_and_expansion
        whale_constraint = (
            "If the target account is a large creator, your reply must act as a "
            "'yes_and_expansion'. Add a highly intellectual, synthesized observation "
            "to the top of their thread. You are strictly forbidden from acting "
            "contrarian to the original author. "
        )

        # Vision pipeline: try to extract an image from the top candidate
        top_image_b64 = None
        if candidates:
            top_image_b64 = self.net.get_post_image_b64(candidates[0])
        vision_hint = (
            "An image is attached to this post. Analyze its structural design "
            "(e.g., layout, typography, code architecture, algorithmic patterns) "
            "and synthesize that into your response. Do not explicitly say "
            "'In this image', just integrate the analysis naturally. "
        ) if top_image_b64 else ""

        prompt = (
            f"These are live posts about '{sector}':\n\n{batch}\n"
            f"Pick the SINGLE post where a short, kind, helpful reply would make the "
            f"person feel heard and less stuck. Add real value: a clearer way to think "
            f"about their problem, a small concrete tip, or a good question. Use a "
            f"'{hook}' angle. {REPLY_HOOK_GUIDANCE.get(hook,'')} Explain any technical "
            f"idea in plain words with an everyday analogy. If the post is sensitive "
            f"(parenting, body image, mental health, religion, politics, money "
            f"struggles), set index to -1 and reply to an empty string. Do not pitch "
            f"anything. Do not say 'great post'. Max 280 chars. No emoji, hashtag, em dash. "
            f"CRITICAL DIVERSITY: Never repeat standard tech advice. Provide a unique, highly specific synthesis that the author hasn't heard before.\n"
            f"{whale_constraint}"
            f"{vision_hint}\n"
            f'Respond strictly as JSON: {{"index": int, "reply": "..."}}'
        )
        raw = self._generate(prompt, dedup=True, image_b64=top_image_b64, enable_tools=True)
        if not raw:
            return
        try:
            data = json.loads(raw)
            idx, text = int(data["index"]), data["reply"]
        except Exception as e:
            logger.warning(f"   [GATE] reply JSON malformed: {e}. Skipping.")
            return
        if idx < 0 or idx >= limit or not self._passes_gates(text):
            logger.warning("   [GATE] reply skipped (sensitive/range/quality).")
            return
        target = candidates[idx]
        try:
            if self.rate["like"].try_consume():
                self.net.like(target.uri, target.cid)
            intent_id, uri = self._publish_with_reconcile(
                kind="reply", text=text, sector=sector, hook=hook,
                write_fn=lambda t: self.net.reply(target, t),
                target_did=target.author.did,
                target_handle=target.author.handle,
                keyword=keyword
            )
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] reply failed: {e}")
            self.breaker.record_failure()
            return
        self.breaker.record_success()
        self.store.mark_seen(target.author.did)
        self.store.mark_seen(content_hash(text))
        self.store.log_action("reply", sector, hook, uri=uri,
                              target_did=target.author.did,
                              target_handle=target.author.handle, text=text, learnable=True, keyword=keyword)
        self.store.remove_pending(intent_id)
        self._mark_action("reply", target_handle=target.author.handle, uri=uri)
        logger.info(f"   [REPLY] @{target.author.handle}: {text[:70]}...")

    # ---- generation + gates ----
    def _generate(self, prompt, dedup=False, image_b64=None, enable_tools=False):
        if dedup:
            recent = self.store.recent_content_texts(5)
            if recent:
                prompt += ("\n\nDo NOT repeat the concepts, phrases, or angles of "
                           "these recent posts:\n" + "\n".join(f"- {t}" for t in recent))

        if image_b64:
            model = "llama-3.1-8b-instant"
            user_content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{image_b64}"
                }},
            ]
        else:
            model = "llama-3.1-8b-instant"
            user_content = prompt

        messages = [
            {"role": "system", "content": self.persona},
            {"role": "user", "content": user_content}
        ]

        tools = []
        if enable_tools:
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "search_news",
                        "description": "Searches Google News for the latest headlines and snippets on a technical topic. Use to find real-world updates before writing.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "The topic to search for (e.g. 'React 19 updates')"}
                            },
                            "required": ["query"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "search_images",
                        "description": "Searches Google Images for diagrams, mockups, or technical visuals.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "The image to search for"}
                            },
                            "required": ["query"]
                        }
                    }
                }
            ]

        import serper
        
        for turn in range(3):
            try:
                kwargs = {
                    "model": model,
                    "messages": messages,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                else:
                    kwargs["response_format"] = {"type": "json_object"}

                resp = self.ai.chat.completions.create(**kwargs)
                msg = resp.choices[0].message
                
                if getattr(msg, "tool_calls", None):
                    messages.append(msg)
                    for tc in msg.tool_calls:
                        func_name = tc.function.name
                        try:
                            args = json.loads(tc.function.arguments)
                        except:
                            args = {}
                            
                        logger.info(f"   [TOOL] LLM autonomously called {func_name}({args})")
                        res = "No results."
                        if func_name == "search_news":
                            res = serper.search_news(args.get("query", "")) or "No results."
                        elif func_name == "search_images":
                            res = serper.search_images(args.get("query", "")) or "No results."
                            
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": func_name,
                            "content": res
                        })
                else:
                    ans = msg.content.strip()
                    if ans.startswith("```json"):
                        ans = ans[7:].strip()
                    elif ans.startswith("```"):
                        ans = ans[3:].strip()
                    if ans.endswith("```"):
                        ans = ans[:-3].strip()
                    return ans
            except Exception as e:
                logger.warning(f"   [FAULT] generation failed ({model}) turn {turn}: {e}")
                return "{}"
        return "{}"

    def _passes_gates(self, text) -> bool:
        if not text or not text.strip() or len(text) > 300:
            return False
        if self.store.already_acted_on(content_hash(text)):
            return False
        low = text.lower()
        if any(p in low for p in SPAM_PHRASES):
            return False
        if any(p in low for p in SENSITIVE_PHRASES):
            return False
        # word-boundary check for short risky tokens (avoids domain-term collisions)
        if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in SENSITIVE_WORDS):
            return False
        if any(ch in text for ch in ("\U0001F300", "\u2014")):  # emoji / em dash
            return False
        return True

    # ---- reporting ----
    def report(self):
        followers = self.store.snapshots[-1]["followers"] if self.store.snapshots else 0
        phase_name, _ = self._current_phase()
        logger.info(f"\n[REPORT] followers={followers}/{FOLLOWER_TARGET} "
                    f"anchor_posts={self.store.anchor_posts} phase={phase_name}")
        for dim, vals in self.store.bandit.items():
            for v, arm in vals.items():
                a, b = arm["alpha"], arm["beta"]
                trials = (a - 1) + (b - 1)
                eng = arm.get("engagement", 0.0)
                wlb = wilson_lower_bound(eng, trials) if trials > 0 else 0.0
                mean_eng = (eng / trials) if trials > 0 else 0.0
                logger.info(f"   {dim}/{v}: Beta({a:.1f},{b:.1f}) MeanEng={mean_eng:.2f} "
                            f"Wilson_lb={wlb:.3f} n={trials:.0f} TotalEng={eng:.0f}")
