import os
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional

from core.platform import Platform
from core.config import logger

class ThreadsPlatform(Platform):
    """Adapter for the official Threads Meta Graph API.
    
    Requires THREADS_USER_ID and THREADS_ACCESS_TOKEN in the environment.
    Uses the container -> publish two-step flow for posting.
    """
    
    def __init__(self):
        self.user_id = os.environ.get("THREADS_USER_ID")
        self.access_token = os.environ.get("THREADS_ACCESS_TOKEN")
        self.base_url = "https://graph.threads.net/v1.0"
        
        if not self.user_id or not self.access_token:
            logger.warning("[THREADS] Missing credentials! Posts will silently fail.")
            
        self.did = f"threads:{self.user_id}"
        self.handle = "threads_agent"
        
        if self.user_id:
            # Try to fetch username if possible (not strictly necessary for posting)
            try:
                data = self._get(f"/{self.user_id}?fields=username")
                if data and "username" in data:
                    self.handle = data["username"]
            except Exception as e:
                pass
            logger.info(f"      [NET] Threads authenticated as @{self.handle}")

    def _post_request(self, endpoint: str, data: dict) -> Optional[dict]:
        if not self.user_id or not self.access_token:
            return None
            
        data["access_token"] = self.access_token
        encoded_data = urllib.parse.urlencode(data).encode("utf-8")
        url = f"{self.base_url}{endpoint}"
        
        req = urllib.request.Request(url, data=encoded_data, method="POST")
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            logger.warning(f"      [FAULT] Threads API error: {err_msg}")
            return None
        except Exception as e:
            logger.warning(f"      [FAULT] Threads network error: {e}")
            return None

    def _get(self, endpoint: str) -> Optional[dict]:
        if not self.access_token:
            return None
            
        sep = "&" if "?" in endpoint else "?"
        url = f"{self.base_url}{endpoint}{sep}access_token={self.access_token}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"      [FAULT] Threads GET error: {e}")
            return None

    def _publish_container(self, text: str, reply_to_id: str = None) -> Optional[str]:
        """Step 1: Create container, Step 2: Publish it."""
        # 1. Create container
        payload = {
            "media_type": "TEXT",
            "text": text[:500]  # Hard limit 500
        }
        if reply_to_id:
            payload["reply_to_id"] = reply_to_id
            
        res = self._post_request(f"/{self.user_id}/threads", payload)
        if not res or "id" not in res:
            return None
            
        container_id = res["id"]
        
        # 2. Publish container
        pub_res = self._post_request(f"/{self.user_id}/threads_publish", {"creation_id": container_id})
        if not pub_res or "id" not in pub_res:
            return None
            
        published_id = pub_res["id"]
        logger.info(f"      [THREADS] Successfully published: {published_id}")
        return published_id

    # --- Core Write Methods ---
    def post(self, text: str) -> str:
        res = self._publish_container(text)
        return res if res else "threads-failed-post"

    def reply(self, target, text: str) -> str:
        # If target is a string (CID from omni platform)
        if isinstance(target, str):
            reply_id = target
        else:
            # Fallback if target is a Bluesky post object or dict
            reply_id = getattr(target, "cid", None)
            
        if not reply_id:
            # If we can't find a Threads ID to reply to, we just post standalone or fail gracefully
            return self.post(text)
            
        res = self._publish_container(text, reply_to_id=reply_id)
        return res if res else "threads-failed-reply"

    def post_with_image(self, text: str, image_bytes: bytes, alt_text: str = "") -> str:
        # The Threads API requires a public image_url for media.
        # Since we generate images in memory and don't have a public CDN handy,
        # we gracefully degrade to a text-only post for Threads.
        logger.info("      [THREADS] Skipping image payload (requires public URL), posting text only.")
        return self.post(text)

    def post_with_video(self, text: str, video_bytes: bytes, alt_text: str = "") -> str:
        return self.post(text)

    def post_in_thread(self, text: str, root_uri: str, root_cid: str, parent_uri: str, parent_cid: str) -> str:
        return self.reply(parent_cid, text)

    def quote_post(self, text: str, quote_uri: str, quote_cid: str) -> str:
        # Threads API doesn't support quote posts via API yet, degrade to reply
        return self.reply(quote_cid, text)

    # --- Methods that mutate graph but may not be supported by Threads API ---
    def like(self, uri: str, cid: str): pass
    def follow(self, did: str): pass
    def mute_actor(self, did: str): pass
    def repost(self, uri: str, cid: str): pass
    def set_profile(self, name: str, description: str): pass
    def pin_post(self, uri: str, cid: str): pass
    def create_list(self, name: str, description: str = "") -> str | None: return None
    def add_to_list(self, list_uri: str, target_did: str) -> bool: return False
    def send_interaction(self, item_uri: str, interaction_type: str, feed_uri: str = None): pass

    # --- Core Read Methods (mostly stubbed out if not natively needed for Omni sync) ---
    def follower_count(self) -> int: return 0
    def search_posts(self, keyword: str, limit: int = 15) -> list: return []
    def fetch_timeline(self, limit: int = 30) -> list: return []
    def get_profile(self, actor: str): return None
    def post_engagement(self, uri: str) -> int: return 0
    def followed_back_by(self, actor: str) -> bool: return False
    def recent_followers(self, limit: int = 25) -> list: return []
    def get_all_follows(self) -> set: return set()
    def get_post_image_b64(self, post) -> str | None: return None
    def get_post_cid(self, uri: str) -> str | None: return None
    def find_post(self, target_hash: str, limit: int = 50) -> tuple[str, str] | None: return None
    def get_likers(self, uri: str, limit: int = 50) -> list: return []
    def get_reposters(self, uri: str, limit: int = 50) -> list: return []
