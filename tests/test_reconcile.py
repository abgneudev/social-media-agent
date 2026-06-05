"""Tests for item 1: idempotent publish with reconcile-before-retry.

A successful publish followed by a process crash before the ledger entry must
not produce a duplicate post on the next attempt. The pending intent is
persisted before the network call, and on the next tick or process start
_reconcile_pending locates the post in our author feed by content_hash and
finalizes the bookkeeping instead of retrying the write.
"""
import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

from atproto import exceptions

from core import config
from core.store import Store
from core.governance import RateBudget, CircuitBreaker
from core.engine import FollowerEngine


class FakeRecord:
    def __init__(self, text):
        self.text = text
        self.reply = None


class FakePost:
    def __init__(self, uri, cid, text):
        self.uri = uri
        self.cid = cid
        self.record = FakeRecord(text)


class FakeFeedItem:
    def __init__(self, post):
        self.post = post


class FakeFeedResp:
    def __init__(self, posts):
        self.feed = [FakeFeedItem(p) for p in posts]


class MockAdapter:
    """Mock BlueskyAdapter mimicking just the surface _publish_with_reconcile
    and _reconcile_pending need. Tracks calls so the test can assert no
    duplicate publish happens."""
    def __init__(self):
        self.did = "did:plc:testaccount"
        self.handle = "test.bsky.social"
        self.published_posts = []   # FakePost instances, newest first
        self.post_call_count = 0
        self.find_post_calls = 0

    def post(self, text):
        self.post_call_count += 1
        uri = f"at://did:plc:testaccount/app.bsky.feed.post/{self.post_call_count}"
        cid = f"cid-{self.post_call_count}"
        self.published_posts.insert(0, FakePost(uri, cid, text))
        return uri

    def find_post(self, target_hash, limit=50):
        self.find_post_calls += 1
        for p in self.published_posts[:limit]:
            if config.content_hash(p.record.text) == target_hash:
                return (p.uri, p.cid)
        return None


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="kf-test-"))
        self.patches = []
        for name in ("BANDIT_STATE_FILE", "ACTION_LEDGER_FILE", "SNAPSHOT_FILE",
                     "SEEN_FILE", "ENGINE_STATE_FILE", "CIRCUIT_BREAKER_FILE",
                     "KILL_SWITCH_FILE", "PENDING_WRITES_FILE"):
            p = mock.patch.object(config, name, self.tmpdir / f"{name.lower()}")
            p.start()
            self.patches.append(p)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_engine(self, adapter):
        # Skip live login by constructing without __init__.
        e = FollowerEngine.__new__(FollowerEngine)
        e.store = Store()
        e.net = adapter
        e.ai = mock.MagicMock()
        e.breaker = CircuitBreaker()
        e.rate = {k: RateBudget(v["capacity"], v["refill_per_sec"])
                  for k, v in config.RATE_BUDGETS.items()}
        e.sector_activity = {}
        e.sector_posts = {}
        e.persona = ""
        return e

    def test_publish_then_crash_before_recording_does_not_double_post(self):
        """The acceptance test: write succeeds, the process is killed before
        log_action runs, the next process reconciles and does not retry."""
        adapter = MockAdapter()
        engine = self._make_engine(adapter)
        text = "How browser layout actually decides where a button ends up on the screen."

        # First "process": publish succeeds and returns. We then simulate a
        # crash by NOT calling the post-write bookkeeping (no log_action,
        # no mark_seen, no remove_pending).
        intent_id, uri = engine._publish_with_reconcile(
            kind="post", text=text, sector="frontend_engineering",
            hook="storytelling", write_fn=adapter.post,
        )
        self.assertEqual(adapter.post_call_count, 1)
        self.assertEqual(len(engine.store.list_pending()), 1)
        self.assertEqual(engine.store.ledger, [])

        # Second "process": fresh Store loads the pending entry from disk.
        engine2 = self._make_engine(adapter)
        self.assertEqual(len(engine2.store.list_pending()), 1)
        engine2._reconcile_pending()

        # Reconcile must (a) NOT call net.post a second time, (b) write a
        # ledger entry matching the recovered URI, (c) clear the pending
        # intent, (d) mark the content_hash as seen so the dedup gate blocks
        # any future generation of the same text.
        self.assertEqual(adapter.post_call_count, 1, "must not double-post")
        self.assertEqual(len(engine2.store.ledger), 1)
        self.assertEqual(engine2.store.ledger[0]["uri"], uri)
        self.assertEqual(engine2.store.ledger[0]["kind"], "post")
        self.assertEqual(engine2.store.list_pending(), [])
        self.assertIn(config.content_hash(text), engine2.store.seen)

    def test_write_raises_but_post_landed_recovers_inline(self):
        """The network drops between server commit and the SDK reading the
        response: the write call raises, but find_post sees the record and
        we recover the URI without retrying."""
        adapter = MockAdapter()
        engine = self._make_engine(adapter)
        text = "Accessibility quietly decides which buttons users actually find."

        def raising_after_commit(t):
            adapter.post(t)   # server commits, record lands in feed
            raise exceptions.AtProtocolError("response read timed out")

        intent_id, uri = engine._publish_with_reconcile(
            kind="post", text=text, sector="design_systems",
            hook="unpopular_opinion", write_fn=raising_after_commit,
        )
        self.assertEqual(adapter.post_call_count, 1)
        self.assertEqual(adapter.find_post_calls, 1)
        self.assertTrue(uri.startswith("at://"))

    def test_write_raises_and_nothing_landed_leaves_pending_and_reraises(self):
        """If the write raised and find_post finds nothing, leave the intent
        in place (so a later reconcile can pick it up if it landed late) and
        re-raise so the caller records a real failure."""
        adapter = MockAdapter()
        engine = self._make_engine(adapter)
        text = "Why design tokens matter more than most teams admit."

        def fully_failing(t):
            raise exceptions.AtProtocolError("network unreachable")

        with self.assertRaises(exceptions.AtProtocolError):
            engine._publish_with_reconcile(
                kind="post", text=text, sector="ux_design",
                hook="viral_listicle", write_fn=fully_failing,
            )
        self.assertEqual(adapter.post_call_count, 0)
        self.assertEqual(len(engine.store.list_pending()), 1)
        self.assertEqual(engine.store.ledger, [])

    def test_reconcile_does_not_duplicate_existing_ledger_entry(self):
        """If a ledger entry already exists for the recovered URI (e.g., the
        crash happened AFTER log_action ran but BEFORE remove_pending),
        reconcile must clear the intent without appending a duplicate row."""
        adapter = MockAdapter()
        engine = self._make_engine(adapter)
        text = "Component libraries fail when the contract is unclear."

        intent_id, uri = engine._publish_with_reconcile(
            kind="post", text=text, sector="design_systems",
            hook="storytelling", write_fn=adapter.post,
        )
        # Simulate: log_action ran, but remove_pending did not.
        engine.store.log_action("post", "design_systems", "storytelling",
                                uri=uri, text=text, learnable=True)
        self.assertEqual(len(engine.store.ledger), 1)
        self.assertEqual(len(engine.store.list_pending()), 1)

        engine._reconcile_pending()
        self.assertEqual(len(engine.store.ledger), 1, "no duplicate ledger row")
        self.assertEqual(engine.store.list_pending(), [])


if __name__ == "__main__":
    unittest.main()
