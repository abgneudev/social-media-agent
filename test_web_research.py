import logging
import config
config.configure_logging(logging.DEBUG)

import web_research
from store import Store, atomic_write_json
import os
import json

print("Starting test...")
def dummy_ai(prompt):
    print("AI Prompt received:", len(prompt), "chars")
    # Simulate first phase (query generation)
    if "formulate TWO Google Search queries" in prompt:
        return '''
        {
          "tactics_query": "how to grow design tech audience bluesky 2026",
          "inspiration_query": "high quality UX cognitive load guides"
        }
        '''
    
    # Simulate second phase (extraction)
    return '''
    {
      "is_credible": true,
      "category": "tactics",
      "experimental_hooks": [{"hook_name": "empirical_insight", "guidance": "Share a mathematical platform insight."}],
      "strategic_guidance": ["Keep your tone analytical and sharp."],
      "trending_topics": ["Bento box UI", "AI generated interfaces"],
      "curated_links": [{"url": "https://example.com/ux-guide", "title": "UX Guide", "summary": "Great guide"}]
    }
    '''

# Inject fake web_insights directly to bypass Serper failing due to missing key for the Store check
fake_insights = {
    "experimental_hooks": [{"hook_name": "myth_buster", "guidance": "Bust a UX myth."}],
    "strategic_guidance": ["Keep your tone analytical and sharp."],
    "trending_topics": ["Bento box UI", "AI generated interfaces"],
    "curated_links": [{"url": "https://example.com/ux-guide", "title": "UX Guide", "summary": "Great guide"}]
}
atomic_write_json(config.STATE_DIR / "web_insights.json", fake_insights)

print("Initializing store...")
store = Store()
empirical_data = {
    "bandit": store.bandit,
    "trends": {"visual_communication": ["UI trends", "Figma plugins"]},
    "followers": 7
}

print("Running daily research...")
# We expect this to fail smoothly since we don't have SERPER_API_KEY, or it will 
# use Serper without a key and fail. Let's see what happens.
res = web_research.run_daily_research(dummy_ai, empirical_data)
print("Research result:", res)
