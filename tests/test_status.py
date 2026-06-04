"""Tests for item 3: heartbeat status.json.

write_status must emit the operator-visible fields (timestamp, tick, follower
count, last action, breaker state, consecutive empty ticks) atomically. Calling
_mark_action must update _last_action so successive heartbeats reflect what
the engine just did.
"""
import os
import sys
import json
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

import config
from store import Store
from governance import RateBudget, CircuitBreaker
from engine import FollowerEngine, write_status


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="kf-status-"))
        self.patches = []
        for name in ("BANDIT_STATE_FILE", "ACTION_LEDGER_FILE", "SNAPSHOT_FILE",
                     "SEEN_FILE", "ENGINE_STATE_FILE", "CIRCUIT_BREAKER_FILE",
                     "KILL_SWITCH_FILE", "PENDING_WRITES_FILE", "STATUS_FILE"):
            p = mock.patch.object(config, name, self.tmpdir / f"{name.lower()}")
            p.start()
            self.patches.append(p)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _engine(self):
        e = FollowerEngine.__new__(FollowerEngine)
        e.store = Store()
        e.net = mock.MagicMock()
        e.ai = mock.MagicMock()
        e.breaker = CircuitBreaker()
        e.rate = {k: RateBudget(v["capacity"], v["refill_per_sec"])
                  for k, v in config.RATE_BUDGETS.items()}
        e.sector_activity = {}
        e.sector_posts = {}
        e.persona = ""
        e._tick_actions = 0
        e._last_action = None
        return e

    def test_status_has_required_fields(self):
        e = self._engine()
        e.store.snapshots.append({"ts": 0, "tick": 1, "followers": 7, "delta": 0})
        write_status(e)
        with open(config.STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for field in ("ts", "tick", "followers", "last_action",
                      "breaker_state", "consecutive_empty_ticks",
                      "pending_writes", "phase"):
            self.assertIn(field, data, f"status.json missing {field}")
        self.assertEqual(data["followers"], 7)
        self.assertEqual(data["breaker_state"], "CLOSED")
        self.assertIsNone(data["last_action"])

    def test_mark_action_updates_last_action(self):
        e = self._engine()
        e._mark_action("follow", target_handle="alice.bsky.social",
                       target_did="did:plc:alice")
        self.assertEqual(e._tick_actions, 1)
        self.assertEqual(e._last_action["kind"], "follow")
        self.assertEqual(e._last_action["target_handle"], "alice.bsky.social")

        write_status(e)
        with open(config.STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["last_action"]["kind"], "follow")
        self.assertEqual(data["last_action"]["target_handle"], "alice.bsky.social")

    def test_status_reflects_breaker_state_change(self):
        e = self._engine()
        e.breaker.trip_open(reason="test")
        write_status(e)
        with open(config.STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["breaker_state"], "OPEN")

    def test_status_write_is_atomic_via_helper(self):
        """Sanity: the file exists with valid JSON after a write, with no
        stray *.tmp left behind (atomic_write_json handles the rename)."""
        e = self._engine()
        write_status(e)
        self.assertTrue(config.STATUS_FILE.exists())
        json.loads(config.STATUS_FILE.read_text(encoding="utf-8"))
        stray = list(self.tmpdir.glob("*.tmp"))
        self.assertEqual(stray, [], f"atomic write left stragglers: {stray}")


if __name__ == "__main__":
    unittest.main()
