"""Store: atomic JSON I/O helpers and the Store class.

The Store holds the engine's persistent state: bandit posteriors, action
ledger, follower-count snapshots, dedup seen-set, engine scratch state, and
the pending-writes queue used by the reconcile-before-retry mechanism.
"""
import os
import json
import time
import uuid
import tempfile

from core import config
from core.config import (
    STATE_DIR,
    POST_DECAY,
    FOLLOW_ATTRIBUTION_SECONDS, CONTENT_ATTRIBUTION_SECONDS,
    logger
)
import re


# ==========================================
# ATOMIC STATE I/O
# ==========================================
def atomic_write_json(filepath, data):
    """Write JSON to a tempfile in the SAME directory as filepath, then
    os.replace. Same-filesystem rename is atomic on POSIX, so a concurrent
    reader (or a crash mid-write) never sees a partial file."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(filepath.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, str(filepath))
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def load_json(filepath, default):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ==========================================
# STORE
# ==========================================
class Store:
    def __init__(self, soul):
        self.soul = soul
        es = load_json(config.ENGINE_STATE_FILE, {})
        saved_sectors = es.get("sectors", [])
        self.sectors = list(set(saved_sectors) | set(getattr(self.soul, "sectors", [])))
        self.bandit = self._load_bandit(es.get("bandit", None), self.sectors)
        self.ledger = es.get("ledger", [])
        self.keyword_telemetry = es.get("keyword_telemetry", {})
        self.authorities = es.get("authorities", {})
        
        self.snapshots = load_json(config.SNAPSHOT_FILE, [])
        self.seen = set(load_json(config.SEEN_FILE, []))
        self.pending = load_json(config.PENDING_WRITES_FILE, [])
        self.tick = es.get("tick", 0)
        self.anchor_posts = es.get("anchor_posts", 0)
        self.phase = es.get("phase", "bootstrap")
        self.ewma_growth = es.get("ewma_growth", 0.0)
        self.trends = es.get("trends", {})
        self.pinned = es.get("pinned", False)
        self.active_plan = es.get("active_plan", None)
        self.last_profile_opt_tick = es.get("last_profile_opt_tick", -999)
        self.last_research_tick = es.get("last_research_tick", -999)
        self.research_interval = es.get("research_interval", 8)
        self.consecutive_empty_ticks = es.get("consecutive_empty_ticks", 0)
        
        saved_topics = es.get("topic_angle_examples", [])
        self.topic_angle_examples = list(set(saved_topics) | set(getattr(self.soul, "topic_angle_examples", [])))
        
        self.keyword_map = es.get("keyword_map", getattr(self.soul, "keyword_map", {}))
        
        saved_signals = es.get("relevance_signals", [])
        self.relevance_signals = list(set(saved_signals) | set(getattr(self.soul, "relevance_signals", [])))
        self._compile_relevance_re()
        
        logger.info(f"      [STATE] bandit={json.dumps(self.bandit)}")
        logger.info(f"      [STATE] ledger={len(self.ledger)} actions, "
                    f"snapshots={len(self.snapshots)}, seen={len(self.seen)}, "
                    f"pending={len(self.pending)}")

    def _load_bandit(self, loaded, active_sectors):
        """Load bandit state and migrate to the current arm vocabulary."""
        # Retain dynamic hooks from web research
        web_insights = load_json(config.STATE_DIR / "web_insights.json", {})
        dynamic_hooks = [h.get("hook_name") for h in web_insights.get("experimental_hooks", []) if h.get("hook_name")]
        if web_insights.get("curated_links"):
            dynamic_hooks.append("curated_link")

        all_post_hooks = set(self.soul.post_hooks) | set(dynamic_hooks)

        fresh = {dim: {v: {"alpha": 1.0, "beta": 1.0} for v in vals}
                 for dim, vals in (("sector", active_sectors),
                                   ("post_hook", all_post_hooks),
                                   ("reply_hook", self.soul.reply_hooks))}
        if loaded is None:
            return fresh
        for dim, vals in fresh.items():
            loaded.setdefault(dim, {})
            for v, prior in vals.items():
                loaded[dim].setdefault(v, prior)
            stale_arms = [a for a in loaded[dim] if a not in vals]
            for a in stale_arms:
                del loaded[dim][a]
        for stale in [k for k in loaded if k not in fresh]:
            del loaded[stale]
        return loaded

    def save_bandit(self):  self.save_engine()
    def save_ledger(self):  self.save_engine()
    def save_keyword_telemetry(self): self.save_engine()
    
    def save_snapshots(self): atomic_write_json(config.SNAPSHOT_FILE, self.snapshots)
    def save_seen(self):    atomic_write_json(config.SEEN_FILE, sorted(self.seen))
    def save_pending(self): atomic_write_json(config.PENDING_WRITES_FILE, self.pending)

    def add_pending(self, entry):
        """Persist a write intent BEFORE the network call. Survives a crash
        between a successful write and the ledger entry: on the next tick or
        process start, _reconcile_pending finds the post via author-feed scan
        and finalizes the bookkeeping instead of double-posting."""
        self.pending.append(entry)
        self.save_pending()

    def remove_pending(self, intent_id):
        before = len(self.pending)
        self.pending = [p for p in self.pending if p.get("intent_id") != intent_id]
        if len(self.pending) != before:
            self.save_pending()

    def list_pending(self):
        return list(self.pending)

    def save_engine(self):
        atomic_write_json(config.ENGINE_STATE_FILE, {
            "tick": self.tick, "anchor_posts": self.anchor_posts,
            "phase": self.phase, "ewma_growth": self.ewma_growth,
            "trends": self.trends, "pinned": self.pinned,
            "active_plan": self.active_plan,
            "last_profile_opt_tick": self.last_profile_opt_tick,
            "last_research_tick": self.last_research_tick,
            "research_interval": self.research_interval,
            "consecutive_empty_ticks": self.consecutive_empty_ticks,
            "bandit": self.bandit, "ledger": self.ledger,
            "keyword_telemetry": self.keyword_telemetry,
            "keyword_map": self.keyword_map,
            "relevance_signals": self.relevance_signals,
            "sectors": self.sectors,
            "topic_angle_examples": self.topic_angle_examples,
            "authorities": self.authorities
        })

    def _compile_relevance_re(self):
        self.relevance_re = re.compile(
            r"\b(" + "|".join(re.escape(s) for s in self.relevance_signals) + r")s?\b",
            re.IGNORECASE,
        )

    def is_relevant_text(self, text: str) -> bool:
        return bool(text) and self.relevance_re.search(text) is not None

    def update(self, dim, value, reward):
        """Bandit posterior update. reward is clamped into [0, 1]; alpha gets
        +reward, beta gets +(1 - reward). For backward compatibility, a bool
        True maps to reward=1.0 and False to reward=0.0, so existing call
        sites that pass a binary success still work. The fractional path
        lets content actions feed a real engagement count (normalized via
        TRACTION_REWARD_CAP) instead of a 0/1 success."""
        if dim not in self.bandit or value not in self.bandit[dim]:
            return
        if reward is True:
            r = 1.0
        elif reward is False:
            r = 0.0
        else:
            r = float(reward)
        if r < 0.0:
            r = 0.0
        elif r > 1.0:
            r = 1.0
        arm = self.bandit[dim][value]
        arm["alpha"] += r
        arm["beta"] += (1.0 - r)
        self.save_bandit()

    def decay(self):
        for dim in self.bandit.values():
            for arm in dim.values():
                arm["alpha"] = 1.0 + (arm["alpha"] - 1.0) * POST_DECAY
                arm["beta"] = 1.0 + (arm["beta"] - 1.0) * POST_DECAY
        self.save_bandit()

    def log_action(self, kind, sector, hook, uri=None, target_did=None,
                   target_handle=None, text=None, learnable=True, keyword=None):
        self.ledger.append({
            "id": uuid.uuid4().hex[:12], "kind": kind, "sector": sector, "hook": hook,
            "uri": uri, "target_did": target_did, "target_handle": target_handle,
            "text": text, "learnable": learnable, "keyword": keyword,
            "ts": time.time(), "hour": int(time.strftime("%H")), "matured": False,
        })
        self.save_ledger()

    def mature_actions(self):
        """Oldest-first learnable actions past their per-kind attribution window."""
        now = time.time()
        due = []
        for a in self.ledger:
            if a["matured"] or not a.get("learnable", True):
                continue
            window = (FOLLOW_ATTRIBUTION_SECONDS if a["kind"] == "follow"
                      else CONTENT_ATTRIBUTION_SECONDS)
            if (now - a["ts"]) >= window:
                due.append(a)
        return sorted(due, key=lambda a: a["ts"])

    def mark_matured(self, action_id):
        for a in self.ledger:
            if a["id"] == action_id:
                a["matured"] = True
                break
        self.save_ledger()

    def already_acted_on(self, key): return key in self.seen
    def mark_seen(self, key):
        self.seen.add(key)
        self.save_seen()

    def recent_content_texts(self, n=5):
        return [a["text"] for a in self.ledger if a.get("text")][-n:]
