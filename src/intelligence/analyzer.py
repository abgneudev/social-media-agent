"""Niche analyzer.

Samples high-engagement posts from the niche, classifies each by the
archetype vocabulary (same labels the bandit pulls from store.soul.post_hooks), and
records which archetypes and which topic angles are trending RIGHT NOW
for the niche.

What this analyzer is NOT:
  - It does not produce a single style or voice guidance block. The
    persona owns voice and is untouched.
  - It does not return verbatim sentences, quotes, or specific phrasings
    from the sampled posts. Doing so would collapse generation onto
    imitated language, which is the opposite of what Commit 1's archetypes
    are trying to produce. The output is strictly DISTRIBUTIONAL: counts
    by archetype and short noun-phrase topic angles.

What the engine does with the output:
  - Bias bandit exploration by adding a small alpha bump to hot archetypes
    at sampling time. The bump is small and capped; every archetype must
    still get sampled. The bandit remains the judge of what works for THIS
    account; the analyzer only shifts where the bandit looks first.
  - Feed rotating topic angles into the variant prompt so different drafts
    see different topic ideas. This INCREASES variety. It must not become
    a single guidance block every draft echoes.

Caveats encoded in the classification prompt:
  - What works for a high-follower leader does NOT transfer wholesale to
    a cold account: a big account can post low-effort takes and still win
    on existing reach. We extract transferable archetype/topic structure
    only; never "post less effort like the big account did."

Cadence: engine calls run() every ANALYZER_CADENCE_TICKS, skipping when
the breaker is open or the kill switch is engaged. Graceful absence: if
the blob does not exist (first run, file removed, write failed), the rest
of the engine behaves exactly as it did without the analyzer.
"""
import json
import time
import random

from core import config
from core.config import (
    logger,  
    ANALYZER_SAMPLE_PER_SECTOR, ANALYZER_TOTAL_SAMPLE_CAP,
)
from core.store import atomic_write_json, load_json


# Hard cap on each topic angle string. Keeps the analyzer output
# distributional (short noun phrases) rather than copyable sentences.
TOPIC_ANGLE_CHAR_CAP = 60


def _post_engagement(p):
    return ((getattr(p, "like_count", 0) or 0)
            + (getattr(p, "repost_count", 0) or 0)
            + (getattr(p, "reply_count", 0) or 0))


def _sample_high_engagement_posts(store, net):
    """Pull recent posts from the niche, rank by engagement, sample the
    top from each sector and combine. Reuses the existing search_posts
    path; no new bulk fetches. Returns a list of (sector, text, engagement)
    tuples capped at ANALYZER_TOTAL_SAMPLE_CAP."""
    pool = []
    our_did = getattr(net, "did", None)
    for sector in store.sectors:
        keywords = store.keyword_map.get(sector, [])
        if not keywords:
            continue
        keyword = random.choice(keywords)
        try:
            posts = net.search_posts(keyword, limit=25)
        except Exception as e:
            logger.warning(f"   [ANALYZER] search failed for {sector}: {e}")
            continue
        scored = []
        for p in posts:
            text = getattr(getattr(p, "record", None), "text", None)
            if not text:
                continue
            author_did = getattr(getattr(p, "author", None), "did", None)
            if our_did and author_did == our_did:
                continue
            scored.append((_post_engagement(p), sector, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        for eng, s, t in scored[:ANALYZER_SAMPLE_PER_SECTOR]:
            pool.append((s, t, eng))
    pool.sort(key=lambda x: x[2], reverse=True)
    return pool[:ANALYZER_TOTAL_SAMPLE_CAP]


def _classify_prompt(store, samples, topic_angle_examples):
    """Build the classification prompt. The schema requires per-sample
    archetype + a short noun-phrase topic_angle. We forbid copying source
    sentences explicitly and cap angle length so the model cannot smuggle
    a paste-back."""
    archetypes = ", ".join(store.soul.post_hooks)
    body = "\n".join(f"[{i}] {t}" for i, (_, t, _) in enumerate(samples))
    examples_str = ", ".join(repr(ex) for ex in topic_angle_examples) if topic_angle_examples else "'design architecture', 'heuristic breakdown'"
    return (
        f"You are classifying recent high-engagement posts from a niche to "
        f"learn what shapes and topic ideas are getting traction RIGHT NOW. "
        f"Your output is STRUCTURAL ONLY. Do not quote or paraphrase any "
        f"sentence from the inputs.\n\n"
        f"For each post, return:\n"
        f"  archetype: ONE of these labels (closest fit): {archetypes}\n"
        f"  topic_angle: a SHORT NOUN PHRASE (max {TOPIC_ANGLE_CHAR_CAP} chars, "
        f"under 5 words, no punctuation, no full sentence) naming the topic the "
        f"post is about. Examples of good topic_angles: {examples_str}. Examples of "
        f"BAD topic_angles (do not produce these): full sentences, direct quotes, "
        f"first-person verbs, anything copied from the input.\n\n"
        f"Caveat to apply when interpreting: high-follower accounts can post "
        f"low-effort takes and still win on existing reach. Capture the "
        f"transferable archetype/topic structure, not 'post less effort'.\n\n"
        f"Posts:\n{body}\n\n"
        f'Respond strictly as JSON: {{"classifications": [{{"archetype": "...", '
        f'"topic_angle": "..."}}]}}'
    )


def _classify(store, ai_generate, samples, topic_angle_examples):
    """Call the AI to classify the sampled posts. Returns a list aligned
    with `samples`. Bad rows (missing archetype, oversize topic_angle) are
    dropped so a partial response is still useful."""
    if not samples:
        return []
    raw = ai_generate(_classify_prompt(store, samples, topic_angle_examples))
    if not raw:
        return []
    try:
        parsed = json.loads(raw).get("classifications", [])
    except Exception as e:
        logger.warning(f"   [ANALYZER] classify JSON malformed: {e}")
        return []
    out = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        arch = row.get("archetype")
        angle = row.get("topic_angle")
        if arch not in store.soul.post_hooks:
            continue
        if not isinstance(angle, str) or not angle.strip():
            continue
        angle = angle.strip()
        if len(angle) > TOPIC_ANGLE_CHAR_CAP:
            angle = angle[:TOPIC_ANGLE_CHAR_CAP].rstrip()
        # Defensive: drop anything that looks like a quoted full sentence.
        if angle.endswith(".") or angle.endswith("!") or angle.endswith("?"):
            angle = angle.rstrip(".!?").strip()
        if not angle:
            continue
        out.append({"archetype": arch, "topic_angle": angle})
    return out


def run(net, ai_generate,  topic_angle_examples, sectors):
    """One analyzer pass. ai_generate is a callable that takes a prompt
    string and returns a JSON string (the engine's _generate wrapper, with
    its existing fault handling). Writes the niche_insights blob atomically.

    Returns the written blob on success, None on failure. A failure (no
    samples, AI down, write error) leaves any existing blob in place; the
    engine reads whatever is on disk and gracefully degrades to no nudges
    if the read returns nothing."""
    samples = _sample_high_engagement_posts(store, net)
    if not samples:
        logger.info("[ANALYZER] no samples to classify; leaving prior insights.")
        return None
    classifications = _classify(store, llm.generate, samples, store.topic_angle_examples)
    if not classifications:
        logger.info("[ANALYZER] no classifications; leaving prior insights.")
        return None
    archetype_traction = {a: 0 for a in store.soul.post_hooks}
    topic_angles = []
    for row in classifications:
        archetype_traction[row["archetype"]] = archetype_traction.get(row["archetype"], 0) + 1
        if row["topic_angle"] not in topic_angles:
            topic_angles.append(row["topic_angle"])
    blob = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sample_size": len(classifications),
        "archetype_traction": archetype_traction,
        "topic_angles": topic_angles,
    }
    try:
        atomic_write_json(config.NICHE_INSIGHTS_FILE, blob)
        top_arch = max(archetype_traction, key=lambda a: archetype_traction[a])
        logger.info(f"[ANALYZER] wrote insights: {len(classifications)} samples, "
                    f"{len(topic_angles)} topic angles, top archetype={top_arch}")
    except Exception as e:
        logger.warning(f"   [ANALYZER] write failed: {e}")
        return None
    return blob


def load_insights():
    """Read the niche_insights blob. Returns None if absent or unreadable
    so callers can gracefully degrade to no-nudge behavior."""
    blob = load_json(config.NICHE_INSIGHTS_FILE, None)
    if not isinstance(blob, dict):
        return None
    if "archetype_traction" not in blob:
        return None
    return blob


def archetype_nudges(soul, blob, max_nudge):
    """Convert archetype_traction counts into per-arm alpha bumps in
    [0, max_nudge]. The most-represented archetype gets the full bump,
    others scale linearly with their share. Missing archetypes get 0.

    The bump is small and capped: it biases Thompson sampling toward hot
    archetypes without zeroing other arms. Every archetype must still be
    sampled in expectation, which is what the test guarantees."""
    if not blob:
        return {a: 0.0 for a in soul.post_hooks}
    traction = blob.get("archetype_traction") or {}
    max_count = max((traction.get(a, 0) for a in soul.post_hooks), default=0)
    if max_count <= 0:
        return {a: 0.0 for a in soul.post_hooks}
    return {a: (traction.get(a, 0) / max_count) * max_nudge for a in soul.post_hooks}


def topic_angles_for_prompt(blob, k):
    """Pick k random topic angles to seed into the variant prompt. Picking
    a different subset per call rotates the pool, so different generation
    calls see different angles. Returns [] if there are no angles."""
    if not blob:
        return []
    pool = blob.get("topic_angles") or []
    if not pool:
        return []
    k = min(k, len(pool))
    return random.sample(pool, k)
