"""Config: constants, paths, logging, and the soul-file loader.

Everything domain-and-identity (name, bio, persona, hooks, niche keywords,
relevance signals) is loaded from soul.yaml at import. Everything else
(file paths, attribution windows, breaker thresholds, growth phases, rate
budgets, safety gate floors) stays in code.

Safety floors (SENSITIVE_PHRASES_FLOOR, SENSITIVE_WORDS_FLOOR, SPAM_PHRASES_FLOOR)
and the scoring thresholds in FollowerEngine._score_follow_target are future
externalization candidates but stay code-enforced for now; a new soul has
not been validated end-to-end and weakening the gates from data is too easy
a foot-gun. The soul MAY add to these lists; it MAY NOT remove from them.
"""
import os
import re
import sys
import hashlib
import logging
from pathlib import Path
from dataclasses import dataclass, field

import yaml


# ==========================================
# LOGGING (shared module logger)
# ==========================================
logger = logging.getLogger("kiloforge")


def configure_logging(level=logging.INFO):
    """Idempotent stderr handler with level + ISO-ish timestamp. Stderr (not
    stdout) so systemd's StandardError=journal captures these even if stdout
    is redirected. Honors $KILOFORGE_LOG_LEVEL if set."""
    env = os.environ.get("KILOFORGE_LOG_LEVEL", "").upper().strip()
    if env in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = getattr(logging, env)
    if logger.handlers:
        logger.setLevel(level)
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


# ==========================================
# PATHS
# ==========================================
_SCRIPT_DIR = Path(__file__).parent.resolve()

# soul.yaml ships with the code (read-only, baked into the deploy), so it
# stays in _SCRIPT_DIR even when the rest of state lives on a mounted disk.
SOUL_FILE = _SCRIPT_DIR / "soul.yaml"


def resolve_state_dir():
    """Where the engine writes its runtime state.

    On Render (or any host with an ephemeral filesystem) the disk that
    survives redeploys must be mounted somewhere and KF_STATE_DIR pointed
    at it. Without that, every deploy resets the bandit, ledger, snapshots,
    seen-set, pending intents, and breaker state, and the agent appears to
    behave like a fresh install each time.

    Locally KF_STATE_DIR is unset, so we fall back to _SCRIPT_DIR; this
    preserves the previous monolithic behavior and keeps existing local
    state files findable.

    Creates the directory if it does not exist (Render mounts an empty
    disk on first boot)."""
    env = os.environ.get("KF_STATE_DIR", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return _SCRIPT_DIR


STATE_DIR = resolve_state_dir()

# All state files live under STATE_DIR. atomic_write_json creates its
# tempfile in the same directory as the target, so the rename is always
# same-filesystem (no EXDEV) on the Render disk too.
BANDIT_STATE_FILE = STATE_DIR / "bandit_state.json"
ACTION_LEDGER_FILE = STATE_DIR / "action_ledger.json"
SNAPSHOT_FILE = STATE_DIR / "account_snapshots.json"
SEEN_FILE = STATE_DIR / "seen_targets.json"
ENGINE_STATE_FILE = STATE_DIR / "engine_state.json"
CIRCUIT_BREAKER_FILE = STATE_DIR / "circuit_breaker.json"
KILL_SWITCH_FILE = STATE_DIR / "engine_status.txt"
PENDING_WRITES_FILE = STATE_DIR / "pending_writes.json"
STATUS_FILE = STATE_DIR / "status.json"
# Niche analyzer output. Distributional only (which archetypes / topic angles
# are trending in the niche). NEVER contains verbatim source text. Read by
# the engine to bias bandit exploration and to seed topic ideas into the
# variant generator. Graceful absence: when missing, generation and the
# bandit run exactly as without it.
NICHE_INSIGHTS_FILE = STATE_DIR / "niche_insights.json"
KEYWORD_TELEMETRY_FILE = STATE_DIR / "keyword_telemetry.json"
# Written by the firehose daemon (background thread). The main engine reads
# this file to learn which of our posts are gaining traction in real-time and
# who is engaging. Graceful absence: when missing, the engine runs without
# network-level signals, exactly as before.
NETWORK_TELEMETRY_FILE = STATE_DIR / "network_telemetry.json"
# Tracks the at:// URI of the curated list the engine creates and populates.
CURATED_LIST_FILE = STATE_DIR / "curated_list.json"


# ==========================================
# SCALARS
# ==========================================
FOLLOWER_TARGET = 100
TICK_INTERVAL = 150                  # 2.5 min/tick, ~24 ticks/hour, human-paced
# Content attribution: 24h is realistic for organic Bluesky reach on a small
# account. The prior 9-minute window meant almost everything matured as a
# failure, so the bandit could not tell good archetypes from bad. Maturation
# is wall-clock based (mature_actions compares now - ts to the window) so it
# survives restarts and ticks that arrive days after the action was logged.
CONTENT_ATTRIBUTION_SECONDS = 24 * 60 * 60
# Human reaction times vary widely. 24 hours matches the content window and
# gives users enough time to see the notification and follow back. If this
# is too short, almost all follows will be marked as failures, training
# the bandit on false negatives.
FOLLOW_ATTRIBUTION_SECONDS = 86400
MAX_LIKES_PER_TICK = 4               # stay human, avoid bot-like like-spray

CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN = 20 * 60
PENDING_GRACE_SECONDS = 60 * 60      # after this, give up on an unresolved intent
STALL_THRESHOLD = 200                  # consecutive active-but-empty ticks before tripping breaker

ANCHOR_POST_TARGET = 3
PROFILE_OPT_MIN_TRIALS = 5           # data-sufficiency trigger for bio rewrite
PROFILE_OPT_COOLDOWN_TICKS = 12
# Per-tick decay applied to bandit alpha/beta. Recalibrated for the 24h
# content window: at TICK_INTERVAL=150s, a 24h window spans ~576 ticks, so
# 0.9999 retains ~94% of the evidence across maturation. The previous 0.99
# would have collapsed signal to ~0.3% before a post matured, erasing every
# reward.
POST_DECAY = 0.9999

# Engagement -> reward normalization for content actions. Reward is
# min(1.0, engagement / TRACTION_REWARD_CAP). Posts with one like still earn
# a fractional reward; posts with real traction earn the full reward. Below
# the cap the relationship is linear so the bandit can still distinguish
# "got 1 like" from "got 6 likes". A fixed cap (vs a recent-baseline) keeps
# this cheap, with no extra API calls at maturation.
TRACTION_REWARD_CAP = 10.0

# Niche analyzer cadence. The analyzer is expensive (a search per sector
# plus one classification AI call) so it runs infrequently. At 150s/tick
# that is ~200 ticks ~= 8h. The first analyzer pass also runs once early
# in the process so the engine is not flying blind for hours after start.
ANALYZER_CADENCE_TICKS = 200
ANALYZER_SAMPLE_PER_SECTOR = 8
ANALYZER_TOTAL_SAMPLE_CAP = 24
# Bandit exploration nudge derived from niche_insights. A hot archetype
# gets up to EXPLORATION_NUDGE_MAX added to its alpha at sampling time
# (NOT persisted to the bandit state). The cap is intentionally small so
# the nudge biases exploration without zeroing other arms. Every archetype
# must still be sampled regularly: the bandit remains the judge for THIS
# account; the analyzer only nudges where to look first.
EXPLORATION_NUDGE_MAX = 0.6
# How many trending topic angles the variant prompt is shown per call.
# Picked at random from the analyzer's pool so different calls see
# different angles, increasing topic variety per generation cycle.
TOPIC_ANGLES_PER_PROMPT = 2

RATE_BUDGETS = {
    "follow": {"capacity": 5,  "refill_per_sec": 1 / 60.0},
    "reply":  {"capacity": 2,  "refill_per_sec": 1 / 200.0},
    "like":   {"capacity": 8,  "refill_per_sec": 1 / 30.0},
    "post":   {"capacity": 2,  "refill_per_sec": 1 / 300.0},
    "quote":  {"capacity": 2,  "refill_per_sec": 1 / 300.0},
}

# Phase-based action mix. Cold start leans on follows; later phases lean on posting.
GROWTH_PHASES = [
    (1,   "cold_start",     {"follow": 0.55, "like": 0.20, "reply": 0.10, "post": 0.10, "quote": 0.05}),
    (5,   "first_proof",    {"follow": 0.45, "like": 0.20, "reply": 0.15, "post": 0.15, "quote": 0.05}),
    (10,  "early_traction", {"follow": 0.35, "like": 0.20, "reply": 0.20, "post": 0.15, "quote": 0.10}),
    (20,  "compound",       {"follow": 0.25, "like": 0.15, "reply": 0.25, "post": 0.20, "quote": 0.15}),
    (50,  "community",      {"follow": 0.15, "like": 0.10, "reply": 0.30, "post": 0.25, "quote": 0.20}),
    (100, "scaling",        {"follow": 0.10, "like": 0.10, "reply": 0.25, "post": 0.35, "quote": 0.20}),
]


# ==========================================
# SAFETY FLOORS (code-enforced, future externalization candidates)
# ==========================================
# These are the minimum gate lists. The soul file may ADD to them but never
# remove or weaken them. The merged effective lists below are what the gates
# actually check.

# Multiword sensitive phrases (substring is fine, they cannot collide with domain terms).
SENSITIVE_PHRASES_FLOOR = [
    "vote for", "left wing", "right wing", "you should take", "cure for",
    "therapy session", "mental illness", "lose weight", "weight loss",
    "bad parent", "bad mom", "bad dad", "your kids will", "real mothers",
    "real fathers", "you can't afford", "broke people", "poor people", "rich people",
    "shut up", "must be nice", "skill issue", "imagine not", "obviously you",
]
# Short risky tokens matched on word boundaries (so "moron" != "oxymoron").
SENSITIVE_WORDS_FLOOR = [
    "democrat", "republican", "liberal", "conservative", "woke", "prayer",
    "bible", "quran", "church", "mosque", "atheist", "diagnosis", "medication",
    "antidepressant", "anorexia", "obesity", "bmi", "idiot", "moron", "loser",
    "stfu", "wtf", "fetish", "bdsm", "sexual", "porn", "kink", "nsfw", "sex",
]
SPAM_PHRASES_FLOOR = [
    "http://", "https://", "buy ", "dm me", "check out my", "great post",
    "sign up", "use my code", "click here", "limited time", "act now", "free trial",
]


# ==========================================
# SOUL
# ==========================================
@dataclass
class Soul:
    """Parsed soul.yaml. All fields are required (the loader fails closed)
    except the extra_* safety additions, which default to empty lists."""
    name: str
    bio: str
    persona: str
    post_hooks: list
    reply_hooks: list[str]
    post_hook_guidance: dict[str, str]
    reply_hook_guidance: dict[str, str]
    keyword_map: dict[str, list[str]]
    relevance_signals: list[str]
    topic_angle_examples: list[str]
    sectors: list[str]
    extra_sensitive_phrases: list[str] = field(default_factory=list)
    extra_sensitive_words: list = field(default_factory=list)
    extra_spam_phrases: list = field(default_factory=list)


class SoulLoadError(Exception):
    """Raised when soul.yaml is missing, malformed, or violates the required
    shape. The loader fails closed: an agent with no persona or empty niche
    keywords would still try to post, so refusing to start is the only
    safe response."""


_REQUIRED_KEYS = {
    "name", "bio", "persona",
    "post_hooks", "reply_hooks",
    "post_hook_guidance", "reply_hook_guidance",
    "sectors", "core_relevance_signals",
}


def load_soul(path=None):
    """Load and validate the soul file. Returns a Soul. Fail-closed: any
    problem raises SoulLoadError rather than degrading to defaults.

    Validation rules:
    - File must exist and parse as YAML.
    - Top level must be a mapping.
    - All _REQUIRED_KEYS must be present and non-empty.
    - Hook-guidance dicts must cover every declared hook.
    - sectors must be a non-empty list of strings.
    - core_relevance_signals must be a non-empty list of strings.
    """
    p = Path(path) if path is not None else SOUL_FILE
    if not p.exists():
        raise SoulLoadError(f"soul file not found at {p}")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise SoulLoadError(f"soul file at {p} failed to parse: {e}")
    if not isinstance(data, dict):
        raise SoulLoadError(f"soul file at {p} must be a YAML mapping at top level")
    missing = _REQUIRED_KEYS - set(data.keys())
    if missing:
        raise SoulLoadError(f"soul file missing required keys: {sorted(missing)}")
    for k in ("name", "bio", "persona"):
        if not (isinstance(data[k], str) and data[k].strip()):
            raise SoulLoadError(f"soul field {k!r} must be a non-empty string")
    for k in ("post_hooks", "reply_hooks", "sectors", "core_relevance_signals", "topic_angle_examples"):
        if k in data:
            v = data[k]
            if not (isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v)):
                raise SoulLoadError(f"soul field {k!r} must be a non-empty list of non-empty strings")
    for k in ("post_hook_guidance", "reply_hook_guidance"):
        if not (isinstance(data[k], dict) and data[k]):
            raise SoulLoadError(f"soul field {k!r} must be a non-empty mapping")
    for hook in data["post_hooks"]:
        if hook not in data["post_hook_guidance"]:
            raise SoulLoadError(f"post_hook_guidance missing entry for {hook!r}")
    for hook in data["reply_hooks"]:
        if hook not in data["reply_hook_guidance"]:
            raise SoulLoadError(f"reply_hook_guidance missing entry for {hook!r}")

    extras = {}
    for k in ("extra_sensitive_phrases", "extra_sensitive_words", "extra_spam_phrases"):
        v = data.get(k, [])
        if v is None:
            v = []
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise SoulLoadError(f"soul field {k!r} must be a list of strings if present")
        extras[k] = v
        
    topic_examples = data.get("topic_angle_examples", ["dark mode contrast", "design tokens vs css vars", "usability test sample sizes"])
    
    return Soul(
        name=data["name"].strip(),
        bio=data["bio"].strip(),
        persona=data["persona"].strip(),
        post_hooks=list(data["post_hooks"]),
        reply_hooks=list(data["reply_hooks"]),
        post_hook_guidance=dict(data["post_hook_guidance"]),
        reply_hook_guidance=dict(data["reply_hook_guidance"]),
        keyword_map={k: [] for k in data["sectors"]},
        relevance_signals=list(data["core_relevance_signals"]),
        topic_angle_examples=list(topic_examples),
        sectors=list(data["sectors"]),
        **extras,
    )


# ==========================================
# SOUL-DERIVED EFFECTIVE CONSTANTS
# ==========================================
# Loaded eagerly at import. If soul.yaml is missing or invalid, the import
# itself raises and the process refuses to start. This is the fail-closed
# behavior the spec requires.
SOUL = load_soul()

NAME_TEXT = SOUL.name
BIO_TEXT = SOUL.bio
PERSONA = SOUL.persona
POST_HOOKS = SOUL.post_hooks
REPLY_HOOKS = SOUL.reply_hooks
POST_HOOK_GUIDANCE = SOUL.post_hook_guidance
REPLY_HOOK_GUIDANCE = SOUL.reply_hook_guidance
KEYWORD_MAP = SOUL.keyword_map
RELEVANCE_SIGNALS = SOUL.relevance_signals
TOPIC_ANGLE_EXAMPLES = SOUL.topic_angle_examples

# Sector identifiers come from sectors list.
SECTORS = SOUL.sectors

# Word-boundary matcher built from the merged relevance signals.
# This might be recompiled by engine.py as it learns new signals.
RELEVANCE_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in RELEVANCE_SIGNALS) + r")s?\b",
    re.IGNORECASE,
)

# Effective safety lists: floor UNION soul extras. Union (not replace) so a
# soul file can only broaden the gates, never narrow them.
SENSITIVE_PHRASES = sorted(set(SENSITIVE_PHRASES_FLOOR) | set(SOUL.extra_sensitive_phrases))
SENSITIVE_WORDS = sorted(set(SENSITIVE_WORDS_FLOOR) | set(SOUL.extra_sensitive_words))
SPAM_PHRASES = sorted(set(SPAM_PHRASES_FLOOR) | set(SOUL.extra_spam_phrases))


# ==========================================
# SMALL UTILITIES
# ==========================================
def content_hash(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def is_relevant_text(text: str) -> bool:
    return bool(text) and RELEVANCE_RE.search(text) is not None
