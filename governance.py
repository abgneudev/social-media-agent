"""Governance: token-bucket rate budgets and the circuit breaker.

Both are tiny state holders kept apart from Store so the engine can declare
them inline at __init__ time. CircuitBreaker is the only one that persists
(its state survives process restarts so a tripped breaker stays tripped).
"""
import time

import config
from config import (
    CIRCUIT_BREAKER_THRESHOLD, CIRCUIT_BREAKER_COOLDOWN,
    logger,
)
from store import atomic_write_json, load_json


class RateBudget:
    """Token bucket. Refill steadily, spend one token per action."""
    def __init__(self, capacity, refill_per_sec):
        self.capacity = float(capacity)
        self.refill = float(refill_per_sec)
        self.tokens = float(capacity)
        self.last = time.time()

    def _refill(self):
        now = time.time()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.refill)
        self.last = now

    def try_consume(self, n=1) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


import contextlib
from atproto import exceptions

class CircuitBreaker:
    """CLOSED normal, OPEN blocks network and cools down, then auto-resets."""
    def __init__(self):
        self.state = "CLOSED"
        self.consecutive_failures = 0
        self.opened_at = 0.0
        data = load_json(config.CIRCUIT_BREAKER_FILE, None)
        if data:
            self.state = data.get("state", "CLOSED")
            self.consecutive_failures = data.get("consecutive_failures", 0)
            self.opened_at = data.get("opened_at", 0.0)

    def _persist(self):
        atomic_write_json(config.CIRCUIT_BREAKER_FILE, {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "opened_at": self.opened_at,
        })

    def record_success(self):
        if self.consecutive_failures or self.state != "CLOSED":
            self.consecutive_failures = 0
            self.state = "CLOSED"
            self._persist()

    @contextlib.contextmanager
    def guard(self, action_name="Action"):
        try:
            yield
            self.record_success()
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] {action_name} failed: {e}")
            self.record_failure()

    def record_failure(self):
        self.consecutive_failures += 1
        logger.warning(f"      [BREAKER] failures {self.consecutive_failures}/{CIRCUIT_BREAKER_THRESHOLD}")
        if self.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            self.state = "OPEN"
            self.opened_at = time.time()
            logger.error(f"      [BREAKER] OPEN. Cooling down {CIRCUIT_BREAKER_COOLDOWN // 60} min.")
        self._persist()

    def trip_open(self, reason="forced"):
        """Force the breaker open without incrementing the per-failure count.
        Used by the stall detector when the loop is alive but producing no
        useful work tick after tick: something is wrong upstream and silently
        spinning will not surface it. Idempotent if already open."""
        if self.state == "OPEN":
            return
        self.state = "OPEN"
        self.opened_at = time.time()
        if self.consecutive_failures < CIRCUIT_BREAKER_THRESHOLD:
            self.consecutive_failures = CIRCUIT_BREAKER_THRESHOLD
        logger.error(f"      [BREAKER] OPEN ({reason}). Cooling down "
                     f"{CIRCUIT_BREAKER_COOLDOWN // 60} min.")
        self._persist()

    def is_open(self) -> bool:
        if self.state != "OPEN":
            return False
        elapsed = time.time() - self.opened_at
        if elapsed >= CIRCUIT_BREAKER_COOLDOWN:
            logger.info("      [BREAKER] cooldown over, resetting to CLOSED.")
            self.state = "CLOSED"
            self.consecutive_failures = 0
            self._persist()
            return False
        logger.info(f"      [BREAKER] OPEN, {CIRCUIT_BREAKER_COOLDOWN - elapsed:.0f}s left.")
        return True
