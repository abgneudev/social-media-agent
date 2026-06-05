"""Web Research: Autonomous discovery of strategy and trends.

Once daily, the engine calls run_daily_research(). This module takes the
agent's empirical platform data (bandit state, followers, trends) and uses
an LLM to formulate targeted Google search queries. It evaluates the credibility
and category of fetched articles and extracts hooks, guidance, and links.
"""
import json
import time

from core import config
from core.config import logger, STATE_DIR
from core.store import atomic_write_json, load_json
from clients import serper
from intelligence import memory

WEB_INSIGHTS_FILE = STATE_DIR / "web_insights.json"

def _generate_queries(ai_generate, empirical_data, strategist_direction, sectors):
    """Generates dynamic search queries based on the agent's current empirical state."""
    
    direction_prompt = ""
    if strategist_direction:
        direction_prompt = f"THE STRATEGIST HAS GIVEN YOU A DIRECTIVE FOR THIS CYCLE:\n\"{strategist_direction}\"\nYour queries MUST fulfill this directive.\n\n"
        
    prompt = (
        f"You are the Lead Researcher for an autonomous social media engine operating in these sectors: {', '.join(sectors)}.\n"
        f"Your overarching objective is to conduct web research exactly as requested by the Strategist.\n\n"
        f"{direction_prompt}"
        f"AGENT'S EMPIRICAL DATA:\n"
        f"{json.dumps(empirical_data, indent=2)}\n\n"
        f"CRITICAL RULE ON CREDIBILITY: Generally, you MUST append `site:` operators to your queries to restrict the search ONLY to highly credible known organizations relevant to the agent's sectors. HOWEVER, if the Strategist's directive explicitly requires searching for algorithmic growth, distribution tactics, or topics outside these sectors, you MUST bypass this restriction and search the open web as needed to fulfill the Strategist's exact intent.\n\n"
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

def _research_prompt(article_text, source_link, source_title, sectors):
    return (
        f"You are a strict, high-standard Social Strategist and Researcher managing a portfolio in these sectors: {', '.join(sectors)}.\n"
        f"Your ultimate goal is to extract insights that directly drive audience growth, engagement, and follower acquisition on the Bluesky social network. Ignore anything that does not serve this purpose.\n\n"
        f"Analyze the following article.\n\n"
        f"ARTICLE: {source_title}\nURL: {source_link}\n\n"
        f"CONTENT:\n{article_text[:8000]}\n\n"
        f"RULES FOR EXTRACTION:\n"
        f"1. ROUTING & CREDIBILITY: Determine if the article is highly credible. If it's generic fluff, spam, or low quality, set 'is_credible' to false and leave 'dynamic_schemas' empty. If credible, classify its primary value as either 'tactics' (growth, platform strategy) or 'inspiration' (content ideas to share).\n"
        f"2. Dynamic Schemas: Instead of following a rigid schema, dynamically invent and extract whatever structured schemas are most valuable for the Strategist based on this article. You can extract macro-trends, target communities (e.g. subreddits, discords), new sociological phenomena, experimental post hooks, or actionable strategic guidance.\n"
        f"3. Curated Links: If you find an amazing article worth sharing, you MAY create a 'curated_links' array containing objects with 'url', 'title', and 'summary'.\n"
        f"4. Factual Knowledge: If you find hard empirical facts or case studies, you MAY create a 'factual_knowledge' array of strings.\n\n"
        f"Respond STRICTLY as JSON with this exact structure, but you define the keys inside 'dynamic_schemas':\n"
        f"{{\n"
        f'  "is_credible": true,\n'
        f'  "category": "tactics",\n'
        f'  "dynamic_schemas": {{\n'
        f'    "niche_target_communities": ["..."],\n'
        f'    "sociological_trends": ["..."],\n'
        f'    "experimental_hooks": [{{"hook_name": "...", "guidance": "..."}}],\n'
        f'    "curated_links": [{{"url": "{source_link}", "title": "{source_title}", "summary": "..."}}],\n'
        f'    "factual_knowledge": ["..."]\n'
        f'  }}\n'
        f"}}"
    )

def run_daily_research(ai_generate, empirical_data, strategist_direction="", sectors=[]):
    """Main entry point for daily research. Fetches queries and extracts insights."""
    logger.info("[WEB RESEARCH] Generating dynamic search queries based on empirical data...")
    
    recent_queries = memory.recall_knowledge("past_query", limit=20)
    if recent_queries:
        empirical_data["RECENTLY SEARCHED QUERIES"] = recent_queries

    queries_obj = _generate_queries(ai_generate, empirical_data, strategist_direction, sectors)
    
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
            memory.save_knowledge("past_query", q)
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
            
    merged_schemas = {}
    
    for res in unique_results:
        url = res.get("link")
        title = res.get("title", "")
            
        logger.info(f"   [WEB RESEARCH] Fetching markdown for: {title} ({url})")
        text = serper.fetch_web_markdown(url)
        if not text or len(text) < 500:
            logger.info("   [WEB RESEARCH] Content too short or failed to fetch. Skipping.")
            continue
            
        raw = ai_generate(_research_prompt(text, url, title, sectors))
        if not raw:
            continue
            
        try:
            parsed = json.loads(raw)
            if not parsed.get("is_credible"):
                logger.info("   [WEB RESEARCH] Evaluated as not credible. Skipping.")
                continue
                
            cat = parsed.get("category", "unknown")
            logger.info(f"   [WEB RESEARCH] Evaluated as highly credible ({cat}). Extracting dynamic schemas.")
                
            dynamic_schemas = parsed.get("dynamic_schemas", {})
            for key, items in dynamic_schemas.items():
                if not isinstance(items, list):
                    continue
                if key not in merged_schemas:
                    merged_schemas[key] = []
                merged_schemas[key].extend(items)
                
            # Legacy fact-saving to memory
            if "factual_knowledge" in dynamic_schemas:
                for fact in dynamic_schemas["factual_knowledge"]:
                    memory.save_knowledge(cat, fact)
                    
        except Exception as e:
            logger.warning(f"   [WEB RESEARCH] JSON parse failed: {e}")
            continue
            
    if not merged_schemas:
        logger.info("[WEB RESEARCH] Nothing actionable extracted.")
        return None
        
    blob = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S")
    }
    blob.update(merged_schemas)
    
    try:
        atomic_write_json(config.WEB_INSIGHTS_FILE, blob)
        num_schemas = len(merged_schemas)
        num_links = len(merged_schemas.get("curated_links", []))
        logger.info(f"[WEB RESEARCH] Saved insights: {num_schemas} dynamic schemas, {num_links} curated links.")
        return blob
    except Exception as e:
        logger.error(f"   [WEB RESEARCH] Failed to write insights file: {e}")
        return None

def load_insights():
    """Load the latest web insights."""
    return load_json(WEB_INSIGHTS_FILE, None)
