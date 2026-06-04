"""Tests for Phase B: KF_STATE_DIR routes all engine state.

Two acceptance properties:

1. resolve_state_dir() honors $KF_STATE_DIR when set, falls back to
   _SCRIPT_DIR when unset, and creates the directory if it does not exist
   (Render mounts an empty disk on first boot).

2. Every state-file constant in config.py points UNDER STATE_DIR. A path
   that bypasses KF_STATE_DIR is the bug we are guarding against: it would
   silently reset on every Render deploy.
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


class ResolveStateDirTest(unittest.TestCase):
    def test_env_unset_falls_back_to_script_dir(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KF_STATE_DIR", None)
            self.assertEqual(config.resolve_state_dir(), config._SCRIPT_DIR)

    def test_env_set_returns_that_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="kf-statedir-"))
        try:
            with mock.patch.dict(os.environ, {"KF_STATE_DIR": str(tmp)}):
                resolved = config.resolve_state_dir()
            self.assertEqual(resolved, tmp.resolve())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_env_set_to_missing_path_creates_it(self):
        parent = Path(tempfile.mkdtemp(prefix="kf-statedir-"))
        target = parent / "does" / "not" / "exist" / "yet"
        try:
            self.assertFalse(target.exists())
            with mock.patch.dict(os.environ, {"KF_STATE_DIR": str(target)}):
                resolved = config.resolve_state_dir()
            self.assertTrue(resolved.exists(), "resolve_state_dir must mkdir")
            self.assertTrue(resolved.is_dir())
        finally:
            shutil.rmtree(parent, ignore_errors=True)

    def test_empty_env_value_falls_back(self):
        """KF_STATE_DIR='' should behave the same as unset."""
        with mock.patch.dict(os.environ, {"KF_STATE_DIR": ""}):
            self.assertEqual(config.resolve_state_dir(), config._SCRIPT_DIR)


class AllStateFilesUnderStateDirTest(unittest.TestCase):
    """If a state path bypasses STATE_DIR, the agent silently resets on
    every Render deploy. This test asserts there is no such path."""

    STATE_FILE_NAMES = [
        "BANDIT_STATE_FILE",
        "ACTION_LEDGER_FILE",
        "SNAPSHOT_FILE",
        "SEEN_FILE",
        "ENGINE_STATE_FILE",
        "CIRCUIT_BREAKER_FILE",
        "KILL_SWITCH_FILE",
        "PENDING_WRITES_FILE",
        "STATUS_FILE",
    ]

    def test_every_state_file_path_lives_under_STATE_DIR(self):
        for name in self.STATE_FILE_NAMES:
            self.assertTrue(hasattr(config, name), f"config missing {name}")
            p = getattr(config, name)
            self.assertEqual(p.parent.resolve(), config.STATE_DIR.resolve(),
                             f"{name} ({p}) is not under STATE_DIR ({config.STATE_DIR})")

    def test_soul_file_stays_with_code_not_state(self):
        """soul.yaml ships with the deploy (read-only config), not the
        mounted disk. Verify it does NOT end up under STATE_DIR when the
        two are different paths."""
        # In the running test process, STATE_DIR equals _SCRIPT_DIR (because
        # KF_STATE_DIR is unset). The structural assertion is: the source
        # uses _SCRIPT_DIR for SOUL_FILE, not STATE_DIR.
        import inspect
        src = inspect.getsource(config)
        # Find the SOUL_FILE assignment line. It must reference _SCRIPT_DIR.
        soul_lines = [ln for ln in src.splitlines() if ln.strip().startswith("SOUL_FILE")]
        self.assertTrue(soul_lines, "config.py must define SOUL_FILE")
        self.assertTrue(
            any("_SCRIPT_DIR" in ln for ln in soul_lines),
            "SOUL_FILE must be anchored at _SCRIPT_DIR, not STATE_DIR",
        )


if __name__ == "__main__":
    unittest.main()
