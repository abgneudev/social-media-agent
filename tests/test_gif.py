"""Tests for Commit 4: Klipy GIF attachment.

Posture under test: GIF attachment is always a bonus, never required. The
post path must:

  1. Publish text-only when the variant has no gifQuery.
  2. Publish text-only when Klipy returns no match (or no app key).
  3. Publish text-only when the Klipy network fetch raises.
  4. Publish text-only when the Bluesky image upload raises.
  5. Use the strictest content_filter when calling Klipy.
  6. Cache by query so the same query in the same process hits the API once.

The Klipy resolver is exercised against a fake urlopen so tests do not
hit the live API.
"""
import io
import json
import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

from core import config
import klipy
from core.store import Store
from core.governance import RateBudget, CircuitBreaker
from core.engine import FollowerEngine


# A 1x1 transparent GIF (well-formed minimal payload). Used wherever the
# tests need plausible image bytes to flow through without a real Klipy.
TINY_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
            b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
            b"\x00\x00\x02\x02D\x01\x00;")


def _iso_state(test):
    test.tmpdir = Path(tempfile.mkdtemp(prefix="kf-gif-"))
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
    e.net.did = "did:plc:test"
    e.net.post.return_value = "at://did:plc:test/app.bsky.feed.post/abc"
    e.net.post_with_image.return_value = "at://did:plc:test/app.bsky.feed.post/img"
    e.net.find_post.return_value = None
    e.net.get_post_cid.return_value = "cid-1"
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


class _FakeResponse:
    """Minimal stand-in for urllib's response context manager. Used by the
    Klipy resolve/fetch tests to avoid live network calls."""
    def __init__(self, body, headers=None):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = headers or {"Content-Type": "image/gif"}

    def read(self, n=None):
        if n is None:
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class WriteFnFallbacksToTextOnlyTest(unittest.TestCase):
    def setUp(self):
        _iso_state(self)
        klipy.reset_cache()

    def tearDown(self):
        _teardown_iso(self)
        klipy.reset_cache()

    def test_no_gif_query_publishes_text_only(self):
        e = _bare_engine()
        write_fn = e._build_write_fn_with_optional_gif("", "the text")
        uri = write_fn("the text")
        self.assertEqual(uri, "at://did:plc:test/app.bsky.feed.post/abc")
        e.net.post.assert_called_once_with("the text")
        e.net.post_with_image.assert_not_called()

    def test_klipy_no_match_publishes_text_only(self):
        e = _bare_engine()
        write_fn = e._build_write_fn_with_optional_gif("aurora rocketship", "t")
        with mock.patch.object(klipy, "resolve", return_value=None):
            uri = write_fn("t")
        self.assertEqual(uri, "at://did:plc:test/app.bsky.feed.post/abc")
        e.net.post.assert_called_once_with("t")
        e.net.post_with_image.assert_not_called()

    def test_klipy_fetch_failure_publishes_text_only(self):
        e = _bare_engine()
        write_fn = e._build_write_fn_with_optional_gif("design coffee", "t")
        with mock.patch.object(klipy, "resolve",
                               return_value="https://example.com/x.gif"), \
             mock.patch.object(klipy, "fetch_bytes", return_value=None):
            uri = write_fn("t")
        self.assertEqual(uri, "at://did:plc:test/app.bsky.feed.post/abc")
        e.net.post_with_image.assert_not_called()

    def test_image_upload_raise_falls_back_to_text(self):
        e = _bare_engine()
        e.net.post_with_image.side_effect = RuntimeError("blob upload failed")
        write_fn = e._build_write_fn_with_optional_gif("design coffee", "t")
        with mock.patch.object(klipy, "resolve",
                               return_value="https://example.com/x.gif"), \
             mock.patch.object(klipy, "fetch_bytes",
                               return_value=(TINY_GIF, "image/gif")):
            uri = write_fn("t")
        self.assertEqual(uri, "at://did:plc:test/app.bsky.feed.post/abc")
        # Both attempted: image first, then text fallback.
        e.net.post_with_image.assert_called_once()
        e.net.post.assert_called_once_with("t")

    def test_happy_path_attaches_image(self):
        e = _bare_engine()
        write_fn = e._build_write_fn_with_optional_gif("celebration", "t")
        with mock.patch.object(klipy, "resolve",
                               return_value="https://example.com/x.gif"), \
             mock.patch.object(klipy, "fetch_bytes",
                               return_value=(TINY_GIF, "image/gif")):
            uri = write_fn("t")
        self.assertEqual(uri, "at://did:plc:test/app.bsky.feed.post/img")
        e.net.post_with_image.assert_called_once()
        e.net.post.assert_not_called()


class KlipyResolverTest(unittest.TestCase):
    def setUp(self):
        klipy.reset_cache()

    def tearDown(self):
        klipy.reset_cache()
        os.environ.pop("KLIPY_APP_KEY", None)

    def test_resolve_returns_none_without_app_key(self):
        os.environ.pop("KLIPY_APP_KEY", None)
        self.assertIsNone(klipy.resolve("celebration"))

    def test_resolve_picks_sm_webp_first(self):
        os.environ["KLIPY_APP_KEY"] = "test-key"
        payload = {"data": {"data": [
            {"file": {
                "xs": {"webp": "https://x/xs.webp", "gif": "https://x/xs.gif"},
                "sm": {"webp": "https://x/sm.webp", "gif": "https://x/sm.gif"},
            }}
        ]}}
        with mock.patch("klipy.urllib.request.urlopen",
                        return_value=_FakeResponse(json.dumps(payload))):
            url = klipy.resolve("celebration")
        self.assertEqual(url, "https://x/sm.webp")

    def test_resolve_uses_strictest_content_filter(self):
        os.environ["KLIPY_APP_KEY"] = "test-key"
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
            return _FakeResponse(json.dumps({"data": {"data": []}}))

        with mock.patch("klipy.urllib.request.urlopen", side_effect=fake_urlopen):
            klipy.resolve("anything")
        self.assertIn("content_filter=", captured["url"])
        self.assertIn(f"content_filter={klipy.KLIPY_CONTENT_FILTER}",
                      captured["url"])
        # Sanity: KLIPY_CONTENT_FILTER must be the strictest documented value.
        self.assertEqual(klipy.KLIPY_CONTENT_FILTER, "high")

    def test_resolve_caches_by_normalized_query(self):
        """Same query, twice, in the same process must hit the API once."""
        os.environ["KLIPY_APP_KEY"] = "test-key"
        payload = {"data": {"data": [
            {"file": {"sm": {"webp": "https://x/sm.webp"}}}
        ]}}
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            return _FakeResponse(json.dumps(payload))

        with mock.patch("klipy.urllib.request.urlopen", side_effect=fake_urlopen):
            url1 = klipy.resolve("Design  Coffee")
            url2 = klipy.resolve("design coffee")   # different case + spaces
        self.assertEqual(url1, "https://x/sm.webp")
        self.assertEqual(url2, url1)
        self.assertEqual(calls["n"], 1,
                         "second resolve should have hit the cache, not the API")

    def test_resolve_caches_negative_result(self):
        """A confirmed miss must not be re-queried."""
        os.environ["KLIPY_APP_KEY"] = "test-key"
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            return _FakeResponse(json.dumps({"data": {"data": []}}))

        with mock.patch("klipy.urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertIsNone(klipy.resolve("nothing matches"))
            self.assertIsNone(klipy.resolve("nothing matches"))
        self.assertEqual(calls["n"], 1)

    def test_resolve_returns_none_on_network_error(self):
        os.environ["KLIPY_APP_KEY"] = "test-key"
        with mock.patch("klipy.urllib.request.urlopen",
                        side_effect=OSError("network down")):
            self.assertIsNone(klipy.resolve("anything"))

    def test_fetch_bytes_respects_size_cap(self):
        """A GIF larger than KLIPY_MAX_BYTES must be dropped, not loaded."""
        oversized = b"\x00" * (klipy.KLIPY_MAX_BYTES + 10)
        with mock.patch("klipy.urllib.request.urlopen",
                        return_value=_FakeResponse(oversized)):
            self.assertIsNone(klipy.fetch_bytes("https://x/big.gif"))


class GenerateVariantsParsesGifQueryTest(unittest.TestCase):
    def setUp(self):
        _iso_state(self)

    def tearDown(self):
        _teardown_iso(self)

    def test_gif_query_stripped_and_preserved(self):
        e = _bare_engine()
        e._sample_distinct_post_hooks = lambda n=3: [
            "one_line_provocation", "teardown", "contrarian_take"
        ]

        def fake_generate(prompt, dedup=False):
            return json.dumps({"variants": [
                {"archetype": "one_line_provocation",
                 "text": "Most onboarding lies about what users actually do.",
                 "thread_parts": [], "gifQuery": "  surprised cat  "},
                {"archetype": "teardown",
                 "text": "Stop labeling buttons 'submit'. Name the outcome.",
                 "thread_parts": []},   # gifQuery missing entirely
                {"archetype": "contrarian_take",
                 "text": "Design tokens are a contract, not styling shortcuts.",
                 "thread_parts": [], "gifQuery": ""},
            ]})

        e._generate = fake_generate
        _, variants = e._generate_variants("ux_design")
        by_arch = {v["archetype"]: v for v in variants}
        self.assertEqual(by_arch["one_line_provocation"]["gif_query"],
                         "surprised cat")
        self.assertEqual(by_arch["teardown"]["gif_query"], "")
        self.assertEqual(by_arch["contrarian_take"]["gif_query"], "")


if __name__ == "__main__":
    unittest.main()
