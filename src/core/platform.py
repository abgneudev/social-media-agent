from abc import ABC, abstractmethod

class Platform(ABC):
    """Abstract base class for social media platforms.
    Defines the contract the Engine expects from any platform adapter."""
    
    did: str
    handle: str

    @abstractmethod
    def follower_count(self) -> int: pass

    @abstractmethod
    def search_posts(self, keyword: str, limit: int = 15) -> list: pass
    def get_author_feed(self, actor: str, limit: int = 15) -> list: pass
    def search_actors(self, keyword: str, limit: int = 15) -> list: pass

    @abstractmethod
    def fetch_timeline(self, limit: int = 30) -> list: pass

    @abstractmethod
    def get_profile(self, actor: str): pass

    @abstractmethod
    def post_engagement(self, uri: str) -> int: pass

    @abstractmethod
    def followed_back_by(self, actor: str) -> bool: pass

    @abstractmethod
    def recent_followers(self, limit: int = 25) -> list: pass

    @abstractmethod
    def get_all_follows(self) -> set: pass

    @abstractmethod
    def get_post_image_b64(self, post) -> str | None: pass

    @abstractmethod
    def like(self, uri: str, cid: str): pass

    @abstractmethod
    def follow(self, did: str): pass

    @abstractmethod
    def mute_actor(self, did: str): pass

    @abstractmethod
    def send_interaction(self, item_uri: str, interaction_type: str, feed_uri: str = None): pass

    @abstractmethod
    def reply(self, target, text: str) -> str: pass

    @abstractmethod
    def post(self, text: str) -> str: pass

    @abstractmethod
    def post_with_image(self, text: str, image_bytes: bytes, alt_text: str = "") -> str: pass

    @abstractmethod
    def post_with_video(self, text: str, video_bytes: bytes, alt_text: str = "") -> str: pass

    @abstractmethod
    def post_in_thread(self, text: str, root_uri: str, root_cid: str, parent_uri: str, parent_cid: str) -> str: pass

    @abstractmethod
    def get_post_cid(self, uri: str) -> str | None: pass

    @abstractmethod
    def repost(self, uri: str, cid: str): pass

    @abstractmethod
    def quote_post(self, text: str, quote_uri: str, quote_cid: str) -> str: pass

    @abstractmethod
    def find_post(self, target_hash: str, limit: int = 50) -> tuple[str, str] | None: pass

    @abstractmethod
    def set_profile(self, name: str, description: str): pass

    @abstractmethod
    def pin_post(self, uri: str, cid: str): pass

    @abstractmethod
    def create_list(self, name: str, description: str = "") -> str | None: pass

    @abstractmethod
    def add_to_list(self, list_uri: str, target_did: str) -> bool: pass

    @abstractmethod
    def get_likers(self, uri: str, limit: int = 50) -> list: pass

    @abstractmethod
    def get_reposters(self, uri: str, limit: int = 50) -> list: pass
