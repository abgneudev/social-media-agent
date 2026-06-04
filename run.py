"""Runtime entry point.

The loop here intentionally stays minimal: configure logging, normalize
credentials, build the engine, then tick forever. All the actual logic
lives in engine.py. The two pieces of robustness that bind tick to tick
(stall counter, heartbeat) run in this file because they must execute even
when tick() raises.

A --dry-run mode skips Bluesky login entirely and only exercises the
generation path, printing the three structurally-different archetype
drafts for one sector. Useful for previewing variant divergence without
publishing.
"""
import os
import sys
import json
import time
import random

import config
from config import (
    logger, configure_logging,
    FOLLOWER_TARGET, TICK_INTERVAL,
    CONTENT_ATTRIBUTION_SECONDS, FOLLOW_ATTRIBUTION_SECONDS,
    SECTORS,
)
from engine import FollowerEngine, write_status


def _dry_run():
    """Generate three divergent variants for one sector and print them.
    Skips Bluesky login (no adapter constructed); still requires a Groq
    key so the generation call can fire."""
    configure_logging()
    if not os.environ.get("GROQ_API_KEY"):
        logger.error("[DRYRUN] set GROQ_API_KEY to exercise the generation path.")
        raise SystemExit(1)
    from groq import Groq
    from store import Store
    from governance import RateBudget, CircuitBreaker
    from config import RATE_BUDGETS, PERSONA

    sector = sys.argv[2] if len(sys.argv) > 2 else random.choice(SECTORS)
    if sector not in SECTORS:
        logger.error(f"[DRYRUN] unknown sector '{sector}'. Choose from: {SECTORS}")
        raise SystemExit(1)
    logger.info(f"[DRYRUN] generating divergent variants for sector='{sector}'")

    e = FollowerEngine.__new__(FollowerEngine)
    e.store = Store()
    e.net = None
    e.ai = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    e.breaker = CircuitBreaker()
    e.rate = {k: RateBudget(v["capacity"], v["refill_per_sec"])
              for k, v in RATE_BUDGETS.items()}
    e.sector_activity = {}
    e.sector_posts = {}
    e.persona = PERSONA
    result = e.dry_run_post(sector)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
        _dry_run()
        return
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
