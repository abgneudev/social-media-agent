"""Tests for item 2: stall detector trips the breaker.

An active tick that completed zero successful network actions should bump the
empty-tick counter. Once STALL_THRESHOLD consecutive empty ticks have passed,
the breaker is forced open so the daemon stops chewing cycles silently.
Halted ticks and ticks where the breaker is already open are skipped: the
counter only accounts for ticks the engine genuinely tried to work in.
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

import config
from store import Store
from governance import RateBudget, CircuitBreaker
from engine import FollowerEngine


class StallTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="kf-stall-"))
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
        return e

    def test_consecutive_empty_active_ticks_trip_breaker(self):
        e = self._engine()
        for i in range(config.STALL_THRESHOLD - 1):
            e._tick_active = True
            e._tick_actions = 0
            e.update_stall_counter()
            self.assertFalse(e.breaker.is_open(),
                             f"breaker tripped too early at tick {i + 1}")
            self.assertEqual(e.store.consecutive_empty_ticks, i + 1)

        # One more empty active tick reaches the threshold.
        e._tick_active = True
        e._tick_actions = 0
        e.update_stall_counter()
        self.assertEqual(e.store.consecutive_empty_ticks, config.STALL_THRESHOLD)
        self.assertTrue(e.breaker.is_open(),
                        "breaker must be OPEN after STALL_THRESHOLD empty ticks")

    def test_one_action_resets_the_counter(self):
        e = self._engine()
        # Build up some empty ticks just shy of the threshold.
        for _ in range(config.STALL_THRESHOLD - 1):
            e._tick_active = True
            e._tick_actions = 0
            e.update_stall_counter()
        self.assertEqual(e.store.consecutive_empty_ticks, config.STALL_THRESHOLD - 1)
        self.assertFalse(e.breaker.is_open())

        # A productive tick clears the counter.
        e._tick_active = True
        e._tick_actions = 1
        e.update_stall_counter()
        self.assertEqual(e.store.consecutive_empty_ticks, 0)
        self.assertFalse(e.breaker.is_open())

    def test_inactive_ticks_do_not_count(self):
        """Halted or breaker-open ticks must not bump the empty counter:
        the daemon is intentionally idle, not stalled."""
        e = self._engine()
        for _ in range(config.STALL_THRESHOLD + 5):
            e._tick_active = False
            e._tick_actions = 0
            e.update_stall_counter()
        self.assertEqual(e.store.consecutive_empty_ticks, 0)
        self.assertFalse(e.breaker.is_open())

    def test_counter_persists_across_store_reload(self):
        """Engine state survives a process restart so the stall detector
        cannot be reset just by crashing."""
        e1 = self._engine()
        for _ in range(3):
            e1._tick_active = True
            e1._tick_actions = 0
            e1.update_stall_counter()
        self.assertEqual(e1.store.consecutive_empty_ticks, 3)

        # Fresh engine reads the same state file.
        e2 = self._engine()
        self.assertEqual(e2.store.consecutive_empty_ticks, 3)


if __name__ == "__main__":
    unittest.main()
