import json
from intelligence import meta_critic
from core import config

# Mock ai_generate function
def mock_ai_generate(prompt):
    print("----- SENT PROMPT TO LLM -----")
    print(prompt[:1000] + "...\n")
    # Return a mocked JSON response
    return json.dumps({
        "persona_override": "Be more aggressive and focus on 'myth_bust' style posts.",
        "extra_hooks": [
            {"name": "brutal_teardown", "guidance": "Tear down a bad design without holding back."}
        ]
    })

# Mock bandit data where 'myth_bust' is massively outperforming others
mock_bandit = {
    "post_hook": {
        "myth_bust": {"alpha": 100.0, "beta": 10.0},        # EV = 0.909
        "generic_advice": {"alpha": 1.0, "beta": 50.0},     # EV = 0.019
        "how_to": {"alpha": 10.0, "beta": 10.0}             # EV = 0.500
    },
    "sector": {
        "design_systems": {"alpha": 20.0, "beta": 2.0},
        "css": {"alpha": 2.0, "beta": 20.0}
    }
}

print("Running Meta-Critic Evaluation...")
success = meta_critic.evaluate_strategy(mock_ai_generate, mock_bandit)
print(f"Success: {success}")

if success:
    print("\nReloading dynamic strategy...")
    config.reload_dynamic_strategy()
    
    print("\n--- NEW PERSONA ---")
    print(config.PERSONA)
    
    print("\n--- NEW HOOKS ---")
    print([hook for hook in config.POST_HOOKS if hook in ['brutal_teardown']])
