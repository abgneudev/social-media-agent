"""Tests for Phase A: the soul-file loader.

Two acceptance properties:

1. The loader fails CLOSED. A missing or malformed soul.yaml must raise
   SoulLoadError rather than degrade to silent defaults. An agent with no
   persona or no niche keywords would still try to post; refusing to start
   is the only safe response.

2. The soul cannot WEAKEN the code-defined safety floor. Even if a
   soul.yaml omits or attempts to narrow the sensitive_* lists, the
   effective gate lists must still contain every floor entry, merged by
   union.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

import config
from config import load_soul, SoulLoadError


def _write(tmpdir, content):
    p = Path(tmpdir) / "soul.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# Minimum viable soul; tests start from this and break one thing at a time.
_OK_SOUL = dedent("""
    name: Test Agent
    bio: A test bio that is sufficiently long.
    persona: A short persona for tests.
    post_hooks: [viral_listicle]
    reply_hooks: [yes_and_expansion]
    post_hook_guidance:
      viral_listicle: "Be punchy."
    reply_hook_guidance:
      yes_and_expansion: "Affirm and extend."
    keyword_map:
      topic: ["keyword one", "keyword two"]
    relevance_signals: ["foo", "bar"]
""").lstrip()


class SoulLoaderFailsClosedTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kf-soul-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_raises(self):
        missing = Path(self.tmpdir) / "no_such_soul.yaml"
        with self.assertRaises(SoulLoadError):
            load_soul(missing)

    def test_malformed_yaml_raises(self):
        p = _write(self.tmpdir, "name: [unclosed\n  - bracket")
        with self.assertRaises(SoulLoadError):
            load_soul(p)

    def test_not_a_mapping_raises(self):
        p = _write(self.tmpdir, "- this\n- is\n- a list")
        with self.assertRaises(SoulLoadError):
            load_soul(p)

    def test_missing_required_key_raises(self):
        # Drop persona; everything else is fine.
        partial = _OK_SOUL.replace("persona: A short persona for tests.\n", "")
        p = _write(self.tmpdir, partial)
        with self.assertRaisesRegex(SoulLoadError, "persona"):
            load_soul(p)

    def test_empty_persona_raises(self):
        partial = _OK_SOUL.replace(
            "persona: A short persona for tests.",
            'persona: ""',
        )
        p = _write(self.tmpdir, partial)
        with self.assertRaisesRegex(SoulLoadError, "persona"):
            load_soul(p)

    def test_empty_keyword_map_raises(self):
        partial = _OK_SOUL.replace(
            'keyword_map:\n  topic: ["keyword one", "keyword two"]\n',
            "keyword_map: {}\n",
        )
        p = _write(self.tmpdir, partial)
        with self.assertRaisesRegex(SoulLoadError, "keyword_map"):
            load_soul(p)

    def test_hook_without_guidance_raises(self):
        partial = _OK_SOUL.replace(
            "post_hooks: [viral_listicle]",
            "post_hooks: [viral_listicle, storytelling]",
        )
        p = _write(self.tmpdir, partial)
        with self.assertRaisesRegex(SoulLoadError, "storytelling"):
            load_soul(p)

    def test_valid_soul_loads(self):
        p = _write(self.tmpdir, _OK_SOUL)
        soul = load_soul(p)
        self.assertEqual(soul.name, "Test Agent")
        self.assertEqual(soul.post_hooks, ["viral_listicle"])
        self.assertEqual(list(soul.keyword_map.keys()), ["topic"])


class SoulCannotWeakenSafetyFloorTest(unittest.TestCase):
    """The soul may only ADD to safety lists. A soul that omits the floor
    entirely, or that supplies a narrower list, must still produce an
    effective gate list that contains every floor entry."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kf-soul-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_soul_without_extras_keeps_full_floor(self):
        # Soul has no extra_sensitive_* fields at all; the merge in config
        # at import time still yields a list that contains every floor entry.
        for word in config.SENSITIVE_WORDS_FLOOR:
            self.assertIn(word, config.SENSITIVE_WORDS,
                          f"safety floor word {word!r} missing from merged list")
        for phrase in config.SENSITIVE_PHRASES_FLOOR:
            self.assertIn(phrase, config.SENSITIVE_PHRASES)
        for phrase in config.SPAM_PHRASES_FLOOR:
            self.assertIn(phrase, config.SPAM_PHRASES)

    def test_soul_that_tries_to_narrow_safety_does_not_weaken_it(self):
        """A soul that uses extra_sensitive_words to supply a deliberately
        sparse list (with a benign entry, not even attempting to override
        anything) does not remove any floor entry. The merge is union, so
        there is no API to remove."""
        narrow = _OK_SOUL + dedent("""
            extra_sensitive_words: ["only_my_custom_word"]
            extra_sensitive_phrases: []
            extra_spam_phrases: []
        """)
        p = _write(self.tmpdir, narrow)
        soul = load_soul(p)
        merged_words = sorted(
            set(config.SENSITIVE_WORDS_FLOOR) | set(soul.extra_sensitive_words)
        )
        # Floor entries survive. The new custom word is included. No floor
        # entry was dropped.
        for w in config.SENSITIVE_WORDS_FLOOR:
            self.assertIn(w, merged_words,
                          f"floor word {w!r} disappeared from merged list")
        self.assertIn("only_my_custom_word", merged_words)

    def test_loader_rejects_non_list_extra_safety_fields(self):
        # If someone puts extra_sensitive_words as a string ("idiot") thinking
        # they can override, the loader must refuse rather than silently
        # iterate the characters.
        bad = _OK_SOUL + 'extra_sensitive_words: "should_be_a_list"\n'
        p = _write(self.tmpdir, bad)
        with self.assertRaisesRegex(SoulLoadError, "extra_sensitive_words"):
            load_soul(p)


if __name__ == "__main__":
    unittest.main()
