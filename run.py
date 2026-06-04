"""Runtime entry point.

The loop here intentionally stays minimal: configure logging, normalize
credentials, build the engine, then tick forever. All the actual logic
lives in engine.py. The two pieces of robustness that bind tick to tick
(stall counter, heartbeat) run in this file because they must execute even
when tick() raises.
"""
import os
import time

import config
from config import (
    logger, configure_logging,
    FOLLOWER_TARGET, TICK_INTERVAL,
    CONTENT_ATTRIBUTION_SECONDS, FOLLOW_ATTRIBUTION_SECONDS,
)
from engine import FollowerEngine, write_status


def main():
    configure_logging()
    handle = os.environ.get("BLUESKY_HANDLE", "")
    password = os.environ.get("BLUESKY_PASSWORD", "")
    # Normalize: strip whitespace, surrounding quotes, and a leading '@'.
    # Bluesky treats an identifier containing '@' as an email; "@handle" -> empty
    # local part -> "InvalidEmail: Address local part cannot be empty".
    handle = handle.strip().strip('"').strip("'").lstrip("@")
    password = password.strip().strip('"').strip("'")
    if not handle or not password:
        logger.error("[FATAL] set BLUESKY_HANDLE and BLUESKY_PASSWORD.")
        raise SystemExit(1)

    logger.info("=" * 60)
    logger.info("[SYSTEM] Kiloforge Follower Engine v3")
    logger.info(f"[SYSTEM] goal: {FOLLOWER_TARGET} real follower(s), measured directly")
    logger.info(f"[SYSTEM] tick={TICK_INTERVAL}s content_window={CONTENT_ATTRIBUTION_SECONDS}s "
                f"follow_window={FOLLOW_ATTRIBUTION_SECONDS}s")
    logger.info(f"[SYSTEM] kill switch: write HALTED to {config.KILL_SWITCH_FILE.name}")
    logger.info("=" * 60)

    engine = FollowerEngine(handle, password)
    engine.bootstrap()

    while True:
        try:
            engine.tick()
            engine.report()
        except (KeyboardInterrupt, SystemExit):
            # Operator stop. Let the process exit cleanly so systemd sees it.
            logger.info("[SYSTEM] interrupt received, exiting.")
            raise
        except Exception:
            # Log the exception type AND a full traceback so unattended runs
            # surface real programming errors instead of swallowing them into
            # a one-line message that looks like a transient network blip.
            # logger.exception attaches the traceback at ERROR level.
            logger.exception("[GUARD] tick raised an unhandled exception")
        # Stall accounting runs whether the tick succeeded or raised, so a
        # persistent error eventually trips the breaker instead of spinning
        # silently forever.
        try:
            engine.update_stall_counter()
        except Exception:
            logger.exception("[GUARD] stall counter raised")
        # Heartbeat: a single atomic JSON write the operator (or a monitoring
        # scrape) can poll to see the daemon is alive and what it just did,
        # without parsing logs.
        try:
            write_status(engine)
        except Exception:
            logger.exception("[GUARD] status write raised")
        logger.info(f"[SYSTEM] sleeping {TICK_INTERVAL // 60} min...")
        time.sleep(TICK_INTERVAL)


if __name__ == "__main__":
    main()
