"""Tests for Commit 3: niche analyzer that FEEDS the diversity engine.

The analyzer must NOT collapse drafts onto one voice. Acceptance properties:

1. Analyzer output is STRUCTURAL ONLY. The written blob has no field that
   carries verbatim source text or paste-back sentences. Topic angles are
   short noun phrases, capped by length. No "examples", "quotes",
   "verbatim", or "snippets" fields exist on the blob.

2. After the exploration nudge is applied at sampling time, every
   archetype is STILL sampled with non-trivial frequency. The nudge biases
   toward hot archetypes, never zeroes the others out. Without this, the
   analyzer would collapse the bandit onto whatever was hot last week and
   stop exploring.

3. Graceful absence: when no niche_insights file exists, _sample_distinct
   _post_hooks and _generate_variants behave EXACTLY as they did before
   the analyzer landed. The agent must never depend on the blob being
   present.
"""
import os
import sys
import json
import tempfile
import shutil
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

import config
from config import POST_HOOKS
import analyzer
from store import Store, atomic_write_json
from governance import RateBudget, CircuitBreaker
from engine import FollowerEngine


SOURCE_SENTENCES = [
    "This is a real sentence from a sampled post that must not leak back.",
    "Here is a second example with very specific phrasing we do not want copied.",
    "And a third, this time ending in a question to test detection?",
]


def _iso_state(test):
    """Shared temp-dir + state-file patches. Helper rather than a base class
    to keep test classes flat and grep-friendly."""
    test.tmpdir = Path(tempfile.mkdtemp(prefix="kf-analyzer-"))
    test.patches = []
    for name in ("BANDIT_STATE_FILE", "ACTION_LEDGER_FILE", "SNAPSHOT_FILE",
                 "SEEN_FILE", "ENGINE_STATE_FILE", "CIRCUIT_BREAKER_FILE",
                 "KILL_SWITCH_FILE", "PENDING_WRITES_FILE", "STATUS_FILE",
                 "NICHE_INSIGHTS_FILE"):
        p = mock.patch.object(config, name, test.tmpdir / f"{name.lower()}")
        p.start()
        test.patches.append(p)


def _teardown_iso(test):
    for p in test.patches:
        p.stop()
    shutil.rmtree(test.tmpdir, ignore_errors=True)


def _bare_engine():
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


class _Author:
    def __init__(self, did):
        self.did = did


class _Record:
    def __init__(self, text):
        self.text = text


class _Post:
    def __init__(self, text, like=0, repost=0, reply=0, author_did=None):
        self.record = _Record(text)
        self.author = _Author(author_did or "did:plc:other")
        self.like_count = like
        self.repost_count = repost
        self.reply_count = reply


class AnalyzerStructuralOnlyTest(unittest.TestCase):
    def setUp(self):
        _iso_state(self)

    def tearDown(self):
        _teardown_iso(self)

    def test_blob_has_no_verbatim_fields(self):
        """The schema itself must not carry copy-back-prone fields. If a
        future change adds an 'examples' or 'quotes' field, this test
        fails so the author has to defend that choice in review."""
        net = mock.MagicMock()
        net.did = "did:plc:us"
        net.search_posts.return_value = [
            _Post(SOURCE_SENTENCES[0], like=12),
            _Post(SOURCE_SENTENCES[1], like=8),
            _Post(SOURCE_SENTENCES[2], like=4),
        ]

        def fake_generate(prompt):
            return json.dumps({"classifications": [
                {"archetype": POST_HOOKS[0], "topic_angle": "design tokens"},
                {"archetype": POST_HOOKS[1], "topic_angle": "dark mode contrast"},
                {"archetype": POST_HOOKS[2], "topic_angle": "usability sample size"},
            ]})

        blob = analyzer.run(net, fake_generate)
        self.assertIsNotNone(blob)
        # Schema contains only the distributional fields we expect.
        self.assertEqual(
            set(blob.keys()),
            {"ts", "ts_iso", "sample_size", "archetype_traction", "topic_angles"},
            "analyzer blob schema must stay strictly structural",
        )
        forbidden = {"examples", "quotes", "verbatim", "snippets", "samples",
                     "raw", "guidance", "style", "voice", "phrasing", "tone"}
        self.assertTrue(forbidden.isdisjoint(blob.keys()),
                        f"blob contains a copy-prone field: {set(blob.keys()) & forbidden}")

    def test_topic_angles_are_short_noun_phrases(self):
        """Even if the model tries to slip a full sentence into
        topic_angle, the analyzer must enforce the length cap and strip
        trailing punctuation. No angle in the blob should look like a
        full sentence."""
        net = mock.MagicMock()
        net.did = "did:plc:us"
        net.search_posts.return_value = [_Post(SOURCE_SENTENCES[0], like=10)]

        def fake_generate(prompt):
            return json.dumps({"classifications": [
                # Way too long, ends in period: should be capped and stripped.
                {"archetype": POST_HOOKS[0],
                 "topic_angle":
                     "this is a full sentence the model tried to smuggle "
                     "back as a topic angle that should be rejected."},
                # Clean, short.
                {"archetype": POST_HOOKS[1], "topic_angle": "css subgrid"},
            ]})

        blob = analyzer.run(net, fake_generate)
        self.assertIsNotNone(blob)
        for angle in blob["topic_angles"]:
            self.assertLessEqual(
                len(angle), analyzer.TOPIC_ANGLE_CHAR_CAP,
                f"topic_angle exceeds cap: {angle!r}",
            )
            self.assertFalse(angle.endswith("."), f"angle is a sentence: {angle!r}")
            self.assertFalse(angle.endswith("?"), f"angle is a sentence: {angle!r}")
            self.assertFalse(angle.endswith("!"), f"angle is a sentence: {angle!r}")

    def test_topic_angles_do_not_contain_source_sentences(self):
        """Defensive: if the model echoes a source sentence back as a topic
        angle, the length cap should chop it. Verify the cap actually
        prevents whole source sentences from surviving."""
        net = mock.MagicMock()
        net.did = "did:plc:us"
        net.search_posts.return_value = [_Post(SOURCE_SENTENCES[1], like=10)]

        def fake_generate(prompt):
            return json.dumps({"classifications": [
                {"archetype": POST_HOOKS[0], "topic_angle": SOURCE_SENTENCES[1]},
            ]})

        blob = analyzer.run(net, fake_generate)
        # Either the angle was dropped for ending in punctuation, or it
        # was truncated and is no longer an intact copy of the source.
        for angle in blob["topic_angles"]:
            self.assertNotIn(SOURCE_SENTENCES[1], angle,
                             "verbatim source sentence leaked into topic_angles")
            self.assertLess(len(angle), len(SOURCE_SENTENCES[1]))

    def test_classification_with_invalid_archetype_dropped(self):
        net = mock.MagicMock()
        net.did = "did:plc:us"
        net.search_posts.return_value = [_Post("anything", like=3)]

        def fake_generate(prompt):
            return json.dumps({"classifications": [
                {"archetype": "made_up_archetype", "topic_angle": "noun phrase"},
                {"archetype": POST_HOOKS[0], "topic_angle": "real one"},
            ]})

        blob = analyzer.run(net, fake_generate)
        self.assertEqual(blob["sample_size"], 1)
        self.assertEqual(blob["archetype_traction"][POST_HOOKS[0]], 1)


class ExplorationNudgePreservesAllArmsTest(unittest.TestCase):
    """Even with a strongly skewed nudge, every archetype must still get
    sampled with non-trivial frequency. The exploration nudge is supposed
    to bias, not collapse."""

    def setUp(self):
        _iso_state(self)

    def tearDown(self):
        _teardown_iso(self)

    def test_every_archetype_sampled_with_hot_archetype_nudged(self):
        e = _bare_engine()
        hot = POST_HOOKS[0]
        # The hottest archetype dominates the traction counts.
        e._insights = {
            "archetype_traction": {a: (100 if a == hot else 0) for a in POST_HOOKS},
            "topic_angles": [],
        }
        seen = Counter()
        # 600 draws of 3 distinct archetypes each = 1800 single-arm picks.
        # With 8 archetypes that is plenty for every one to appear unless
        # the nudge mechanism is zeroing arms.
        for _ in range(600):
            for arch in e._sample_distinct_post_hooks(3):
                seen[arch] += 1
        for arch in POST_HOOKS:
            self.assertGreater(
                seen[arch], 0,
                f"archetype {arch!r} never sampled under nudge "
                f"(seen counts: {dict(seen)})",
            )
        # And the hot archetype should appear MORE often than a non-hot
        # one. That confirms the nudge is doing its job.
        cold_min = min(seen[a] for a in POST_HOOKS if a != hot)
        self.assertGreater(seen[hot], cold_min,
                           "nudge had no measurable effect; not biasing toward hot archetype")

    def test_nudge_is_capped(self):
        """archetype_nudges must clamp each arm's bump to
        EXPLORATION_NUDGE_MAX. A runaway value would erase the rest of the
        bandit signal."""
        blob = {"archetype_traction": {a: 99999 for a in POST_HOOKS}}
        n = analyzer.archetype_nudges(blob, config.EXPLORATION_NUDGE_MAX)
        for a, bump in n.items():
            self.assertLessEqual(bump, config.EXPLORATION_NUDGE_MAX + 1e-9,
                                 f"nudge for {a!r} exceeded cap: {bump}")


class GracefulAbsenceTest(unittest.TestCase):
    """If niche_insights is missing or stale, the rest of the engine must
    run exactly as it did without the analyzer. The agent never depends on
    the blob being present."""

    def setUp(self):
        _iso_state(self)

    def tearDown(self):
        _teardown_iso(self)

    def test_load_insights_returns_none_when_file_absent(self):
        # File does not exist yet under the patched STATE_DIR.
        self.assertIsNone(analyzer.load_insights())

    def test_sampling_works_with_no_insights(self):
        e = _bare_engine()
        e._insights = None   # explicit absence
        picks = e._sample_distinct_post_hooks(3)
        self.assertEqual(len(set(picks)), 3)
        for p in picks:
            self.assertIn(p, POST_HOOKS)

    def test_variant_prompt_omits_topic_angles_when_no_insights(self):
        e = _bare_engine()
        e._insights = None
        captured = {}

        def capturing_generate(prompt, dedup=False):
            captured["prompt"] = prompt
            return json.dumps({"variants": []})

        e._generate = capturing_generate
        e._generate_variants("ux_design")
        self.assertIn("prompt", captured)
        self.assertNotIn("Topic angles currently earning traction",
                         captured["prompt"],
                         "topic_angles section appeared without insights")

    def test_variant_prompt_includes_topic_angles_when_insights_present(self):
        e = _bare_engine()
        e._insights = {
            "archetype_traction": {a: 1 for a in POST_HOOKS},
            "topic_angles": ["design tokens", "css subgrid", "dark mode contrast"],
        }
        captured = {}

        def capturing_generate(prompt, dedup=False):
            captured["prompt"] = prompt
            return json.dumps({"variants": []})

        e._generate = capturing_generate
        e._generate_variants("ux_design")
        self.assertIn("Topic angles currently earning traction", captured["prompt"])
        # At least one angle landed in the prompt.
        any_angle = any(
            a in captured["prompt"]
            for a in ["design tokens", "css subgrid", "dark mode contrast"]
        )
        self.assertTrue(any_angle, "no topic angle made it into the prompt")

    def test_corrupt_insights_blob_treated_as_absent(self):
        """A malformed JSON or wrong-shape blob must NOT crash the engine;
        load_insights returns None and the sampler/prompt run as if there
        were no analyzer."""
        # Write a malformed JSON.
        config.NICHE_INSIGHTS_FILE.write_text("not json at all", encoding="utf-8")
        self.assertIsNone(analyzer.load_insights())
        # Write a JSON missing the required key.
        atomic_write_json(config.NICHE_INSIGHTS_FILE, {"unrelated": True})
        self.assertIsNone(analyzer.load_insights())


if __name__ == "__main__":
    unittest.main()
