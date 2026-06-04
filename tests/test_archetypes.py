"""Tests for Commit 1: archetypes and divergent variants.

Acceptance properties:

1. A single _original_post generation must produce variants of DISTINCT
   archetypes. The whole point of replacing the three weak hooks with eight
   structurally different archetypes is to force structural diversity per
   generation call; if the variants collapse onto one archetype, the bandit
   has nothing to compare.

2. All new archetypes still pass the existing safety / quality / dedup
   gates. Persona hard rules (no em dash, no emoji, no link, no banned
   words, length cap) hold regardless of archetype.

3. The bandit migration preserves surviving arms' posteriors and seeds new
   arms at Beta(1,1). Old arms that are no longer in POST_HOOKS are dropped
   (they would never be sampled again but would clutter reports).

4. hook_strength is archetype-aware: a 100-char one_line_provocation should
   score well; a 100-char default-archetype text should not get the same
   length bonus.
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
from config import POST_HOOKS
from store import Store, atomic_write_json
from governance import RateBudget, CircuitBreaker
from engine import FollowerEngine, hook_strength


# A clean, safe exemplar for each archetype. Used to confirm the gates do
# not reject any new archetype shape by accident.
ARCHETYPE_EXEMPLARS = {
    "one_line_provocation":
        "Most design systems quietly fail because nobody owns the contract.",
    "mini_thread":
        "Layout debugging gets easier the moment you stop asking 'why is it broken' "
        "and start asking 'what told this element where to go'.",
    "i_was_wrong":
        "I spent a week tweaking a component before realizing the bug was in the "
        "parent container. The lesson: zoom out before you zoom in.",
    "before_after":
        "Clumsy: ship a button labeled 'submit'. Clean: ship a button labeled with "
        "the actual outcome, like 'create account'.",
    "single_question":
        "What is the smallest design decision you have ever made that quietly "
        "changed how a whole team worked?",
    "teardown":
        "Most onboarding screens skip the why and jump to the how. Tell people "
        "what they will be able to do, then show them how to do it.",
    "contrarian_take":
        "Design tokens are not a styling tool. They are a contract between design "
        "and engineering, and treating them like CSS variables misses the point.",
    "plain_definition":
        "A flex container is just a row or column that figures out how to share "
        "space among its children, the way a fair host divides a table.",
}


def _bare_engine(tmpdir):
    """Build a FollowerEngine without doing the live login. Mirrors the
    pattern in test_reconcile / test_status / test_stall."""
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
    e._insights = None
    return e


class _IsolatedStateTest(unittest.TestCase):
    """Shared fixture: every state file relocated to a temp dir so tests do
    not stomp on the developer's live state."""
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="kf-archetypes-"))
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


class DivergentVariantsTest(_IsolatedStateTest):
    def test_sampled_archetypes_are_distinct(self):
        """_sample_distinct_post_hooks must return three distinct arms when
        there are at least three to choose from. Without this, the variant
        prompt collapses to one archetype and the drafts read the same."""
        e = _bare_engine(self.tmpdir)
        for _ in range(10):
            picks = e._sample_distinct_post_hooks(3)
            self.assertEqual(len(picks), len(set(picks)),
                             f"archetypes must be distinct, got {picks}")
            self.assertEqual(len(picks), 3)
            for arch in picks:
                self.assertIn(arch, POST_HOOKS)

    def test_generate_variants_returns_distinct_archetypes(self):
        """The prompt assigns one archetype per slot. We mock the AI to
        return variants labeled with those slot archetypes and verify the
        parser preserves the per-variant archetype tag, which is what the
        bandit later learns on."""
        e = _bare_engine(self.tmpdir)
        # Capture archetypes the prompt asks for so the mock can echo them back.
        captured = {}
        orig = e._sample_distinct_post_hooks

        def spy(n=3):
            picks = orig(n)
            captured["picks"] = picks
            return picks

        e._sample_distinct_post_hooks = spy

        def fake_generate(prompt, dedup=False):
            archs = captured["picks"]
            variants = [
                {"archetype": archs[0],
                 "text": "Most layout bugs come from the parent, not the child.",
                 "thread_parts": []},
                {"archetype": archs[1],
                 "text": "What is the smallest fix that has saved you the most "
                         "debugging time?",
                 "thread_parts": ["A second short part that advances the idea."]
                 if archs[1] == "mini_thread" else []},
                {"archetype": archs[2],
                 "text": "Design tokens are a contract between teams, not a "
                         "styling shortcut, and reading them as CSS variables "
                         "misses the whole point.",
                 "thread_parts": []},
            ]
            return json.dumps({"variants": variants})

        e._generate = fake_generate
        archetypes, variants = e._generate_variants("ux_design")
        labels = [v["archetype"] for v in variants]
        self.assertEqual(len(labels), 3)
        self.assertEqual(len(set(labels)), 3,
                         f"variants should have distinct archetypes, got {labels}")
        self.assertEqual(set(labels), set(archetypes))

    def test_thread_parts_only_kept_for_mini_thread(self):
        """If the model puts thread_parts on a non-mini_thread variant, we
        scrub them; otherwise a contrarian_take could accidentally chain
        replies behind it."""
        e = _bare_engine(self.tmpdir)
        e._sample_distinct_post_hooks = lambda n=3: [
            "contrarian_take", "one_line_provocation", "i_was_wrong"
        ]
        e._generate = lambda prompt, dedup=False: json.dumps({"variants": [
            {"archetype": "contrarian_take", "text": "Stable tokens beat clever ones.",
             "thread_parts": ["a stray continuation that should be dropped"]},
            {"archetype": "one_line_provocation",
             "text": "Most onboarding flows lie about what users will actually do.",
             "thread_parts": []},
            {"archetype": "i_was_wrong",
             "text": "I shipped a confusing CTA last week and learned to say what "
                     "happens next, not what the button is doing.",
             "thread_parts": []},
        ]})
        _, variants = e._generate_variants("ux_design")
        by_arch = {v["archetype"]: v for v in variants}
        self.assertEqual(by_arch["contrarian_take"]["thread_parts"], [])


class ArchetypesPassGatesTest(_IsolatedStateTest):
    def test_every_archetype_in_soul_has_an_exemplar(self):
        """If a new archetype lands in soul.yaml without an exemplar in this
        test file, the rest of the gate tests will silently skip it. Force
        the author of a new archetype to add an exemplar here too."""
        for arch in POST_HOOKS:
            self.assertIn(arch, ARCHETYPE_EXEMPLARS,
                          f"missing test exemplar for archetype {arch!r}")

    def test_clean_exemplar_passes_gates_for_every_archetype(self):
        e = _bare_engine(self.tmpdir)
        for arch in POST_HOOKS:
            text = ARCHETYPE_EXEMPLARS[arch]
            self.assertTrue(
                e._passes_gates(text),
                f"clean exemplar for {arch!r} should pass gates, got:\n  {text}",
            )

    def test_em_dash_blocked_in_every_archetype(self):
        """Persona hard rule: no em dash. Inject one into each exemplar; the
        gate must reject it regardless of archetype. Append rather than
        replace so the injection lands even when the exemplar has no comma."""
        e = _bare_engine(self.tmpdir)
        for arch in POST_HOOKS:
            poisoned = ARCHETYPE_EXEMPLARS[arch] + " — extra."
            self.assertFalse(
                e._passes_gates(poisoned),
                f"em dash should be blocked in {arch!r} variant",
            )

    def test_oversize_text_blocked(self):
        e = _bare_engine(self.tmpdir)
        too_long = "a" * 301
        self.assertFalse(e._passes_gates(too_long))

    def test_mini_thread_variant_blocked_if_a_part_fails(self):
        """_variant_passes_gates must reject the whole mini_thread variant if
        any continuation fails. Without this we could publish a clean opener
        followed by a sensitive-word continuation."""
        e = _bare_engine(self.tmpdir)
        variant = {
            "archetype": "mini_thread",
            "text": ARCHETYPE_EXEMPLARS["mini_thread"],
            "thread_parts": [
                "a clean second part",
                "a dirty third part with an idiot in it",  # SENSITIVE_WORDS hit
            ],
        }
        self.assertFalse(e._variant_passes_gates(variant))


class HookStrengthArchetypeAwareTest(unittest.TestCase):
    def test_short_one_line_provocation_scores_above_default(self):
        """A 100-char text gets the default-archetype length bonus only if
        in [120,280]. Under one_line_provocation it must still score well
        because the archetype's own length window is <=120."""
        text = "Most design systems quietly fail because nobody owns the contract."
        provoc = hook_strength(text, "one_line_provocation")
        default = hook_strength(text, None)
        self.assertGreater(
            provoc, default,
            f"one_line_provocation should reward short text, got {provoc} vs {default}",
        )

    def test_single_question_rewards_trailing_question_mark(self):
        with_q = hook_strength("What is the smallest design decision you have made?",
                               "single_question")
        without_q = hook_strength("This is a sharp question to the niche.",
                                  "single_question")
        self.assertGreater(with_q, without_q)


class BanditMigrationTest(_IsolatedStateTest):
    def test_surviving_arm_keeps_posterior_and_new_arms_seeded(self):
        """Acceptance: a bandit state pre-populated with one OLD post_hook
        (no longer in POST_HOOKS) and one SURVIVING hook (still in
        POST_HOOKS) must, on load, drop the old arm, keep the survivor's
        posterior intact, and seed every other current archetype at
        Beta(1,1)."""
        survivor = POST_HOOKS[0]
        existing = {
            "sector": {config.SECTORS[0]: {"alpha": 5.0, "beta": 2.0}},
            "post_hook": {
                "viral_listicle": {"alpha": 7.0, "beta": 3.0},  # old, dropped
                survivor: {"alpha": 4.0, "beta": 6.0},          # survives
            },
            "reply_hook": {config.REPLY_HOOKS[0]: {"alpha": 1.0, "beta": 1.0}},
        }
        atomic_write_json(config.BANDIT_STATE_FILE, existing)

        s = Store()

        # Survivor keeps posterior.
        self.assertEqual(s.bandit["post_hook"][survivor]["alpha"], 4.0)
        self.assertEqual(s.bandit["post_hook"][survivor]["beta"], 6.0)
        # Stale arm is gone.
        self.assertNotIn("viral_listicle", s.bandit["post_hook"])
        # Every other current archetype is seeded at Beta(1,1).
        for arm in POST_HOOKS:
            if arm == survivor:
                continue
            self.assertEqual(s.bandit["post_hook"][arm], {"alpha": 1.0, "beta": 1.0},
                             f"new arm {arm!r} must be seeded at Beta(1,1)")
        # Sector posterior also preserved (sanity).
        self.assertEqual(s.bandit["sector"][config.SECTORS[0]]["alpha"], 5.0)


if __name__ == "__main__":
    unittest.main()
