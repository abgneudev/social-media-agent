"""Engine: FollowerEngine plus the heartbeat/status writer and small
ranking helpers (wilson_lower_bound, hook_strength).

This module is the orchestrator. It composes Store, BlueskyAdapter,
CircuitBreaker, and RateBudget; runs the sense -> learn -> decide -> act
loop; and exposes update_stall_counter for the runtime to call after every
tick (including ticks that raised).
"""
import os
import re
import json
import math
import time
import uuid
import random

from atproto import exceptions
from groq import Groq

import config
from config import (
    logger, content_hash, is_relevant_text,
    NAME_TEXT, BIO_TEXT, PERSONA,
    SECTORS, POST_HOOKS, REPLY_HOOKS,
    POST_HOOK_GUIDANCE, REPLY_HOOK_GUIDANCE,
    KEYWORD_MAP, RELEVANCE_RE,
    SENSITIVE_PHRASES, SENSITIVE_WORDS, SPAM_PHRASES,
    RATE_BUDGETS, GROWTH_PHASES,
    FOLLOWER_TARGET, MAX_LIKES_PER_TICK,
    ANCHOR_POST_TARGET, PROFILE_OPT_MIN_TRIALS, PROFILE_OPT_COOLDOWN_TICKS,
    PENDING_GRACE_SECONDS, STALL_THRESHOLD,
)
from store import Store, atomic_write_json
from governance import RateBudget, CircuitBreaker
from adapter import BlueskyAdapter


# ==========================================
# HONEST RANKING
# ==========================================
def wilson_lower_bound(successes, trials, z=1.96):
    if trials == 0:
        return 0.0
    p = successes / trials
    denom = 1 + z * z / trials
    center = p + z * z / (2 * trials)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials)
    return (center - margin) / denom


def hook_strength(text: str) -> float:
    """Cheap proxy for hook quality so we can pick among generated variants
    without an extra API call. Rewards a short, curious, concrete first line."""
    if not text:
        return -1.0
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0]
    score = 0.0
    fl = len(first)
    if fl <= 90:
        score += 2.0
    elif fl > 140:
        score -= 1.5
    low = first.lower()
    if "?" in first:
        score += 1.0
    if re.match(r"^\s*\d", first) or low.startswith(("most ", "the ", "why ", "here's", "everyone ")):
        score += 1.0
    if any(low.startswith(g) for g in ("in this", "today i", "let's talk", "i want to", "so i")):
        score -= 1.5
    total = len(text)
    if 120 <= total <= 280:
        score += 1.0
    return score


# ==========================================
# HEARTBEAT
# ==========================================
def write_status(engine):
    """Heartbeat written at the end of every loop iteration. Atomic via the
    existing helper, so an operator running `cat status.json` or a monitoring
    scrape never observes a partial write. Captures just the fields you'd
    need at 3am to decide whether the daemon is healthy: when it last
    ticked, who it thinks it is, what it just did, and whether the breaker
    or stall counter are in trouble."""
    snap = engine.store.snapshots[-1] if engine.store.snapshots else None
    atomic_write_json(config.STATUS_FILE, {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tick": engine.store.tick,
        "phase": engine.store.phase,
        "followers": snap.get("followers") if snap else None,
        "last_action": getattr(engine, "_last_action", None),
        "breaker_state": engine.breaker.state,
        "consecutive_empty_ticks": engine.store.consecutive_empty_ticks,
        "pending_writes": len(engine.store.pending),
        "anchor_posts": engine.store.anchor_posts,
    })


# ==========================================
# THE ENGINE
# ==========================================
class FollowerEngine:
    def __init__(self, handle, password):
        self.store = Store()
        self.net = BlueskyAdapter(handle, password)
        self.ai = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        self.breaker = CircuitBreaker()
        self.rate = {k: RateBudget(v["capacity"], v["refill_per_sec"])
                     for k, v in RATE_BUDGETS.items()}
        self.sector_activity = {}
        self.sector_posts = {}      # cache of fetched posts per sector, reused in _act
        self.persona = PERSONA

    # ---- kill switch ----
    def _halted(self) -> bool:
        try:
            return config.KILL_SWITCH_FILE.read_text(encoding="utf-8").strip().upper() == "HALTED"
        except FileNotFoundError:
            return False

    # ---- bootstrap ----
    def bootstrap(self):
        if self.store.phase != "bootstrap":
            return
        logger.info("[BOOTSTRAP] setting profile conversion surface")
        self.net.set_profile(NAME_TEXT, BIO_TEXT)
        self.store.phase = "explore"
        self.store.save_engine()

    # ---- crash-safe publishing ----
    def _publish_with_reconcile(self, kind, text, sector, hook, write_fn,
                                 learnable=True, target_did=None, target_handle=None):
        """Crash-safe wrapper around a publishing write (post/reply/quote).

        Persists the intent BEFORE the network write. If the write raises but
        the post actually landed (response lost mid-flight, socket reset after
        the server committed, etc.), find_post recovers the URI from our
        author feed and we treat it as success. If the write raises and
        nothing landed, we leave the intent in place and re-raise so the
        caller counts a real failure; the next tick's _reconcile_pending will
        pick up any intent that turns out to have landed after all.

        Returns (intent_id, uri). The caller is responsible for the rest of
        the bookkeeping (log_action, mark_seen, save_engine) and for calling
        store.remove_pending(intent_id) once that bookkeeping has succeeded.
        """
        ch = content_hash(text)
        intent_id = uuid.uuid4().hex[:12]
        self.store.add_pending({
            "intent_id": intent_id, "kind": kind, "content_hash": ch,
            "text": text, "sector": sector, "hook": hook,
            "target_did": target_did, "target_handle": target_handle,
            "learnable": learnable, "ts": time.time(),
        })
        try:
            uri = write_fn(text)
        except exceptions.AtProtocolError as e:
            found = self.net.find_post(ch)
            if found and found[0]:
                uri = found[0]
                logger.info(f"      [RECONCILE] {kind} raised {type(e).__name__} but "
                            f"post landed at {uri[:60]}; treating as success")
                return intent_id, uri
            raise
        return intent_id, uri

    def _reconcile_pending(self):
        """Resolve any unfinalized intents from earlier ticks or a prior
        process. For each, scan our author feed by content_hash. If found,
        record a ledger entry (unless one already exists for that URI) and
        clear the intent. Intents older than PENDING_GRACE_SECONDS that still
        cannot be located are dropped: we assume they never landed."""
        pendings = self.store.list_pending()
        if not pendings:
            return
        logger.info(f"[RECONCILE] checking {len(pendings)} pending write(s)")
        for p in pendings:
            ch = p.get("content_hash") or content_hash(p.get("text", "") or "")
            found = self.net.find_post(ch)
            if found and found[0]:
                uri = found[0]
                already = any(a.get("uri") == uri for a in self.store.ledger)
                if not already:
                    self.store.log_action(
                        p.get("kind"), p.get("sector"), p.get("hook"),
                        uri=uri,
                        target_did=p.get("target_did"),
                        target_handle=p.get("target_handle"),
                        text=p.get("text"),
                        learnable=p.get("learnable", True),
                    )
                self.store.mark_seen(ch)
                self.store.remove_pending(p["intent_id"])
                logger.info(f"   [RECONCILE] {p.get('kind')} -> {uri[:60]}")
            elif (time.time() - p.get("ts", 0)) > PENDING_GRACE_SECONDS:
                logger.info(f"   [RECONCILE] dropping stale {p.get('kind')} intent "
                            f"({int(time.time() - p.get('ts', 0))}s old, never landed)")
                self.store.remove_pending(p["intent_id"])

    # ---- main tick ----
    def tick(self):
        self.store.tick += 1
        t = self.store.tick
        # Reset per-tick state up front so update_stall_counter (which runs in
        # the runtime loop even when tick() raises) sees consistent values.
        # _last_action carries across ticks so status.json keeps showing the
        # last meaningful action; it is only overwritten when something new
        # actually happens.
        self._tick_actions = 0
        self._tick_active = False
        if not hasattr(self, "_last_action"):
            self._last_action = None
        logger.info(f"\n{'='*60}\n[TICK {t}] {time.strftime('%Y-%m-%d %X')}\n{'='*60}")

        if self._halted():
            logger.info("[HALTED] kill switch on. No network calls.")
            return
        if self.breaker.is_open():
            logger.info("[BREAKER] open. Skipping tick.")
            return
        # Past the gates: this tick is genuinely trying to do work, so the
        # stall counter should evaluate it.
        self._tick_active = True

        # Resolve any pending intents from a prior tick or process before we
        # start generating new content, so the dedup gates see the right state.
        self._reconcile_pending()

        if not self._sense():
            return
        if t % 5 == 1:
            self._sense_trends()

        self._learn()
        self.store.decay()
        self._maybe_optimize_profile()

        sector, post_hook, reply_hook = self._decide()
        self._act(sector, post_hook, reply_hook)

        if t % 4 == 0:
            self._courtesy_follow_back()

    def _mark_action(self, kind, **details):
        """Single touch-point for every successful network action: bumps the
        per-tick action counter (drives the stall detector) and records the
        most recent action for status.json so an off-box monitor can see what
        the agent just did without parsing logs."""
        self._tick_actions += 1
        self._last_action = {"kind": kind, "ts": time.time(), **details}

    def update_stall_counter(self):
        """Runs from the runtime loop after every tick (including when the
        tick raised). If a genuinely-active tick produced zero successful
        network actions, increment the empty-tick counter; once it reaches
        STALL_THRESHOLD, force the breaker open so the daemon stops chewing
        cycles silently instead of looking alive while doing nothing.

        Skipped when the tick never became active (halted, breaker already
        open). On any action, the counter resets."""
        if not getattr(self, "_tick_active", False):
            return
        if getattr(self, "_tick_actions", 0) == 0:
            self.store.consecutive_empty_ticks += 1
            logger.warning(f"   [STALL] empty tick "
                           f"{self.store.consecutive_empty_ticks}/{STALL_THRESHOLD}")
            if self.store.consecutive_empty_ticks >= STALL_THRESHOLD:
                logger.error(f"[STALL] {STALL_THRESHOLD} consecutive empty ticks; "
                             f"tripping breaker to surface the problem")
                self.breaker.trip_open(reason=f"stall ({STALL_THRESHOLD} empty ticks)")
        else:
            if self.store.consecutive_empty_ticks:
                logger.info(f"   [STALL] action observed, resetting empty counter "
                            f"(was {self.store.consecutive_empty_ticks})")
            self.store.consecutive_empty_ticks = 0
        self.store.save_engine()

    # ---- SENSE ----
    def _sense(self) -> bool:
        try:
            followers = self.net.follower_count()
            self.breaker.record_success()
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] follower read failed: {e}")
            self.breaker.record_failure()
            return False

        prev = self.store.snapshots[-1]["followers"] if self.store.snapshots else followers
        delta = followers - prev
        self.store.ewma_growth = 0.5 * delta + 0.5 * self.store.ewma_growth
        self.store.snapshots.append({"ts": time.time(), "tick": self.store.tick,
                                     "followers": followers, "delta": delta})
        self.store.save_snapshots()
        self.store.save_engine()
        logger.info(f"[SENSE] followers={followers} (delta {delta:+d}) ewma={self.store.ewma_growth:+.2f}")
        if followers >= FOLLOWER_TARGET:
            logger.info(f"[GOAL] reached {followers} >= {FOLLOWER_TARGET}. Continuing to compound.")

        # Full sector scan every other tick; reuse the cache otherwise. Cuts API calls
        # and feeds both _decide (activity) and _act (candidate reuse).
        if self.store.tick % 2 == 1 or not self.sector_activity:
            logger.info("[SENSE] scanning network activity across sectors")
            self.sector_activity, self.sector_posts = {}, {}
            for sector in SECTORS:
                keyword = random.choice(KEYWORD_MAP[sector])
                try:
                    posts = self.net.search_posts(keyword, limit=12)
                    self.breaker.record_success()
                except exceptions.AtProtocolError as e:
                    logger.info(f"   {sector}: scan failed ({e})")
                    self.breaker.record_failure()
                    self.sector_activity[sector] = {"keyword": keyword, "total": 0}
                    self.sector_posts[sector] = []
                    continue
                self.sector_posts[sector] = posts
                self.sector_activity[sector] = {"keyword": keyword, "total": len(posts)}
                label = "hot" if len(posts) >= 7 else "warm" if len(posts) >= 3 else "quiet"
                logger.info(f"   {sector}: {len(posts)} posts for '{keyword}' [{label}]")
        return True

    def _sense_trends(self):
        hottest, best = None, -1
        for sector, data in self.sector_activity.items():
            if data.get("total", 0) > best:
                best, hottest = data["total"], sector
        if not hottest or best == 0:
            return
        posts = self.sector_posts.get(hottest, [])
        texts = [p.record.text for p in posts if getattr(p.record, "text", None)][:10]
        if not texts:
            return
        batch = "\n---\n".join(texts)
        prompt = (
            f"These are recent posts in the '{hottest}' space:\n{batch}\n\n"
            f"Extract exactly 3 specific, trending keywords or concepts people are "
            f"actively discussing. Respond strictly as JSON: "
            f'{{"keywords": ["kw1","kw2","kw3"]}}'
        )
        raw = self._generate(prompt, dedup=False)
        if not raw:
            return
        try:
            kws = json.loads(raw).get("keywords", [])
            if kws:
                self.store.trends[hottest] = kws
                self.store.save_engine()
                logger.info(f"   [TRENDS] {hottest}: {kws}")
        except Exception as e:
            logger.warning(f"   [FAULT] trend parsing failed: {e}")

    # ---- LEARN ----
    def _learn(self):
        due = self.store.mature_actions()
        if not due:
            logger.info("[LEARN] nothing matured yet.")
            return
        logger.info(f"[LEARN] scoring {len(due)} matured action(s)")
        for a in due:
            try:
                if a["kind"] == "follow":
                    success = self.net.followed_back_by(a["target_did"])
                    label = "FOLLOW-BACK" if success else "no follow-back"
                else:  # post, reply, quote -> our own URI, engagement is attributable
                    success = self.net.post_engagement(a["uri"]) > 0
                    label = "engaged" if success else "no engagement"
                self.breaker.record_success()
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] scoring failed, retry next tick: {e}")
                self.breaker.record_failure()
                continue
            self.store.update("sector", a["sector"], success)
            if a["kind"] in ("post", "quote"):
                self.store.update("post_hook", a["hook"], success)
            elif a["kind"] == "reply":
                self.store.update("reply_hook", a["hook"], success)
            self.store.mark_matured(a["id"])
            logger.info(f"   -> {a['kind']} [{a['sector']}/{a['hook']}]: {label}")

    # ---- profile optimization (data-sufficiency trigger) ----
    def _maybe_optimize_profile(self):
        if (self.store.tick - self.store.last_profile_opt_tick) < PROFILE_OPT_COOLDOWN_TICKS:
            return
        best_sector, best_e = None, -1
        for sector, arm in self.store.bandit["sector"].items():
            trials = (arm["alpha"] - 1) + (arm["beta"] - 1)
            e = arm["alpha"] / (arm["alpha"] + arm["beta"])
            if trials >= PROFILE_OPT_MIN_TRIALS and e > best_e:
                best_e, best_sector = e, sector
        if not best_sector:
            return
        logger.info(f"[OPTIMIZE] rewriting bio around best sector '{best_sector}'")
        trends_info = ""
        if best_sector in self.store.trends:
            trends_info = f"Weave in these trends if natural: {', '.join(self.store.trends[best_sector])}. "
        prompt = (
            f"Write a bio (max 160 chars) for {NAME_TEXT}. Our strongest content is "
            f"in '{best_sector}'. {trends_info}Use clear keywords for that area, "
            f"explain complex things simply, warm and approachable. Must end with "
            f"' Automated account.' No hashtags. "
            f'Respond strictly as JSON: {{"bio": "..."}}'
        )
        raw = self._generate(prompt, dedup=False)
        if not raw:
            return
        try:
            bio = json.loads(raw)["bio"][:256]
            self.net.set_profile(NAME_TEXT, bio)
            self.store.last_profile_opt_tick = self.store.tick
            self.store.save_engine()
        except Exception as e:
            logger.info(f"   [OPTIMIZE] failed: {e}")

    # ---- DECIDE ----
    def _decide(self):
        logger.info("[DECIDE] Thompson sampling (dead sectors excluded)")
        sector_samples = []
        for sector in SECTORS:
            arm = self.store.bandit["sector"][sector]
            s = random.betavariate(arm["alpha"], arm["beta"])
            active = self.sector_activity.get(sector, {}).get("total", 0) > 0
            logger.info(f"   sector {sector}: Beta({arm['alpha']:.1f},{arm['beta']:.1f}) "
                        f"sample={s:.3f} active={active}")
            sector_samples.append((s, sector, active))
        live = [x for x in sector_samples if x[2]]
        pool = live if live else sector_samples
        sector = max(pool, key=lambda x: x[0])[1]

        def best_arm(dim, values):
            best, pick = -1.0, values[0]
            for v in values:
                arm = self.store.bandit[dim][v]
                s = random.betavariate(arm["alpha"], arm["beta"])
                if s > best:
                    best, pick = s, v
            return pick

        post_hook = best_arm("post_hook", POST_HOOKS)
        reply_hook = best_arm("reply_hook", REPLY_HOOKS)
        logger.info(f"   -> sector={sector} post_hook={post_hook} reply_hook={reply_hook}")
        return sector, post_hook, reply_hook

    # ---- ACT ----
    def _current_phase(self):
        followers = self.store.snapshots[-1].get("followers", 0) if self.store.snapshots else 0
        for max_f, name, weights in GROWTH_PHASES:
            if followers < max_f:
                return name, weights
        return GROWTH_PHASES[-1][1], GROWTH_PHASES[-1][2]

    def _act(self, sector, post_hook, reply_hook):
        phase_name, weights = self._current_phase()
        logger.info(f"[ACT] phase={phase_name} weights={ {k: round(v,2) for k,v in weights.items()} }")

        # 1. Seed anchor posts up front; keep posting in later phases.
        should_post = (self.store.anchor_posts < ANCHOR_POST_TARGET
                       or phase_name in ("compound", "community", "scaling"))
        if should_post and self.rate["post"].try_consume():
            self._original_post(sector, post_hook)

        # 2. Candidates: reuse the sense-stage cache when possible; only search when
        #    a trend keyword overrides the cached sector keyword.
        candidates, keyword = self._candidates_for(sector)
        if not candidates:
            logger.info(f"[ACT] no relevant candidates for '{keyword}'. Retargeting next tick.")
            return
        logger.info(f"[ACT] {len(candidates)} relevant candidate(s) for '{keyword}'.")

        # 3. Weighted action plan. Each action type fires with prob ~ its weight.
        plan = [a for a, w in weights.items() if random.random() < min(1.0, w * 2.5)]
        if "like" not in plan:
            plan.append("like")
        random.shuffle(plan)
        for action in plan:
            if action == "follow" and self.rate["follow"].try_consume():
                self._strategic_follow(sector, post_hook, candidates)
            elif action == "reply" and self.rate["reply"].try_consume():
                self._helpful_reply(sector, reply_hook, candidates)
            elif action == "quote" and self.rate["quote"].try_consume():
                self._quote_best(sector, post_hook, candidates)
            elif action == "like":
                self._spray_likes(candidates)

    def _candidates_for(self, sector):
        trend_kw = (random.choice(self.store.trends[sector])
                    if sector in self.store.trends and self.store.trends[sector] else None)
        if trend_kw:
            keyword = trend_kw
            try:
                posts = self.net.search_posts(keyword)
                self.breaker.record_success()
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] market scan failed: {e}")
                self.breaker.record_failure()
                posts = []
        else:
            keyword = self.sector_activity.get(sector, {}).get("keyword", sector)
            posts = self.sector_posts.get(sector, [])
        cands = [c for c in posts
                 if getattr(c.author, "did", None) != self.net.did
                 and not self.store.already_acted_on(c.author.did)
                 and self._is_relevant_content(c)]
        return cands, keyword

    def _is_relevant_content(self, post) -> bool:
        text = ""
        if hasattr(post, "record") and hasattr(post.record, "text"):
            text = post.record.text or ""
        return is_relevant_text(text)

    # ---- like ----
    def _spray_likes(self, candidates):
        liked = 0
        for c in candidates:
            if liked >= MAX_LIKES_PER_TICK or not self.rate["like"].try_consume():
                break
            uri, cid = getattr(c, "uri", None), getattr(c, "cid", None)
            if not uri or not cid or self.store.already_acted_on(f"like:{uri}"):
                continue
            try:
                self.net.like(uri, cid)
                self.store.mark_seen(f"like:{uri}")
                self.breaker.record_success()
                liked += 1
                self._mark_action("like", uri=uri)
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] like failed: {e}")
                self.breaker.record_failure()
                break
        if liked:
            logger.info(f"   [LIKE] liked {liked} relevant post(s)")

    # ---- quote (replaces the misattributed plain repost) ----
    def _quote_best(self, sector, hook, candidates):
        best, best_eng = None, -1
        for c in candidates[:5]:
            uri, cid = getattr(c, "uri", None), getattr(c, "cid", None)
            if not uri or not cid or self.store.already_acted_on(f"quote:{uri}"):
                continue
            eng = ((getattr(c, "like_count", 0) or 0) + (getattr(c, "repost_count", 0) or 0)
                   + (getattr(c, "reply_count", 0) or 0))
            if eng > best_eng:
                best_eng, best = eng, c
        if not best:
            return
        src = (best.record.text or "")[:200]
        prompt = (
            f"This post is about '{sector}':\n\"{src}\"\n\n"
            f"Write one short comment (max 200 chars) to quote-post it, adding a "
            f"genuinely useful plain-language insight that builds on it. Use a "
            f"'{hook}' angle. {POST_HOOK_GUIDANCE.get(hook,'')} Never pitch anything. "
            f'Respond strictly as JSON: {{"comment": "..."}}'
        )
        raw = self._generate(prompt, dedup=True)
        text = None
        if raw:
            try:
                text = json.loads(raw)["comment"]
            except Exception:
                text = None
        handle = getattr(best.author, "handle", "unknown")
        if text and self._passes_gates(text):
            try:
                intent_id, uri = self._publish_with_reconcile(
                    kind="quote", text=text, sector=sector, hook=hook,
                    write_fn=lambda t: self.net.quote_post(t, best.uri, best.cid),
                    target_handle=handle,
                )
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] quote failed: {e}")
                self.breaker.record_failure()
                return
            self.breaker.record_success()
            self.store.mark_seen(f"quote:{best.uri}")
            self.store.mark_seen(content_hash(text))
            self.store.log_action("quote", sector, hook, uri=uri,
                                  target_handle=handle, text=text, learnable=True)
            self.store.remove_pending(intent_id)
            self._mark_action("quote", target_handle=handle, uri=uri)
            logger.info(f"   [QUOTE] @{handle}: {text[:70]}...")
            return
        # Fallback: plain repost as goodwill, NOT learnable (no attributable reward).
        try:
            self.net.repost(best.uri, best.cid)
            self.breaker.record_success()
            self.store.mark_seen(f"quote:{best.uri}")
            self.store.log_action("repost", sector, "n/a", uri=best.uri,
                                  target_handle=handle, learnable=False)
            self._mark_action("repost", target_handle=handle, uri=best.uri)
            logger.info(f"   [REPOST] reshared @{handle} (goodwill, non-learnable)")
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] repost failed: {e}")
            self.breaker.record_failure()

    # ---- follow ----
    def _strategic_follow(self, sector, hook, candidates):
        logger.info("   [FOLLOW] scoring candidates for follow-back likelihood")
        scored = []
        for c in candidates[:5]:
            did = getattr(c.author, "did", None)
            if not did:
                continue
            profile = self.net.get_profile(did)
            if profile is None:
                continue
            score, reason = self._score_follow_target(profile)
            handle = getattr(c.author, "handle", did)
            logger.info(f"      @{handle}: {score:.2f} ({reason})")
            if score > 0:
                scored.append((score, c, handle))
        if not scored:
            logger.info("   [FOLLOW] nothing scored above threshold.")
            return
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, target, handle = scored[0]
        try:
            self.net.follow(target.author.did)
            self.breaker.record_success()
            self.store.mark_seen(target.author.did)
            self.store.log_action("follow", sector, hook,
                                  target_did=target.author.did, target_handle=handle)
            self._mark_action("follow", target_handle=handle,
                              target_did=target.author.did)
            logger.info(f"   [FOLLOW] @{handle} (score={best_score:.2f}, awaiting follow-back)")
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] follow failed: {e}")
            self.breaker.record_failure()

    def _score_follow_target(self, profile):
        # Future externalization candidate: these thresholds live in code
        # for now because a weak soul could otherwise dial the agent to
        # follow anyone, and that is the easiest way to get rate-limited
        # or banned. Keep code-enforced until the soul-swap story is tested.
        followers = int(getattr(profile, "followers_count", 0) or 0)
        following = int(getattr(profile, "follows_count", 0) or 0)
        posts = int(getattr(profile, "posts_count", 0) or 0)
        bio = getattr(profile, "description", "") or ""
        handle = getattr(profile, "handle", "") or ""
        display = getattr(profile, "display_name", "") or ""

        viewer = getattr(profile, "viewer", None)
        if viewer and getattr(viewer, "followed_by", None):
            return (-1.0, "already follows us")
        if viewer and getattr(viewer, "following", None):
            return (-1.0, "we already follow them")
        if followers > 5000:
            return (-1.0, f"too large ({followers})")
        if posts < 3:
            return (-1.0, f"too few posts ({posts})")

        profile_text = f"{bio} {display} {handle}"
        hits = RELEVANCE_RE.findall(profile_text)
        if not hits:
            return (-1.0, "no domain relevance")
        score, reasons = 0.0, []
        if len(hits) >= 3:
            score += 4.0; reasons.append(f"strong match ({len(hits)})")
        elif len(hits) >= 2:
            score += 3.0; reasons.append(f"good match ({len(hits)})")
        else:
            score += 1.5; reasons.append("weak match")

        if following > 0:
            ratio = followers / following
            if ratio < 0.3:
                score += 4.0; reasons.append("high reciprocity")
            elif ratio <= 3.0:
                score += 3.0; reasons.append("reciprocal")
            else:
                score += 0.5; reasons.append("low reciprocity")
        else:
            score += 1.0; reasons.append("new account")

        if 50 <= followers <= 2000:
            score += 2.0; reasons.append("right size")
        elif 10 <= followers < 50:
            score += 1.0; reasons.append("small but real")
        elif followers < 10:
            score += 0.5

        if posts >= 20:
            score += 1.5; reasons.append("active")
        elif posts >= 5:
            score += 0.5
        if len(bio.strip()) > 20:
            score += 1.0; reasons.append("has bio")
        return (score, ", ".join(reasons))

    def _courtesy_follow_back(self):
        """Follow back recent followers we do not yet follow. Aids retention."""
        followers = self.net.recent_followers(limit=25)
        done = 0
        for f in followers:
            did = getattr(f, "did", None)
            if not did or self.store.already_acted_on(f"fb:{did}"):
                continue
            viewer = getattr(f, "viewer", None)
            if viewer and getattr(viewer, "following", None):
                self.store.mark_seen(f"fb:{did}")
                continue
            if not self.rate["follow"].try_consume():
                break
            try:
                self.net.follow(did)
                self.store.mark_seen(f"fb:{did}")
                self.breaker.record_success()
                done += 1
                self._mark_action("follow_back", target_did=did)
            except exceptions.AtProtocolError as e:
                logger.warning(f"   [FAULT] courtesy follow-back failed: {e}")
                self.breaker.record_failure()
                break
        if done:
            logger.info(f"   [RETAIN] followed back {done} new follower(s)")

    # ---- original post (multi-variant hooks) ----
    def _original_post(self, sector, hook):
        trends_info = ""
        if sector in self.store.trends and self.store.trends[sector]:
            trends_info = f"Weave in these trends if they fit: {', '.join(self.store.trends[sector])}. "
        prompt = (
            f"Write THREE distinct original Bluesky posts (each max 280 chars) about "
            f"'{sector}' using a '{hook}' angle. {POST_HOOK_GUIDANCE.get(hook,'')} "
            f"{trends_info}"
            f"Each must take a confusing concept from UX, design, or frontend and "
            f"explain it in plain words anyone could understand, using an everyday "
            f"analogy, so a reader thinks 'oh, THAT is what that means.' The three must "
            f"open with different first lines and cover different ideas. Warm and "
            f"friendly, never talking down. No pitch, no link, no emoji, no hashtag, "
            f"no em dash, no jargon left unexplained. Avoid sensitive topics. "
            f'Respond strictly as JSON: {{"variants": ["...","...","..."]}}'
        )
        raw = self._generate(prompt, dedup=True)
        if not raw:
            return
        try:
            variants = json.loads(raw).get("variants", [])
        except Exception as e:
            logger.warning(f"   [GATE] post JSON malformed: {e}. Skipping.")
            return
        candidates = [v for v in variants if self._passes_gates(v)]
        if not candidates:
            logger.warning("   [GATE] all post variants rejected. Skipping slot.")
            return
        text = max(candidates, key=hook_strength)  # pick the strongest hook
        try:
            intent_id, uri = self._publish_with_reconcile(
                kind="post", text=text, sector=sector, hook=hook,
                write_fn=self.net.post,
            )
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] post failed: {e}")
            self.breaker.record_failure()
            return
        cid = None
        try:
            resp = self.net.client.app.bsky.feed.get_post_thread({"uri": uri, "depth": 0})
            cid = getattr(getattr(resp.thread, "post", None), "cid", None)
        except Exception:
            pass
        self.breaker.record_success()
        self.store.anchor_posts += 1
        self.store.mark_seen(content_hash(text))
        self.store.log_action("post", sector, hook, uri=uri, text=text, learnable=True)
        self.store.remove_pending(intent_id)
        self._mark_action("post", uri=uri, sector=sector, hook=hook)
        logger.info(f"   [POST] anchor #{self.store.anchor_posts} (hook_strength="
                    f"{hook_strength(text):.1f}): {text[:70]}...")
        if not self.store.pinned and cid:
            self.net.pin_post(uri, cid)
            self.store.pinned = True
        self.store.save_engine()

    # ---- reply ----
    def _helpful_reply(self, sector, hook, candidates):
        limit = min(5, len(candidates))
        batch = ""
        for i, c in enumerate(candidates[:limit]):
            preview = (c.record.text or "")[:200] if getattr(c.record, "text", None) else "(empty)"
            batch += f"[{i}] @{c.author.handle}: {preview}\n\n"
        prompt = (
            f"These are live posts about '{sector}':\n\n{batch}\n"
            f"Pick the SINGLE post where a short, kind, helpful reply would make the "
            f"person feel heard and less stuck. Add real value: a clearer way to think "
            f"about their problem, a small concrete tip, or a good question. Use a "
            f"'{hook}' angle. {REPLY_HOOK_GUIDANCE.get(hook,'')} Explain any technical "
            f"idea in plain words with an everyday analogy. If the post is sensitive "
            f"(parenting, body image, mental health, religion, politics, money "
            f"struggles), set index to -1 and reply to an empty string. Do not pitch "
            f"anything. Do not say 'great post'. Max 280 chars. No emoji, hashtag, em dash.\n"
            f'Respond strictly as JSON: {{"index": int, "reply": "..."}}'
        )
        raw = self._generate(prompt, dedup=True)
        if not raw:
            return
        try:
            data = json.loads(raw)
            idx, text = int(data["index"]), data["reply"]
        except Exception as e:
            logger.warning(f"   [GATE] reply JSON malformed: {e}. Skipping.")
            return
        if idx < 0 or idx >= limit or not self._passes_gates(text):
            logger.warning("   [GATE] reply skipped (sensitive/range/quality).")
            return
        target = candidates[idx]
        try:
            if self.rate["like"].try_consume():
                self.net.like(target.uri, target.cid)
            intent_id, uri = self._publish_with_reconcile(
                kind="reply", text=text, sector=sector, hook=hook,
                write_fn=lambda t: self.net.reply(target, t),
                target_did=target.author.did,
                target_handle=target.author.handle,
            )
        except exceptions.AtProtocolError as e:
            logger.warning(f"   [FAULT] reply failed: {e}")
            self.breaker.record_failure()
            return
        self.breaker.record_success()
        self.store.mark_seen(target.author.did)
        self.store.mark_seen(content_hash(text))
        self.store.log_action("reply", sector, hook, uri=uri,
                              target_did=target.author.did,
                              target_handle=target.author.handle, text=text, learnable=True)
        self.store.remove_pending(intent_id)
        self._mark_action("reply", target_handle=target.author.handle, uri=uri)
        logger.info(f"   [REPLY] @{target.author.handle}: {text[:70]}...")

    # ---- generation + gates ----
    def _generate(self, prompt, dedup=False):
        if dedup:
            recent = self.store.recent_content_texts(5)
            if recent:
                prompt += ("\n\nDo NOT repeat the concepts, phrases, or angles of "
                           "these recent posts:\n" + "\n".join(f"- {t}" for t in recent))
        try:
            resp = self.ai.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": self.persona},
                          {"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"   [FAULT] generation failed: {e}")
            return None

    def _passes_gates(self, text) -> bool:
        if not text or not text.strip() or len(text) > 300:
            return False
        if self.store.already_acted_on(content_hash(text)):
            return False
        low = text.lower()
        if any(p in low for p in SPAM_PHRASES):
            return False
        if any(p in low for p in SENSITIVE_PHRASES):
            return False
        # word-boundary check for short risky tokens (avoids domain-term collisions)
        if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in SENSITIVE_WORDS):
            return False
        if any(ch in text for ch in ("\U0001F300", "\u2014")):  # emoji / em dash
            return False
        return True

    # ---- reporting ----
    def report(self):
        followers = self.store.snapshots[-1]["followers"] if self.store.snapshots else 0
        phase_name, _ = self._current_phase()
        logger.info(f"\n[REPORT] followers={followers}/{FOLLOWER_TARGET} "
                    f"anchor_posts={self.store.anchor_posts} phase={phase_name}")
        for dim, vals in self.store.bandit.items():
            for v, arm in vals.items():
                a, b = arm["alpha"], arm["beta"]
                trials = (a - 1) + (b - 1)
                wlb = wilson_lower_bound(a - 1, trials) if trials > 0 else 0.0
                logger.info(f"   {dim}/{v}: Beta({a:.1f},{b:.1f}) E={a/(a+b):.3f} "
                            f"Wilson_lb={wlb:.3f} n={trials:.0f}")
