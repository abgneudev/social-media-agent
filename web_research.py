"""Web Research: Autonomous discovery of strategy and trends.

Once daily, the engine calls run_daily_research(). This module takes the
agent's empirical platform data (bandit state, followers, trends) and uses
an LLM to formulate targeted Google search queries. It evaluates the credibility
and category of fetched articles and extracts hooks, guidance, and links.
"""
import json
import time

import config
from config import logger, STATE_DIR
from store import atomic_write_json, load_json
import serper

WEB_INSIGHTS_FILE = STATE_DIR / "web_insights.json"

def _generate_queries(ai_generate, empirical_data):
    """Generates dynamic search queries based on the agent's current empirical state."""
    prompt = (
        f"You are the Lead Strategist for an autonomous social media engine operating in these sectors: {', '.join(config.SECTORS)}.\n\n"
        f"Based on the agent's current empirical platform data, you must determine your research direction for this cycle and formulate 2-3 Google Search queries to execute your strategy.\n\n"
        f"AGENT'S EMPIRICAL DATA:\n"
        f"{json.dumps(empirical_data, indent=2)}\n\n"
        f"CRITICAL RULE ON CREDIBILITY: You MUST append `site:` operators to your queries to restrict the search ONLY to highly credible known organizations, companies, or institutions relevant to the agent's sectors. Use your knowledge to select the most authoritative domains for the specific topic you are querying. Do NOT perform generic open web searches.\n\n"
        f"DIRECTION AUTONOMY & BOTTLENECK DIAGNOSIS: You have full autonomy to decide what the agent needs to learn right now. However, you MUST mathematically diagnose your growth bottlenecks first. Look at the ratio of followers to anchor_posts in the empirical data. If that ratio is low, your bottleneck is NOT content quality—it is Platform Distribution. In that case, you MUST prioritize researching the algorithmic mechanics, feed ranking rules, and network graph dynamics of the Bluesky platform. If growth is healthy, prioritize Content Inspiration to fuel future posts across your sectors. State your diagnostic reasoning in your direction, and formulate queries to attack the bottleneck.\n\n"
        f"Respond STRICTLY as JSON with exactly two keys:\n"
        f"{{\n"
        f'  "research_direction": "Brief explanation of your strategy for this research cycle",\n'
        f'  "queries": ["query 1", "query 2"]\n'
        f"}}"
    )
    raw = ai_generate(prompt)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"   [WEB RESEARCH] Failed to parse generated queries: {e}")
        return None

def _research_prompt(article_text, source_link, source_title):
    return (
        f"You are a strict, high-standard Social Strategist and Researcher managing a portfolio in these sectors: {', '.join(config.SECTORS)}.\n\n"
        f"Analyze the following article.\n\n"
        f"ARTICLE: {source_title}\nURL: {source_link}\n\n"
        f"CONTENT:\n{article_text[:8000]}\n\n"
        f"RULES FOR EXTRACTION:\n"
        f"1. ROUTING & CREDIBILITY: Determine if the article is highly credible. If it's generic fluff, spam, or low quality, set 'is_credible' to false and leave all lists empty. If credible, classify its primary value as either 'tactics' (growth, platform strategy) or 'inspiration' (content ideas to share).\n"
        f"2. Experimental Hooks: Invent 1-2 new post archetypes inspired by the article's structure or message (e.g., 'myth_bust'). Provide the hook name and a 1-sentence guidance on how to use it.\n"
        f"3. Strategic Guidance: Extract 1-2 specific, actionable pieces of advice on voice, tone, or platform strategy.\n"
        f"4. Trending Topics: Extract 1-2 trending topics (noun phrases max 5 words).\n"
        f"5. Factual Knowledge: Extract 1-3 hard empirical facts, statistics, or case-study results from the article. Each fact must be a complete sentence that cites the article context.\n"
        f"6. Curated Links: If the article itself is highly credible and worth sharing with the agent's audience, include it. Otherwise, leave empty.\n\n"
        f"Respond STRICTLY as JSON:\n"
        f"{{\n"
        f'  "is_credible": true,\n'
        f'  "category": "tactics",\n'
        f'  "experimental_hooks": [{{"hook_name": "...", "guidance": "..."}}],\n'
        f'  "strategic_guidance": ["..."],\n'
        f'  "trending_topics": ["..."],\n'
        f'  "factual_knowledge": ["..."],\n'
        f'  "curated_links": [{{"url": "{source_link}", "title": "{source_title}", "summary": "..."}}]\n'
        f"}}"
    )

def run_daily_research(ai_generate, empirical_data):
    """Run a single pass of dynamic web research.
    Returns the parsed blob on success, None on failure.
    """
    logger.info("[WEB RESEARCH] Generating dynamic search queries based on empirical data...")
    queries_obj = _generate_queries(ai_generate, empirical_data)
    
    if not queries_obj:
        logger.warning("[WEB RESEARCH] Failed to generate queries.")
        return None
        
    direction = queries_obj.get("research_direction", "unknown")
    logger.info(f"   [WEB RESEARCH] Direction set: {direction}")
    
    queries = queries_obj.get("queries", [])
    results = []
    
    for q in queries:
        if q:
            logger.info(f"   [WEB RESEARCH] Searching: {q}")
            r = serper.search_web_organic(q, num_results=2)
            if r:
                results.extend(r)
                
    if not results:
        logger.warning(f"   [WEB RESEARCH] No organic results found for queries.")
        return None
        
    # Deduplicate results by link
    seen_urls = set()
    unique_results = []
    for res in results:
        url = res.get("link")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(res)
            
    all_hooks = []
    all_guidance = []
    all_topics = []
    all_links = []
    
    for res in unique_results:
        url = res.get("link")
        title = res.get("title", "")
            
        logger.info(f"   [WEB RESEARCH] Fetching markdown for: {title} ({url})")
        text = serper.fetch_web_markdown(url)
        if not text or len(text) < 500:
            logger.info("   [WEB RESEARCH] Content too short or failed to fetch. Skipping.")
            continue
            
        raw = ai_generate(_research_prompt(text, url, title))
        if not raw:
            continue
            
        try:
            parsed = json.loads(raw)
            if not parsed.get("is_credible"):
                logger.info("   [WEB RESEARCH] Evaluated as not credible. Skipping.")
                continue
                
            cat = parsed.get("category", "unknown")
            logger.info(f"   [WEB RESEARCH] Evaluated as highly credible ({cat}). Extracting insights.")
                
            if parsed.get("experimental_hooks"):
                all_hooks.extend(parsed["experimental_hooks"])
            if parsed.get("strategic_guidance"):
                all_guidance.extend(parsed["strategic_guidance"])
            if parsed.get("trending_topics"):
                all_topics.extend(parsed["trending_topics"])
            if parsed.get("curated_links"):
                all_links.extend(parsed["curated_links"])
                
            import memory
            if parsed.get("factual_knowledge"):
                for fact in parsed["factual_knowledge"]:
                    memory.save_knowledge(cat, fact)
                    
        except Exception as e:
            logger.warning(f"   [WEB RESEARCH] JSON parse failed: {e}")
            continue
            
    if not any([all_hooks, all_guidance, all_topics, all_links]):
        logger.info("[WEB RESEARCH] Nothing actionable extracted.")
        return None
        
    blob = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "experimental_hooks": all_hooks,
        "strategic_guidance": all_guidance,
        "trending_topics": all_topics,
        "curated_links": all_links
    }
    
    try:
        atomic_write_json(WEB_INSIGHTS_FILE, blob)
        logger.info(f"[WEB RESEARCH] Saved insights: {len(all_hooks)} hooks, {len(all_links)} links.")
        return blob
    except Exception as e:
        logger.error(f"   [WEB RESEARCH] Failed to write insights file: {e}")
        return None

def load_insights():
    """Load the latest web insights."""
    return load_json(WEB_INSIGHTS_FILE, None)
