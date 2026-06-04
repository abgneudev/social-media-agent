"""Tests for Commit 2: realistic attribution window and fractional reward.

Acceptance properties:

1. mature_actions is wall-clock based: an action logged now does NOT mature
   while the elapsed time is below CONTENT_ATTRIBUTION_SECONDS, and DOES
   mature once the window has passed. This must hold across simulated
   restarts (the ledger is persisted; on reload, an old action still ages
   the same way).

2. POST_DECAY does not erase the reward signal before maturation. With the
   recalibrated decay over the new (~24h) window, an arm that received a
   strong reward at t0 still has a clearly elevated posterior at t = window.

3. Store.update accepts a fractional reward, clamped to [0, 1]. alpha grows
   by reward, beta grows by (1 - reward). Backwards compatible with the
   bool path.

4. The report still surfaces per-archetype posteriors (E and Wilson lower
   bound), so an operator can SEE which shapes are winning.
"""
import os
import sys
import time
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

import config
from config import (
    CONTENT_ATTRIBUTION_SECONDS, FOLLOW_ATTRIBUTION_SECONDS,
    POST_DECAY, POST_HOOKS, TRACTION_REWARD_CAP,
)
from store import Store
from governance import RateBudget, CircuitBreaker
from engine import FollowerEngine


class _IsolatedStateTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="kf-attribution-"))
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


class AttributionWindowTest(_IsolatedStateTest):
    def test_content_window_is_realistic(self):
        """If CONTENT_ATTRIBUTION_SECONDS slips back to a few minutes the
        bandit will mature everything as a failure again. Guard against
        that regression: the window must be at least an hour."""
        self.assertGreaterEqual(CONTENT_ATTRIBUTION_SECONDS, 60 * 60,
                                "content attribution window collapsed to <1h again")

    def test_action_does_not_mature_before_window(self):
        s = Store()
        s.log_action("post", "ux_design", "teardown",
                     uri="at://example/1", text="hello")
        # Patch the wall clock inside store.mature_actions to be 1s ago,
        # well under the window: nothing matures.
        future = time.time() + CONTENT_ATTRIBUTION_SECONDS - 60
        with mock.patch("store.time.time", return_value=future):
            self.assertEqual(s.mature_actions(), [])

    def test_action_matures_after_window_across_restart(self):
        """An action logged now must mature when wall time has advanced past
        the window, even if the process restarted (so the in-memory tick
        counter reset). Persistence is in the ledger timestamp, not in any
        per-tick state."""
        s1 = Store()
        s1.log_action("post", "ux_design", "teardown",
                      uri="at://example/2", text="hello again")

        # Simulate a process restart: drop s1, build s2 from the same files.
        del s1
        s2 = Store()
        self.assertEqual(len(s2.ledger), 1)

        # Far in the future, well past the window.
        far_future = time.time() + CONTENT_ATTRIBUTION_SECONDS * 7
        with mock.patch("store.time.time", return_value=far_future):
            due = s2.mature_actions()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["uri"], "at://example/2")

    def test_follow_window_separate_from_content(self):
        """Different kinds must use different windows. A follow logged just
        after content should still mature on its own (shorter) schedule."""
        s = Store()
        s.log_action("follow", "ux_design", "teardown",
                     target_did="did:plc:alice", target_handle="alice")
        with mock.patch("store.time.time",
                        return_value=time.time() + FOLLOW_ATTRIBUTION_SECONDS + 1):
            due = s.mature_actions()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["kind"], "follow")


class DecayDoesNotEraseSignalTest(_IsolatedStateTest):
    def test_decay_over_one_window_retains_most_of_signal(self):
        """With TICK_INTERVAL=150s the 24h window is ~576 ticks. Compounded
        POST_DECAY over that many ticks must keep the posterior
        recognizably above its Beta(1,1) prior, so a reward earned at t0
        still meaningfully informs the bandit when the post matures."""
        s = Store()
        arm_name = POST_HOOKS[0]
        s.bandit["post_hook"][arm_name]["alpha"] = 6.0   # a strong win
        s.bandit["post_hook"][arm_name]["beta"] = 1.0
        s.save_bandit()
        ticks_per_window = CONTENT_ATTRIBUTION_SECONDS // config.TICK_INTERVAL
        for _ in range(int(ticks_per_window)):
            s.decay()
        alpha = s.bandit["post_hook"][arm_name]["alpha"]
        # alpha started at 6 (5 above the Beta(1,1) prior). After decay the
        # residual above 1 should be at least half of the original delta.
        residual = alpha - 1.0
        self.assertGreater(
            residual, 2.5,
            f"decay too aggressive: alpha collapsed to {alpha:.3f} "
            f"(residual above prior = {residual:.3f})",
        )

    def test_decay_factor_chosen_to_match_window(self):
        """Sanity: a 5% loss per window is the rough design target. Confirm
        the chosen factor stays in a reasonable band so future tweaks to
        either the window or the decay get caught."""
        ticks_per_window = CONTENT_ATTRIBUTION_SECONDS // config.TICK_INTERVAL
        retention = POST_DECAY ** ticks_per_window
        self.assertGreater(retention, 0.5,
                           f"only {retention:.3f} retention over one window is too low")
        self.assertLess(retention, 0.99,
                        f"retention {retention:.3f} per window leaves no decay at all")


class FractionalRewardTest(_IsolatedStateTest):
    def test_fractional_reward_splits_between_alpha_and_beta(self):
        s = Store()
        arm = POST_HOOKS[0]
        a0 = s.bandit["post_hook"][arm]["alpha"]
        b0 = s.bandit["post_hook"][arm]["beta"]
        s.update("post_hook", arm, 0.3)
        a1 = s.bandit["post_hook"][arm]["alpha"]
        b1 = s.bandit["post_hook"][arm]["beta"]
        self.assertAlmostEqual(a1 - a0, 0.3, places=6)
        self.assertAlmostEqual(b1 - b0, 0.7, places=6)

    def test_reward_clamped_to_unit_interval(self):
        s = Store()
        arm = POST_HOOKS[0]
        a0 = s.bandit["post_hook"][arm]["alpha"]
        b0 = s.bandit["post_hook"][arm]["beta"]
        s.update("post_hook", arm, 5.0)   # over-cap -> 1.0
        self.assertAlmostEqual(s.bandit["post_hook"][arm]["alpha"] - a0, 1.0, places=6)
        self.assertAlmostEqual(s.bandit["post_hook"][arm]["beta"] - b0, 0.0, places=6)
        s.update("post_hook", arm, -2.0)   # negative -> 0.0
        # beta should have gained exactly 1.0 from the negative update.
        self.assertAlmostEqual(s.bandit["post_hook"][arm]["beta"] - b0, 1.0, places=6)

    def test_bool_path_still_works(self):
        s = Store()
        arm = POST_HOOKS[0]
        a0 = s.bandit["post_hook"][arm]["alpha"]
        b0 = s.bandit["post_hook"][arm]["beta"]
        s.update("post_hook", arm, True)
        self.assertAlmostEqual(s.bandit["post_hook"][arm]["alpha"] - a0, 1.0, places=6)
        s.update("post_hook", arm, False)
        self.assertAlmostEqual(s.bandit["post_hook"][arm]["beta"] - b0, 1.0, places=6)

    def test_normalization_cap_constant_present(self):
        # If someone removes the cap constant without thinking, the learn
        # path falls back to whatever default and reward math breaks.
        self.assertGreater(TRACTION_REWARD_CAP, 0)


class ReportSurfacesPerArchetypeTest(_IsolatedStateTest):
    def test_report_logs_every_post_hook_arm(self):
        """report() iterates the bandit and logs Beta + Wilson per arm. An
        operator should be able to see which archetypes are winning."""
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
        # Capture log output via a small handler. The kiloforge logger has
        # propagate=False and may default to WARNING; force INFO so report's
        # per-arm lines reach the handler.
        records = []
        handler = mock.MagicMock()
        handler.handle = lambda r: records.append(r.getMessage())
        handler.level = 0
        import logging
        prev_level = config.logger.level
        config.logger.setLevel(logging.INFO)
        config.logger.addHandler(handler)
        try:
            e.report()
        finally:
            config.logger.removeHandler(handler)
            config.logger.setLevel(prev_level)
        joined = "\n".join(records)
        for arm in POST_HOOKS:
            self.assertIn(arm, joined,
                          f"report did not surface posterior for archetype {arm!r}")


if __name__ == "__main__":
    unittest.main()
