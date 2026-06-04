"""Meta-Critic: Autonomous Strategy Evolution.

Evaluates the agent's mathematical performance from the bandit,
prompts the LLM to act as a strategy critic, and outputs JSON
overrides to the agent's base persona and hooks.
"""
import json
import time

import config
from config import logger, STATE_DIR
from store import load_json, atomic_write_json

STRATEGY_FILE = STATE_DIR / "dynamic_strategy.json"

def _compute_ev(bandit_dict):
    """Computes Expected Value (alpha / (alpha + beta)) for items in a bandit dimension."""
    evs = {}
    for item, params in bandit_dict.items():
        alpha = params.get("alpha", 1.0)
        beta = params.get("beta", 1.0)
        evs[item] = alpha / (alpha + beta)
    
    # Sort descending
    return dict(sorted(evs.items(), key=lambda x: x[1], reverse=True))

def evaluate_strategy(ai_generate, bandit):
    """
    Computes performance math, feeds it to the LLM, and saves the new strategy.
    ai_generate: function that takes a string prompt and returns a string response.
    bandit: the agent's store.bandit dictionary.
    """
    logger.info("[META-CRITIC] Evaluating strategy based on empirical performance...")
    
    if not bandit or "post_hook" not in bandit:
        logger.warning("[META-CRITIC] Bandit state empty or invalid. Skipping.")
        return False
        
    post_hook_evs = _compute_ev(bandit["post_hook"])
    sector_evs = _compute_ev(bandit.get("sector", {}))
    
    # Format the data for the LLM
    math_context = "POST HOOK EXPECTED VALUE (Higher is better):\n"
    for hook, ev in post_hook_evs.items():
        math_context += f"- {hook}: {ev:.3f}\n"
        
    math_context += "\nSECTOR EXPECTED VALUE (Higher is better):\n"
    for sec, ev in sector_evs.items():
        math_context += f"- {sec}: {ev:.3f}\n"

    prompt = (
        f"You are the Meta-Critic for an autonomous social media agent.\n"
        f"Your job is to review the mathematical performance of the agent's recent posts, "
        f"and output a strategic pivot to improve growth.\n\n"
        f"AGENT BASE PERSONA:\n{config.PERSONA}\n\n"
        f"EMPIRICAL PERFORMANCE DATA:\n{math_context}\n\n"
        f"INSTRUCTIONS:\n"
        f"1. Identify the top performing hooks and the worst performing hooks.\n"
        f"2. Generate a 'persona_override'. This is a 2-3 sentence paragraph that will be APPENDED to the agent's base persona. It should explicitly tell the agent to lean into the tone/style of the winning hooks and avoid the losing hooks.\n"
        f"3. Generate 1 or 2 entirely new 'experimental_hooks' (with a short guidance string) inspired by the top performers.\n\n"
        f"OUTPUT FORMAT (Strictly JSON):\n"
        f"{{\n"
        f'  "persona_override": "...",\n'
        f'  "extra_hooks": [\n'
        f'    {{"name": "...", "guidance": "..."}}\n'
        f'  ]\n'
        f"}}"
    )
    
    raw = ai_generate(prompt)
    if not raw:
        logger.warning("[META-CRITIC] LLM returned empty response.")
        return False
        
    try:
        parsed = json.loads(raw)
        
        # Save to dynamic strategy
        strategy = {
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "persona_override": parsed.get("persona_override", ""),
            "extra_hooks": parsed.get("extra_hooks", [])
        }
        
        atomic_write_json(STRATEGY_FILE, strategy)
        logger.info(f"[META-CRITIC] Strategy updated: appended {len(strategy['extra_hooks'])} new hooks.")
        return True
    except Exception as e:
        logger.warning(f"[META-CRITIC] Failed to parse JSON or save strategy: {e}")
        return False

def load_dynamic_strategy():
    return load_json(STRATEGY_FILE, {})
