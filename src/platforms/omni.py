from core.platform import Platform
from core.config import logger

class OmniPlatform(Platform):
    """Broadcasting wrapper that routes commands to multiple platforms.
    
    For READ methods (e.g., getting followers, timeline), it currently defaults
    to the FIRST platform in the list (typically Bluesky) as the source of truth
    for the Strategist engine.
    
    For WRITE methods (e.g., post, reply, like), it attempts to broadcast the
    action across all platforms in the list.
    """
    
    def __init__(self, platforms: list[Platform]):
        if not platforms:
            raise ValueError("OmniPlatform requires at least one child platform.")
            
        self.platforms = platforms
        
        # We inherit the primary identity from the first platform
        self.primary = platforms[0]
        self.did = self.primary.did
        self.handle = self.primary.handle

    # --- Broadcaster Helper ---
    def _broadcast(self, method_name: str, *args, **kwargs):
        """Broadcasts a method call to all platforms and returns a dict of results."""
        results = {}
        for p in self.platforms:
            try:
                method = getattr(p, method_name)
                # Call the method
                res = method(*args, **kwargs)
                results[p.__class__.__name__] = res
            except Exception as e:
                logger.warning(f"[OMNI] Platform {p.__class__.__name__} failed on {method_name}: {e}")
                results[p.__class__.__name__] = None
        return results

    # --- WRITE Methods (Broadcast to all) ---
    def post(self, text: str) -> str:
        results = self._broadcast("post", text)
        return results.get(self.primary.__class__.__name__) or "omni-failed"

    def reply(self, target, text: str) -> str:
        results = self._broadcast("reply", target, text)
        return results.get(self.primary.__class__.__name__) or "omni-failed"

    def post_with_image(self, text: str, image_bytes: bytes, alt_text: str = "") -> str:
        results = self._broadcast("post_with_image", text, image_bytes, alt_text)
        return results.get(self.primary.__class__.__name__) or "omni-failed"

    def post_with_video(self, text: str, video_bytes: bytes, alt_text: str = "") -> str:
        results = self._broadcast("post_with_video", text, video_bytes, alt_text)
        return results.get(self.primary.__class__.__name__) or "omni-failed"

    def post_in_thread(self, text: str, root_uri: str, root_cid: str, parent_uri: str, parent_cid: str) -> str:
        results = self._broadcast("post_in_thread", text, root_uri, root_cid, parent_uri, parent_cid)
        return results.get(self.primary.__class__.__name__) or "omni-failed"

    def quote_post(self, text: str, quote_uri: str, quote_cid: str) -> str:
        results = self._broadcast("quote_post", text, quote_uri, quote_cid)
        return results.get(self.primary.__class__.__name__) or "omni-failed"

    def like(self, uri: str, cid: str):
        self._broadcast("like", uri, cid)

    def follow(self, did: str):
        self._broadcast("follow", did)

    def mute_actor(self, did: str):
        self._broadcast("mute_actor", did)

    def repost(self, uri: str, cid: str):
        self._broadcast("repost", uri, cid)

    def set_profile(self, name: str, description: str):
        self._broadcast("set_profile", name, description)

    def pin_post(self, uri: str, cid: str):
        self._broadcast("pin_post", uri, cid)

    def create_list(self, name: str, description: str = "") -> str | None:
        results = self._broadcast("create_list", name, description)
        return results.get(self.primary.__class__.__name__)

    def add_to_list(self, list_uri: str, target_did: str) -> bool:
        results = self._broadcast("add_to_list", list_uri, target_did)
        return results.get(self.primary.__class__.__name__) or False

    def send_interaction(self, item_uri: str, interaction_type: str, feed_uri: str = None):
        self._broadcast("send_interaction", item_uri, interaction_type, feed_uri)


    # --- READ Methods (Defer to Primary Platform) ---
    def follower_count(self) -> int:
        return self.primary.follower_count()

    def search_posts(self, keyword: str, limit: int = 15) -> list:
        return self.primary.search_posts(keyword, limit)

    def fetch_timeline(self, limit: int = 30) -> list:
        return self.primary.fetch_timeline(limit)

    def get_profile(self, actor: str):
        return self.primary.get_profile(actor)

    def post_engagement(self, uri: str) -> int:
        return self.primary.post_engagement(uri)

    def followed_back_by(self, actor: str) -> bool:
        return self.primary.followed_back_by(actor)

    def recent_followers(self, limit: int = 25) -> list:
        return self.primary.recent_followers(limit)

    def get_all_follows(self) -> set:
        return self.primary.get_all_follows()

    def get_post_image_b64(self, post) -> str | None:
        return self.primary.get_post_image_b64(post)

    def get_post_cid(self, uri: str) -> str | None:
        return self.primary.get_post_cid(uri)

    def find_post(self, target_hash: str, limit: int = 50) -> tuple[str, str] | None:
        return self.primary.find_post(target_hash, limit)

    def get_likers(self, uri: str, limit: int = 50) -> list:
        return self.primary.get_likers(uri, limit)

    def get_reposters(self, uri: str, limit: int = 50) -> list:
        return self.primary.get_reposters(uri, limit)
