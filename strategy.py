import math
import re
import random
import config

def wilson_lower_bound(successes, trials, z=1.96):
    """
    Calculates the Wilson Lower Bound for binary success events.
    successes must be <= trials.
    """
    if trials == 0:
        return 0.0
    p = max(0.0, min(1.0, successes / trials))
    denom = 1 + z * z / trials
    center = p + z * z / (2 * trials)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials)
    return (center - margin) / denom

def hook_strength(text, archetype=None):
    """Cheap proxy for hook quality so we can pick among generated variants
    without an extra API call. Rewards a short, curious, concrete first line.
    Archetype adjusts the length expectation."""
    if not text:
        return -1.0
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0]
    score = 0.0
    fl = len(first)
    if fl <= 90:
        score += 2.0
    elif fl > 140:
        score -= 1.5
    low = first.lower()
    if "?" in first:
        score += 1.0
    if re.match(r"^\s*\d", first) or low.startswith(("most ", "the ", "why ", "here's", "everyone ")):
        score += 1.0
    if any(low.startswith(g) for g in ("in this", "today i", "let's talk", "i want to", "so i")):
        score -= 1.5
    total = len(text)
    if archetype == "one_line_provocation":
        if total <= 120:
            score += config.HOOK_STRENGTH_BONUS
        elif total > 160:
            score -= config.HOOK_STRENGTH_PENALTY
    elif archetype == "single_question":
        if text.strip().endswith("?"):
            score += config.HOOK_STRENGTH_BONUS
        else:
            score -= 1.0
    elif archetype == "mini_thread":
        if total <= 200:
            score += 1.0
    elif archetype == "before_after":
        if 80 <= total <= 240:
            score += 1.0
    else:
        if 120 <= total <= 280:
            score += 1.0
    return score

def select_bandit_arm(store, dimension, choices):
    """
    Thompson sampling action selection based on beta distribution.
    """
    best, pick = -1.0, choices[0]
    for v in choices:
        arm = store.bandit[dimension][v]
        s = random.betavariate(arm["alpha"], arm["beta"])
        if s > best:
            best, pick = s, v
    return pick
