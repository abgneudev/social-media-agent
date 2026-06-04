"""SerpAPI Google Images resolver.

Resolves a descriptive query into a high-quality Google Image URL.
Intended for concrete, technical diagrams (e.g. UX flows, UI components)
rather than animated reaction GIFs.
"""
import os
import json
import urllib.parse
import urllib.request

from config import logger, STATE_DIR
from store import load_json, atomic_write_json

# Hard cap on downloaded bytes to prevent OOM/Bluesky blob rejection.
SERPAPI_MAX_BYTES = 1000 * 1024  # ~1MB (Bluesky limit)
SERPAPI_TIMEOUT_SECS = 8

# Persistent cache to avoid burning 250/mo credits
CACHE_FILE = STATE_DIR / "serpapi_cache.json"
# Daily budget to stretch 250 searches across 30 days (~8 per day)
DAILY_BUDGET = 8

def _load_cache():
    return load_json(CACHE_FILE, {"queries": {}, "daily_usage": {}})

def _save_cache(cache):
    atomic_write_json(CACHE_FILE, cache)

def _key():
    return os.environ.get("SERPAPI_KEY", "").strip()

def search_image(query: str):
    api_key = _key()
    if not api_key:
        logger.critical("[SERPAPI] SERPAPI_KEY is missing from environment!")
        return None

    clean_query = query.replace('"', '').replace("'", "").strip()
    if not clean_query:
        return None
        
    cache = _load_cache()
    if clean_query in cache["queries"]:
        return cache["queries"][clean_query]

    import datetime
    today = datetime.date.today().isoformat()
    usage_today = cache["daily_usage"].get(today, 0)
    
    if usage_today >= DAILY_BUDGET:
        logger.warning(f"[SERPAPI] Daily budget ({DAILY_BUDGET}) reached. Skipping query '{clean_query}'.")
        return None

    encoded_query = urllib.parse.quote(clean_query)
    url = f"https://serpapi.com/search.json?engine=google&tbm=isch&q={encoded_query}&api_key={api_key}"
    req = urllib.request.Request(url, headers={"User-Agent": "kiloforge/1"})
    
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            
            # Count the API call against the budget
            cache["daily_usage"][today] = usage_today + 1
            
            if "error" in data:
                logger.error(f"[SERPAPI] API Error: {data['error']}")
                _save_cache(cache)
                return None
                
            images = data.get("images_results", [])
            if not images:
                logger.warning(f"[SERPAPI] No image results found for: '{clean_query}'")
                cache["queries"][clean_query] = None
                _save_cache(cache)
                return None
                
            for img in images:
                original_url = img.get("original")
                if original_url and not original_url.lower().endswith(".svg"):
                    cache["queries"][clean_query] = original_url
                    _save_cache(cache)
                    return original_url
                    
            return None
    except Exception as e:
        logger.error(f"[SERPAPI] Request failed for query '{clean_query}': {e}")
        return None


def fetch_image_bytes(url):
    """Download bytes from a resolved URL. Returns (bytes, mime).
    Gracefully handles sizes by rejecting massive files."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kiloforge/1"})
        with urllib.request.urlopen(req, timeout=SERPAPI_TIMEOUT_SECS) as resp:
            # We read exactly MAX_BYTES + 1. If it's larger, we drop it.
            data = resp.read(SERPAPI_MAX_BYTES + 1)
            ctype = resp.headers.get("Content-Type", "").lower()
            
        if len(data) > SERPAPI_MAX_BYTES:
            logger.warning(f"   [SERPAPI] Image exceeded {SERPAPI_MAX_BYTES} bytes; dropping.")
            return None
            
        if "webp" in ctype:
            mime = "image/webp"
        elif "png" in ctype:
            mime = "image/png"
        elif "jpeg" in ctype or "jpg" in ctype:
            mime = "image/jpeg"
        elif "gif" in ctype:
            mime = "image/gif"
        else:
            # Fallback for generic octet-streams that are often jpegs
            mime = "image/jpeg"
            
        return data, mime
    except Exception as e:
        logger.warning(f"   [SERPAPI] Image fetch failed from {url[:50]}... : {e}")
        return None
