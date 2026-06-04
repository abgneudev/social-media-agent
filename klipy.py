"""Klipy GIF resolver.

Resolves a short text query to a single GIF URL, ready to be downloaded
and uploaded as an inline image embed on Bluesky. Posture:

  - GIF attachment is always a bonus, never required. resolve() returns
    None on any failure (no key, network error, no matching GIF, malformed
    response). Callers degrade to text-only.
  - content_filter is forced to the STRICTEST available setting. The
    account is unattended and posts go out without human screening, so
    permissive filtering is not an option.
  - In-memory cache by query so the same generation cycle does not fetch
    the same query twice; cache is process-local and dies with the
    process, which is fine for a small unattended bot.

Notes on Bluesky embed behavior (4A finding):
  Bluesky DOES NOT inline-animate arbitrary external image URLs. Only
  specific GIF hosts (currently tenor / giphy) are special-cased by the
  official client and rendered as inline animations via the External
  embed type. A Klipy URL plugged into an External embed will render as
  a link CARD, not an animation. The variety we want comes from animated
  inline images, so the adapter must DOWNLOAD the GIF bytes and UPLOAD
  them to Bluesky as a regular image blob (app.bsky.embed.images). That
  path renders inline and animates reliably.

  This finding is documented here, in adapter.post_with_image, and in
  the engine call site. Without empirical confirmation in this
  environment (no Bluesky login credentials available), an operator
  should verify on first --live --once run that the GIF actually
  animates inline; if it shows as a static thumbnail, the cause is
  almost always Bluesky stripping animation from a downsampled image,
  and the fix is to prefer the sm.gif over sm.webp in the URL order.
"""
import os
import json
import urllib.parse
import urllib.request

from config import logger


KLIPY_API_BASE = "https://api.klipy.com/api/v1"
# Strictest available filter. The Klipy public API accepts "off", "low",
# "medium", "high"; "high" is the strictest. If Klipy renames or removes
# this enum, the call still works (filter applied server-side, default is
# typically permissive) but we want the request to fail loudly in that
# case, so we surface a warning rather than silently downgrading.
KLIPY_CONTENT_FILTER = "high"
KLIPY_TIMEOUT_SECS = 6
# Hard cap on downloaded bytes. A runaway GIF should not blow memory or
# slow the tick. Real animated GIFs from sm/xs Klipy URLs are typically
# well under this.
KLIPY_MAX_BYTES = 4 * 1024 * 1024


# Process-local cache. Maps normalized query -> resolved URL (or None
# when a prior resolve attempt confirmed no match). Negative caching is
# intentional: the same query in the same process should not hammer the
# API after a known miss.
_RESOLVE_CACHE = {}


def _key():
    return os.environ.get("KLIPY_APP_KEY", "").strip()


def _normalize(query):
    return " ".join((query or "").lower().split())


def _pick_url(item):
    """Pick the best inline-friendly URL from a Klipy result item.

    Preference order is sm.webp, sm.gif, xs.webp, xs.gif. Smaller variants
    are preferred because Bluesky may reject very large blobs. webp comes
    before gif at each size because webp tends to be smaller for the same
    fidelity; the adapter falls back to gif if Bluesky's animation pipeline
    rejects webp."""
    file_section = (item or {}).get("file") or {}
    for size in ("sm", "xs"):
        bucket = file_section.get(size) or {}
        for ext in ("webp", "gif"):
            url = bucket.get(ext)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
    return None


def resolve(query: str):
    app_key = os.getenv("KLIPY_APP_KEY")
    if not app_key:
        logger.critical("[KLIPY] KLIPY_APP_KEY is missing from environment!")
        return None

    # 1. Sanitize the LLM output (strip literal quotes and whitespace)
    clean_query = query.replace('"', '').replace("'", "").strip()
    
    # 2. Safely URL-encode the string
    encoded_query = urllib.parse.quote(clean_query)
    
    url = f"https://api.klipy.co/v1/gifs/search?q={encoded_query}"
    req = urllib.request.Request(
        url, 
        headers={
            "Authorization": f"Bearer {app_key}",
            "User-Agent": "kiloforge/1"
        }
    )
    
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("items", [])
            if not items:
                logger.warning(f"[KLIPY] API returned 200 but empty items for: '{clean_query}'. Raw data: {data}")
                return None
            return items[0].get("url")
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        logger.error(f"[KLIPY] HTTP {e.code} for query '{clean_query}': {body}")
        return None
    except Exception as e:
        logger.error(f"[KLIPY] Unexpected error: {e}")
        return None


def fetch_bytes(url):
    """Download GIF/webp bytes from a resolved URL. Returns (bytes, mime)
    or None on any failure. Capped at KLIPY_MAX_BYTES."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kiloforge/1"})
        with urllib.request.urlopen(req, timeout=KLIPY_TIMEOUT_SECS) as resp:
            data = resp.read(KLIPY_MAX_BYTES + 1)
            ctype = resp.headers.get("Content-Type", "")
        if len(data) > KLIPY_MAX_BYTES:
            logger.warning(f"   [KLIPY] fetch exceeded {KLIPY_MAX_BYTES} bytes; dropping.")
            return None
        # Map to a mime Bluesky accepts; default to image/gif if header
        # missing or unrecognized.
        ctype = ctype.lower()
        if "webp" in ctype:
            mime = "image/webp"
        elif "gif" in ctype:
            mime = "image/gif"
        elif "png" in ctype:
            mime = "image/png"
        elif "jpeg" in ctype or "jpg" in ctype:
            mime = "image/jpeg"
        else:
            mime = "image/gif"
        return data, mime
    except Exception as e:
        logger.warning(f"   [KLIPY] fetch failed: {e}")
        return None


def reset_cache():
    """Test helper: clear the process-local resolve cache."""
    _RESOLVE_CACHE.clear()
