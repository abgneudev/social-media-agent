"""Serper.dev integration for autonomous agentic tool use.

Provides Web Search, News Search, and Image Search capabilities to the LLM.
Uses Serper.dev (2,500 free queries) instead of SerpAPI (250/mo limit).
"""
import os
import json
import urllib.request
import urllib.error

from config import logger, STATE_DIR
from store import load_json, atomic_write_json

SERPER_TIMEOUT_SECS = 8
CACHE_FILE = STATE_DIR / "serper_cache.json"
DAILY_BUDGET = 50

# Share the same image download logic as before
SERPER_MAX_BYTES = 1000 * 1024

def _load_cache():
    return load_json(CACHE_FILE, {"queries": {}, "daily_usage": {}})

def _save_cache(cache):
    atomic_write_json(CACHE_FILE, cache)

def _key():
    return os.environ.get("SERPER_API_KEY", "").strip()

def _check_budget(clean_query):
    api_key = _key()
    if not api_key:
        logger.critical("[SERPER] SERPER_API_KEY is missing from environment!")
        return None, None

    if not clean_query:
        return None, None
        
    cache = _load_cache()
    
    import datetime
    today = datetime.date.today().isoformat()
    usage_today = cache["daily_usage"].get(today, 0)
    
    if usage_today >= DAILY_BUDGET:
        logger.warning(f"[SERPER] Daily budget ({DAILY_BUDGET}) reached. Skipping query '{clean_query}'.")
        return None, None
        
    return api_key, cache

def _record_usage(cache):
    import datetime
    today = datetime.date.today().isoformat()
    usage_today = cache["daily_usage"].get(today, 0)
    cache["daily_usage"][today] = usage_today + 1
    _save_cache(cache)

def _post_request(url, api_key, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=SERPER_TIMEOUT_SECS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"[SERPER] Request failed for {url}: {e}")
        return None

def search_images(query: str):
    """Finds a high-quality Google Image URL for the given query."""
    clean_query = query.replace('"', '').replace("'", "").strip()
    cache_key = f"image:{clean_query}"
    
    api_key, cache = _check_budget(clean_query)
    if not api_key: return None
    
    if cache_key in cache["queries"]:
        return cache["queries"][cache_key]
        
    data = _post_request("https://google.serper.dev/images", api_key, {"q": clean_query})
    if not data: return None
    
    _record_usage(cache)
    
    images = data.get("images", [])
    if not images:
        logger.warning(f"[SERPER] No image results found for: '{clean_query}'")
        cache["queries"][cache_key] = None
        _save_cache(cache)
        return None
        
    valid_urls = []
    for img in images:
        url = img.get("imageUrl")
        if url and not url.lower().endswith(".svg"):
            valid_urls.append(url)
            
    if valid_urls:
        cache["queries"][cache_key] = valid_urls
        _save_cache(cache)
        return valid_urls
        
    cache["queries"][cache_key] = None
    _save_cache(cache)
    return None

def search_news(query: str):
    """Fetches the top 3 latest news snippets for a topic."""
    clean_query = query.replace('"', '').replace("'", "").strip()
    
    api_key, cache = _check_budget(clean_query)
    if not api_key: return None
    
    # We don't cache news as aggressively as images because news changes daily,
    # but caching within the same tick/run is fine.
    # To keep it simple, we won't cache news to ensure the LLM gets real-time data.
    
    data = _post_request("https://google.serper.dev/news", api_key, {"q": clean_query})
    if not data: return None
    
    _record_usage(cache)
    
    news_items = data.get("news", [])[:3]
    if not news_items:
        return "No recent news found for this topic."
        
    results = []
    for item in news_items:
        results.append(f"Title: {item.get('title')}\nSource: {item.get('source')} ({item.get('date')})\nSnippet: {item.get('snippet')}\n")
        
    return "\n".join(results)

def fetch_image_bytes(url):
    """Download bytes from a resolved URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kiloforge/1"})
        with urllib.request.urlopen(req, timeout=SERPER_TIMEOUT_SECS) as resp:
            data = resp.read(SERPER_MAX_BYTES + 1)
            ctype = resp.headers.get("Content-Type", "").lower()
            
        if len(data) > SERPER_MAX_BYTES:
            logger.warning(f"   [SERPER] Image exceeded {SERPER_MAX_BYTES} bytes; dropping.")
            return None
            
        if "webp" in ctype: mime = "image/webp"
        elif "png" in ctype: mime = "image/png"
        elif "jpeg" in ctype or "jpg" in ctype: mime = "image/jpeg"
        elif "gif" in ctype: mime = "image/gif"
        else: mime = "image/jpeg"
            
        return data, mime
    except Exception as e:
        logger.warning(f"   [SERPER] Image fetch failed from {url[:50]}... : {e}")
        return None

def search_web_organic(query: str, num_results=3):
    """Fetches top organic web results for a given query."""
    clean_query = query.replace('"', '').replace("'", "").strip()
    
    api_key, cache = _check_budget(clean_query)
    if not api_key: return []
    
    data = _post_request("https://google.serper.dev/search", api_key, {"q": clean_query, "num": num_results})
    if not data: return []
    
    _record_usage(cache)
    
    organic = data.get("organic", [])
    results = []
    for item in organic:
        results.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", "")
        })
    return results

def fetch_web_markdown(url: str):
    """Fetches the content of a URL and converts it to markdown using r.jina.ai"""
    jina_url = f"https://r.jina.ai/{url}"
    try:
        req = urllib.request.Request(jina_url, headers={"User-Agent": "kiloforge/1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
            # Truncate if it's absurdly long to save LLM context
            return text[:15000]
    except Exception as e:
        logger.warning(f"   [SERPER] Fetch web markdown failed for {url[:50]}... : {e}")
        return None
