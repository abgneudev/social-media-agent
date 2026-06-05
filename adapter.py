"""Adapter: thin wrapper around the atproto client.

One concrete adapter, by design. The agent talks to Bluesky and only
Bluesky; a protocol-class layer here would be future-proofing for a domain
we have not validated. If a second platform ever lands, add a second
adapter alongside this one and let the engine pick.
"""
import base64
import urllib.request
import urllib.error

from atproto import Client, exceptions, models

import config
from config import content_hash, logger, NAME_TEXT, BIO_TEXT


class BlueskyAdapter:
    def __init__(self, handle, app_password):
        self.client = Client()
        profile = self.client.login(handle, app_password)
        self.did = profile.did
        self.handle = handle
        logger.info(f"      [NET] authenticated as @{handle} ({self.did})")

    # reads
    def follower_count(self) -> int:
        prof = self.client.get_profile(actor=self.did)
        return int(getattr(prof, "followers_count", 0) or 0)

    def search_posts(self, keyword, limit=15) -> list:
        resp = self.client.app.bsky.feed.search_posts({"q": keyword, "limit": limit})
        return list(resp.posts) if getattr(resp, "posts", None) else []

    def fetch_timeline(self, limit=30) -> list:
        try:
            resp = self.client.app.bsky.feed.get_timeline({"limit": limit})
            return [item.post for item in getattr(resp, "feed", []) if hasattr(item, "post")]
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [FAULT] fetch_timeline failed: {e}")
            return []

    def get_profile(self, actor):
        try:
            return self.client.get_profile(actor=actor)
        except exceptions.AtProtocolError:
            return None

    def post_engagement(self, uri) -> int:
        try:
            resp = self.client.app.bsky.feed.get_post_thread({"uri": uri, "depth": 0})
            post = getattr(resp.thread, "post", None)
            if post is None:
                return 0
            return ((getattr(post, "like_count", 0) or 0)
                    + (getattr(post, "repost_count", 0) or 0)
                    + (getattr(post, "reply_count", 0) or 0))
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [TELEMETRY] cannot inspect {uri[:40]}: {e}")
            return 0

    def followed_back_by(self, actor) -> bool:
        prof = self.client.get_profile(actor=actor)
        viewer = getattr(prof, "viewer", None)
        return bool(viewer and getattr(viewer, "followed_by", None))

    def recent_followers(self, limit=25) -> list:
        try:
            resp = self.client.get_followers(actor=self.did, limit=limit)
            return list(getattr(resp, "followers", []) or [])
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [FAULT] recent_followers: {e}")
            return []

    def get_all_follows(self) -> set:
        """Fetch all DIDs the agent is currently following."""
        follows = set()
        cursor = None
        try:
            while True:
                resp = self.client.get_follows(actor=self.did, limit=100, cursor=cursor)
                for f in getattr(resp, "follows", []) or []:
                    if hasattr(f, "did"):
                        follows.add(f.did)
                cursor = getattr(resp, "cursor", None)
                if not cursor:
                    break
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [FAULT] get_all_follows failed: {e}")
        return follows

    def get_post_image_b64(self, post) -> str | None:
        """Extract the first image from a post's embed and return as base64.

        Walks embed.images (app.bsky.embed.images) and downloads the
        fullsize URL. Returns None on any failure so callers degrade to
        text-only generation."""
        try:
            embed = getattr(post, "embed", None)
            if embed is None:
                return None
            images = getattr(embed, "images", None)
            if not images:
                return None
            first = images[0]
            img_url = getattr(first, "fullsize", None) or getattr(first, "thumb", None)
            if not img_url:
                return None
            req = urllib.request.Request(img_url, headers={"User-Agent": "kiloforge/1"})
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = resp.read(4 * 1024 * 1024)
            return base64.b64encode(data).decode("utf-8")
        except urllib.error.URLError as e:
            logger.warning(f"      [VISION] image extraction network timeout/error: {e}")
            return None

    # writes
    # Idempotency notes:
    # - like() and follow() are naturally idempotent on Bluesky. Re-liking a
    #   record or re-following an actor is a server-side no-op, so a retry
    #   after a partial failure cannot create a duplicate.
    # - post(), reply(), and quote_post() each create a fresh record on every
    #   call. They are NOT idempotent. Callers must wrap them in
    #   FollowerEngine._publish_with_reconcile so that a write whose response
    #   was lost can be recovered via find_post() instead of being retried.
    def like(self, uri, cid):
        self.client.like(uri, cid)

    def follow(self, did):
        self.client.follow(did)

    def mute_actor(self, did):
        try:
            self.client.mute(did)
            logger.info(f"   [MUTE] Muted actor: {did}")
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [MUTE] Failed to mute {did}: {e}")

    def send_interaction(self, item_uri, interaction_type, feed_uri=None):
        try:
            feed = feed_uri or config.FEED_URI_FALLBACK
            # self.client.app.bsky.feed.send_interactions(
            #     data={
            #         "feed": feed,
            #         "interactions": [{"item": item_uri, "event": interaction_type}]
            #     }
            # )
            logger.info(f"   [FEEDBACK] Sent {interaction_type.split('#')[-1]} for {item_uri}")
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FEEDBACK] Failed to send interaction: {e}")

    def reply(self, target, text) -> str:
        root = (target.record.reply.root
                if getattr(target.record, "reply", None)
                else {"uri": target.uri, "cid": target.cid})
        parent = {"uri": target.uri, "cid": target.cid}
        ref = self.client.send_post(text=text, reply_to={"root": root, "parent": parent})
        return ref.uri if hasattr(ref, "uri") else str(ref)

    def post(self, text) -> str:
        ref = self.client.send_post(text=text)
        return ref.uri if hasattr(ref, "uri") else str(ref)

    def post_with_image(self, text, image_bytes, alt_text="") -> str:
        """Post text plus a single image embed. Used for GIF attachment.

        Why we upload the bytes rather than External-embed the Klipy URL:
        Bluesky's official client only inline-animates External embeds
        from a small whitelist of GIF hosts (tenor / giphy). A Klipy URL
        in an External embed renders as a link card, which adds no
        format variety. Uploading the bytes as an image blob renders the
        GIF inline and animated regardless of the source host.

        Failures here MUST bubble up: the caller catches and degrades to
        text-only. Wrapping the failure here would hide it from the
        engine's breaker accounting."""
        blob_resp = self.client.upload_blob(image_bytes)
        blob = getattr(blob_resp, "blob", None) or blob_resp
        image = models.AppBskyEmbedImages.Image(
            image=blob, alt=alt_text or "",
        )
        embed = models.AppBskyEmbedImages.Main(images=[image])
        ref = self.client.send_post(text=text, embed=embed)
        return ref.uri if hasattr(ref, "uri") else str(ref)

    def post_with_video(self, text, video_bytes, alt_text="") -> str:
        """Post text plus a single native video embed. Used for animated GIFs (which must be uploaded as MP4s to animate on Bluesky)."""
        ref = self.client.send_video(text=text, video=video_bytes, video_alt=alt_text or "")
        return ref.uri if hasattr(ref, "uri") else str(ref)

    def post_in_thread(self, text, root_uri, root_cid, parent_uri, parent_cid) -> str:
        """Post a continuation in a thread we already started. Used by
        mini_thread to chain 2 to 3 short posts. root is always the first
        part of the thread, parent is the immediately preceding part."""
        root = {"uri": root_uri, "cid": root_cid}
        parent = {"uri": parent_uri, "cid": parent_cid}
        ref = self.client.send_post(text=text, reply_to={"root": root, "parent": parent})
        return ref.uri if hasattr(ref, "uri") else str(ref)

    def get_post_cid(self, uri):
        """Resolve the cid for one of our own posts via a thread fetch.
        Used after posting to grab the cid needed to pin or to thread off
        of. Returns None on any failure (callers degrade gracefully)."""
        try:
            resp = self.client.app.bsky.feed.get_post_thread({"uri": uri, "depth": 0})
            return getattr(getattr(resp.thread, "post", None), "cid", None)
        except exceptions.AtProtocolError:
            return None

    def repost(self, uri, cid):
        self.client.repost(uri, cid)

    def quote_post(self, text, quote_uri, quote_cid) -> str:
        embed = models.AppBskyEmbedRecord.Main(
            record=models.ComAtprotoRepoStrongRef.Main(uri=quote_uri, cid=quote_cid)
        )
        ref = self.client.send_post(text=text, embed=embed)
        return ref.uri if hasattr(ref, "uri") else str(ref)

    def find_post(self, target_hash, limit=50):
        """Search our own recent author feed for a post whose text hashes to
        target_hash. Returns (uri, cid) or None. Used to reconcile a write
        whose response was lost on the network: if the post actually landed,
        we recover its URI here instead of retrying and double-posting.
        Posts contain no embedded marker, so matching is by content_hash of
        the exact text we generated."""
        try:
            resp = self.client.get_author_feed(actor=self.did, limit=limit)
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [RECONCILE] find_post fetch failed: {e}")
            return None
        for item in getattr(resp, "feed", None) or []:
            post = getattr(item, "post", None)
            if post is None:
                continue
            record = getattr(post, "record", None)
            text = getattr(record, "text", None) if record else None
            if text and content_hash(text) == target_hash:
                return (getattr(post, "uri", None), getattr(post, "cid", None))
        return None

    def _get_profile_record(self):
        try:
            return self.client.com.atproto.repo.get_record({
                "repo": self.did, "collection": "app.bsky.actor.profile", "rkey": "self",
            })
        except exceptions.AtProtocolError:
            return None

    def set_profile(self, name, description):
        try:
            existing = self._get_profile_record()
            avatar = banner = pinned = None
            if existing and getattr(existing, "value", None):
                avatar = getattr(existing.value, "avatar", None)
                banner = getattr(existing.value, "banner", None)
                pinned = getattr(existing.value, "pinned_post", None)
            record = models.AppBskyActorProfile.Record(
                display_name=name, description=description,
                avatar=avatar, banner=banner, pinned_post=pinned,
            )
            self.client.com.atproto.repo.put_record({
                "repo": self.did, "collection": "app.bsky.actor.profile",
                "rkey": "self", "record": record,
                "swap_record": getattr(existing, "cid", None) if existing else None,
            })
            logger.info(f"      [PROFILE] set name='{name}' and bio.")
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [PROFILE] update skipped (version/permission): {e}")

    def pin_post(self, uri, cid):
        """Best-effort pin of our anchor post. Version-sensitive, never fatal."""
        try:
            existing = self._get_profile_record()
            val = getattr(existing, "value", None) if existing else None
            record = models.AppBskyActorProfile.Record(
                display_name=getattr(val, "display_name", NAME_TEXT) if val else NAME_TEXT,
                description=getattr(val, "description", BIO_TEXT) if val else BIO_TEXT,
                avatar=getattr(val, "avatar", None) if val else None,
                banner=getattr(val, "banner", None) if val else None,
                pinned_post=models.ComAtprotoRepoStrongRef.Main(uri=uri, cid=cid),
            )
            self.client.com.atproto.repo.put_record({
                "repo": self.did, "collection": "app.bsky.actor.profile",
                "rkey": "self", "record": record,
                "swap_record": getattr(existing, "cid", None) if existing else None,
            })
            logger.info("      [PROFILE] pinned anchor post.")
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [PROFILE] pin skipped (version/permission): {e}")

    # ---- list management ----
    def create_list(self, name, description="") -> str | None:
        """Create a curated list (app.bsky.graph.list). Returns the at:// URI
        of the new list, or None on failure. Lists are public and notify users
        when they are added, making them a powerful inbound-traffic tool."""
        try:
            from datetime import datetime, timezone
            record = {
                "name": name,
                "purpose": "app.bsky.graph.defs#curatelist",
                "description": description,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            resp = self.client.com.atproto.repo.create_record({
                "repo": self.did,
                "collection": "app.bsky.graph.list",
                "record": record,
            })
            uri = getattr(resp, "uri", None)
            logger.info(f"      [LIST] created list '{name}': {uri}")
            return uri
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [LIST] create_list failed: {e}")
            return None

    def add_to_list(self, list_uri, target_did) -> bool:
        """Add a user to a curated list (app.bsky.graph.listitem).
        Returns True on success, False on failure. Idempotent: Bluesky
        silently ignores duplicates."""
        try:
            from datetime import datetime, timezone
            record = {
                "subject": target_did,
                "list": list_uri,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            self.client.com.atproto.repo.create_record({
                "repo": self.did,
                "collection": "app.bsky.graph.listitem",
                "record": record,
            })
            logger.info(f"      [LIST] added {target_did[:30]} to list")
            return True
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [LIST] add_to_list failed: {e}")
            return False

    # ---- engagement lists ----
    def get_likers(self, uri, limit=50) -> list:
        """Fetch the list of users who liked a post. Returns a list of
        profile-like objects with .did, .handle, .followers_count, etc."""
        try:
            resp = self.client.app.bsky.feed.get_likes({"uri": uri, "limit": limit})
            return [item.actor for item in getattr(resp, "likes", []) if hasattr(item, "actor")]
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [TELEMETRY] get_likers failed: {e}")
            return []

    def get_reposters(self, uri, limit=50) -> list:
        """Fetch the list of users who reposted a post. Returns a list of
        profile-like objects."""
        try:
            resp = self.client.app.bsky.feed.get_reposted_by({"uri": uri, "limit": limit})
            return list(getattr(resp, "reposted_by", []) or [])
        except exceptions.AtProtocolError as e:
            logger.warning(f"      [TELEMETRY] get_reposters failed: {e}")
            return []

