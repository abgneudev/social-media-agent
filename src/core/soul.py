import yaml
import re
from pathlib import Path
from dataclasses import dataclass, field

class SoulLoadError(Exception):
    """Raised when soul.yaml is missing, malformed, or violates the required
    shape. The loader fails closed: an agent with no persona or empty niche
    keywords would still try to post, so refusing to start is the only
    safe response."""

@dataclass
class Soul:
    """Parsed soul.yaml. All fields are required (the loader fails closed)
    except the extra_* safety additions, which default to empty lists."""
    name: str
    bio: str
    persona: str
    post_hooks: list[str]
    reply_hooks: list[str]
    post_hook_guidance: dict[str, str]
    reply_hook_guidance: dict[str, str]
    keyword_map: dict[str, list[str]]
    relevance_signals: list[str]
    topic_angle_examples: list[str]
    sectors: list[str]
    extra_sensitive_phrases: list[str] = field(default_factory=list)
    extra_sensitive_words: list[str] = field(default_factory=list)
    extra_spam_phrases: list[str] = field(default_factory=list)

    # Derived properties that will be computed once
    _relevance_re = None
    _sensitive_phrases = None
    _sensitive_words = None
    _spam_phrases = None

    def get_relevance_re(self):
        if self._relevance_re is None:
            self._relevance_re = re.compile(
                r"\b(" + "|".join(re.escape(s) for s in self.relevance_signals) + r")s?\b",
                re.IGNORECASE,
            ) if self.relevance_signals else re.compile(r"a^") # matches nothing
        return self._relevance_re

    def get_sensitive_phrases(self, floor_phrases: list[str]) -> list[str]:
        if self._sensitive_phrases is None:
            self._sensitive_phrases = sorted(set(floor_phrases) | set(self.extra_sensitive_phrases))
        return self._sensitive_phrases

    def get_sensitive_words(self, floor_words: list[str]) -> list[str]:
        if self._sensitive_words is None:
            self._sensitive_words = sorted(set(floor_words) | set(self.extra_sensitive_words))
        return self._sensitive_words

    def get_spam_phrases(self, floor_spam: list[str]) -> list[str]:
        if self._spam_phrases is None:
            self._spam_phrases = sorted(set(floor_spam) | set(self.extra_spam_phrases))
        return self._spam_phrases

    def is_relevant_text(self, text: str) -> bool:
        return bool(text) and self.get_relevance_re().search(text) is not None

def load_soul(path) -> Soul:
    """Load and validate the soul file. Returns a Soul. Fail-closed: any
    problem raises SoulLoadError rather than degrading to defaults."""
    p = Path(path)
    if not p.exists():
        raise SoulLoadError(f"soul file not found at {p}")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise SoulLoadError(f"soul file at {p} failed to parse: {e}")
    if not isinstance(data, dict):
        raise SoulLoadError(f"soul file at {p} must be a YAML mapping at top level")
    missing = {"name", "bio", "persona", "post_hooks", "reply_hooks", "post_hook_guidance", "reply_hook_guidance"} - set(data.keys())
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
        
    topic_examples = data.get("topic_angle_examples", [])
    sectors = data.get("sectors", [])
    relevance_signals = data.get("core_relevance_signals", [])
    
    return Soul(
        name=data["name"].strip(),
        bio=data["bio"].strip(),
        persona=data["persona"].strip(),
        post_hooks=list(data["post_hooks"]),
        reply_hooks=list(data["reply_hooks"]),
        post_hook_guidance=dict(data["post_hook_guidance"]),
        reply_hook_guidance=dict(data["reply_hook_guidance"]),
        keyword_map={k: [] for k in sectors},
        relevance_signals=list(relevance_signals),
        topic_angle_examples=list(topic_examples),
        sectors=list(sectors),
        **extras,
    )

import hashlib
def content_hash(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:16]
