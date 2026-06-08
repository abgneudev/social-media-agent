  # Kiloforge: Autonomous Cognitive Agent — Technical Documentation

  ---

  ## Table of Contents

  1. [Project Overview](#1-project-overview)
  2. [System Architecture & Design](#2-system-architecture--design)
  3. [Directory Structure & Navigation](#3-directory-structure--navigation)
  4. [Core Modules & Components](#4-core-modules--components)
  5. [Data Flow & State Management](#5-data-flow--state-management)
  6. [External Dependencies & Infrastructure](#6-external-dependencies--infrastructure)
  7. [Setup & Deployment Guide](#7-setup--deployment-guide)
  8. [Documentation Gaps](#8-documentation-gaps)

  ---

  ## 1. Project Overview

  **Kiloforge** is a fully autonomous, multi-platform social media growth agent targeting Bluesky and (optionally) Threads. It is not a cron-triggered posting script. Instead, it runs a continuous **Perception-Reasoning-Action (PRA) loop** driven by a **Thompson Sampling reinforcement learning** bandit that learns, in real time, which content strategies convert to genuine follower growth.

  | Attribute | Value |
  |---|---|
  | Primary target platform | Bluesky (AT Protocol) |
  | Secondary platform | Meta Threads (Graph API) |
  | Learning algorithm | Thompson Sampling (Beta-Binomial conjugate priors) |
  | Current niche ("soul") | UX Design / Frontend Engineering / Design Systems |
  | North star metric | Real followers, measured directly from the platform API |
  | Identity configuration | Fully decoupled into `soul.yaml` — swap the file to retarget any domain |

  **Core capabilities:**

  - Generates original content via LLM with multi-layer safety gating.
  - Autonomously follows, likes, replies, and quotes high-relevance accounts.
  - Learns which content archetypes and topics yield engagement and adjusts posteriors continuously.
  - Persists all state atomically so it survives crashes, restarts, and dyno rotations.
  - Manages Bluesky curated lists, profile optimization, and keyword discovery without human intervention.

  ---

  ## 2. System Architecture & Design

  ### 2.1 Architectural Pattern

  Kiloforge follows a **Plan-and-Act Cognitive Architecture** — an agent pattern from the autonomous systems literature that strictly separates perception, reasoning, and execution:

  ```
  ┌──────────────────────────────────────────────────────────────────────┐
  │                          KILOFORGE ENGINE                            │
  │                                                                      │
  │  ┌──────────┐    ┌─────────────┐    ┌──────────────┐                │
  │  │ PERCEIVE │───▶│    REASON   │───▶│     ACT      │                │
  │  │          │    │  (Strategist│    │  (OS Kernel) │                │
  │  │ Telemetry│    │  + Bandit)  │    │  Intent Queue│                │
  │  │ Firehose │    │             │    │  Rate Budgets│                │
  │  │ Trends   │    │  sector +   │    │  Platforms   │                │
  │  └──────────┘    │  hook select│    └──────────────┘                │
  │        ▲         └─────────────┘           │                        │
  │        │               ▲                   │                        │
  │        │               │                   ▼                        │
  │        │         ┌──────────────┐  ┌──────────────┐                │
  │        │         │    LEARN     │  │  SAFETY GATE │                │
  │        └─────────│  Thompson    │  │  Warden +    │                │
  │  Platform API    │  Sampling    │  │  Hard Floors │                │
  │  Follower Diff   │  Bandit      │  └──────────────┘                │
  │                  └──────────────┘                                   │
  └──────────────────────────────────────────────────────────────────────┘
  ```

  ### 2.2 High-Level Component Interaction

  ```
  run.py
    │
    ├─ loads soul.yaml (Soul dataclass)
    ├─ builds platform (BlueskyPlatform / OmniPlatform)
    ├─ starts firehose_daemon (background thread)
    └─ calls engine.orchestrate() every TICK_INTERVAL seconds
          │
          ├─ _sense()         reads follower count, scans sectors
          ├─ _sense_trends()  extracts trending keywords
          ├─ _learn()         matures actions, updates bandit posteriors
          ├─ _decide()        Thompson-samples sector + hook
          ├─ _act()           pops IntentQueue, executes via OmniPlatform
          ├─ _run_evolution() keyword discovery + pruning
          ├─ _run_analyzer()  background niche insights classification
          └─ _autonomous_curation()  list management
  ```

  ### 2.3 Multi-Platform Broadcasting

  The `OmniPlatform` adapter delegates all read operations to Bluesky (the primary signal source) and fans out write operations to every initialized platform simultaneously. Platform errors on secondary platforms fail gracefully as no-ops so the Bluesky path is never blocked.

  ```
  OmniPlatform
    ├── BlueskyPlatform   ← primary (reads + writes)
    └── ThreadsPlatform   ← secondary (writes only, no-op on unsupported calls)
  ```

  ### 2.4 Persistence Model

  All mutable state is JSON, written atomically via a `tempfile + os.replace` pattern. This guarantees:
  - No partial writes visible to readers.
  - Process restarts recover without data corruption.
  - State survives dyno rotation on cloud hosts (Render.com).

  ---

  ## 3. Directory Structure & Navigation

  ```
  c:\flutter\social-agent\
  │
  ├── run.py                         # Entry point — wires config, boots engine, runs main loop
  ├── soul.yaml                      # Identity, persona, hooks, sectors, niche keywords
  ├── requirements.txt               # Python dependencies
  ├── kiloforge.env.example          # Environment variable template
  ├── render.yaml                    # Render.com deployment configuration
  ├── runtime.txt                    # Python version pin for Render
  │
  ├── src/
  │   ├── core/
  │   │   ├── config.py              # Constants, logging, file paths, rate budgets, safety floors
  │   │   ├── engine.py              # FollowerEngine — the main PRA loop orchestrator (~2400 lines)
  │   │   ├── soul.py                # Soul dataclass, soul.yaml loader, regex signal compiler
  │   │   ├── store.py               # Persistent state: bandit, ledger, snapshots, seen-set
  │   │   ├── governance.py          # RateBudget (token bucket) + CircuitBreaker
  │   │   └── platform.py            # Abstract Platform base class
  │   │
  │   ├── intelligence/
  │   │   ├── prompts.py             # All LLM prompt builders (post, reply, quote, curation, etc.)
  │   │   ├── analyzer.py            # Niche insights: samples trending posts, classifies archetypes
  │   │   ├── strategy.py            # Bandit arm selection helpers, Wilson score
  │   │   ├── memory.py              # ChromaDB semantic memory (episodic + knowledge base)
  │   │   ├── web_research.py        # Serper API integration for live industry trend research
  │   │   └── meta_critic.py         # Strategy evaluation utilities
  │   │
  │   ├── platforms/
  │   │   ├── platform.py            # Abstract base (interface definition)
  │   │   ├── bluesky.py             # AT Protocol adapter (reads + writes + list management)
  │   │   ├── threads.py             # Meta Graph API adapter
  │   │   └── omni.py                # Multi-platform broadcaster
  │   │
  │   ├── clients/
  │   │   ├── llm.py                 # Unified LLM client wrapper (Groq, Gemini, OpenAI routing)
  │   │   └── serper.py              # Serper web search client
  │   │
  │   ├── utils/
  │   │   ├── warden.py              # Content safety: Groq Safeguard + hard-floor phrase blocking
  │   │   ├── utils.py               # General utility functions
  │   │   └── breaker.py             # Circuit breaker helpers
  │   │
  │   └── daemons/
  │       └── firehose_daemon.py     # Background thread consuming real-time AT Protocol firehose
  │
  ├── data/
  │   ├── account_snapshots.json     # Follower count history (per-tick snapshots)
  │   ├── action_ledger.json         # Append-only log of every action taken
  │   ├── bandit_state.json          # Thompson Sampling Beta distribution posteriors
  │   ├── circuit_breaker.json       # Persisted breaker state (survives restarts)
  │   ├── curated_list.json          # Autonomous list membership state
  │   ├── dynamic_strategy.json      # Active multi-step strategic plan from the Strategist
  │   ├── engine_state.json          # Tick counter, growth phase, trends, keyword telemetry
  │   ├── network_telemetry.json     # Real-time engagement signals from the firehose
  │   ├── pending_writes.json        # Crash-safe intent queue (cleared after network ack)
  │   ├── seen_targets.json          # Deduplication set (DIDs + post URIs already acted on)
  │   ├── status.json                # Heartbeat blob for operator monitoring
  │   ├── web_insights.json          # Experimental hooks from Serper web research
  │   └── chroma_db/                 # ChromaDB vector store (semantic memory)
  │
  ├── tests/                         # Test suite for src/ (20+ files)
  │
  └── newagent/                      # Parallel modernized implementation (SQLite-backed, in progress)
      ├── kiloforge/                 # Refactored engine core
      ├── scripts/                   # Entry points + smoke tests
      └── tests/                     # 30+ comprehensive test files
  ```

  ---

  ## 4. Core Modules & Components

  ### 4.1 `src/core/engine.py` — FollowerEngine (The OS Kernel)

  The central orchestrator. Manages the PRA loop, the intent queue, token budgets, and delegates to all other subsystems. It is deliberately devoid of strategic reasoning — strategy is produced by the Bandit + Analyzer and injected as an `IntentQueue`.

  **Key classes:**

  | Class | Responsibility |
  |---|---|
  | `IntentQueue` | Priority queue for micro-actions (follow, like, post, reply, quote) |
  | `FollowerEngine` | Main orchestrator; owns the PRA loop |

  **Key `FollowerEngine` methods:**

  | Method | PRA Phase | Description |
  |---|---|---|
  | `orchestrate()` | Coordinator | Single tick entry point; calls all phases in order |
  | `_sense()` | Perception | Reads follower count, polls sector keywords for activity |
  | `_sense_trends()` | Perception | Extracts trending keywords from the hot sector |
  | `_learn()` | Learning | Matures actions from ledger, updates bandit posteriors |
  | `_decide()` | Reasoning | Thompson-samples sector + hook combination |
  | `_act()` | Action | Pops from IntentQueue, executes via platform adapter |
  | `_original_post()` | Action | Generates 3 divergent variants, selects the safest |
  | `_strategic_follow()` | Action | Scores candidates by bio + reciprocity heuristics |
  | `_helpful_reply()` | Action | Generates contextual replies to high-engagement posts |
  | `_quote_best()` | Action | Quotes the highest-engagement candidate post |
  | `_spray_likes()` | Action | Likes multiple verified posts within the rate budget |
  | `_run_evolution()` | Meta | Discovers, expands, and prunes keyword vocabulary |
  | `_autonomous_curation()` | Meta | Creates and populates Bluesky curated lists |
  | `_maybe_optimize_profile()` | Meta | Rewrites bio when data is sufficient |
  | `bootstrap()` | Init | First-run setup (initial follows, profile setup) |
  | `dry_run_post()` | Debug | Generates variants without any network writes |

  ---

  ### 4.2 `src/core/soul.py` — Soul & Identity

  Parses `soul.yaml` into a typed `Soul` dataclass. All identity and domain configuration flows through this object — the engine never reads `soul.yaml` directly.

  **Key responsibilities:**

  - Validates all required fields at load time (fail-closed).
  - Compiles relevance signals into a pre-built regex for fast content filtering.
  - Merges hard-floor safety lists with any soul-level additions.
  - Exposes `is_relevant_text(text)` used by the firehose daemon.

  ---

  ### 4.3 `src/core/store.py` — Persistent State Store

  Owns all durable engine state. Uses atomic JSON writes for every mutation.

  **State managed:**

  | Field | Type | Purpose |
  |---|---|---|
  | `bandit` | `dict[dim][arm] = {alpha, beta}` | Thompson Sampling posteriors per sector/hook |
  | `ledger` | `list[action]` | Immutable action log (post, follow, like, reply, quote) |
  | `snapshots` | `list[{ts, followers}]` | Follower count history for growth attribution |
  | `seen` | `set[str]` | DIDs and post URIs already acted on (deduplication) |
  | `pending` | `list[intent]` | Crash-safe write queue |
  | `tick` | `int` | Monotonically increasing tick counter |
  | `phase` | `str` | Current growth phase (cold_start, first_proof, etc.) |
  | `trends` | `dict[sector, list[keyword]]` | Trending keywords per sector |

  **Key methods:**

  | Method | Description |
  |---|---|
  | `update(dim, value, reward)` | Beta-Binomial posterior update |
  | `decay()` | Per-tick posterior decay (POST_DECAY = 0.9999) |
  | `log_action(kind, ...)` | Append to immutable ledger |
  | `mature_actions()` | Return actions past their attribution window |
  | `already_acted_on(key)` | Deduplication check |
  | `mark_seen(key)` | Add to deduplication set |

  ---

  ### 4.4 `src/core/governance.py` — Rate Budgets & Circuit Breaker

  **`RateBudget` (Token Bucket):**

  Limits per-action-type throughput. Each action type has its own bucket with independent capacity and refill rate. The engine consumes tokens before executing; if the bucket is empty, the action is skipped for this tick.

  | Action | Capacity | Refill Rate |
  |---|---|---|
  | `follow` | 5 tokens | 1 per 60s |
  | `reply` | 2 tokens | 1 per 200s |
  | `like` | 8 tokens | 1 per 30s |
  | `post` | 2 tokens | 1 per 300s |
  | `quote` | 2 tokens | 1 per 300s |

  **`CircuitBreaker`:**

  Trips to `OPEN` after `CIRCUIT_BREAKER_THRESHOLD` (3) consecutive failures, blocking all engine actions during a `CIRCUIT_BREAKER_COOLDOWN` (20-minute) cooling period. State persists across restarts via `circuit_breaker.json`.

  ---

  ### 4.5 `src/intelligence/analyzer.py` — Niche Analyzer

  Runs periodically (every `ANALYZER_CADENCE_TICKS`) as a background call. Samples high-engagement posts from each sector, classifies them by content archetype using an LLM call, and writes a distributional summary to `niche_insights.json`. The engine uses this summary to apply a small nudge (`EXPLORATION_NUDGE_MAX = 0.6`) to bandit alpha values — amplifying arms that are trending without overriding the bandit's learned posteriors.

  **Output schema (niche_insights.json):**

  ```json
  {
    "ts": 1717800000.0,
    "sample_size": 24,
    "archetype_traction": {
      "the_system_glitch": 8,
      "the_translation_layer": 6,
      "i_was_wrong": 4
    },
    "topic_angles": [
      "z-index wars causing the UI to explode",
      "prop drilling melting the component tree"
    ]
  }
  ```

  ---

  ### 4.6 `src/intelligence/memory.py` — Semantic Memory (ChromaDB)

  Provides episodic and knowledge-base memory using ChromaDB's persistent vector store. All four collections use cosine similarity (L2 distance) for retrieval.

  | Collection | Purpose | Usage |
  |---|---|---|
  | `interactions` | Past conversations with specific users | Prevents repetitive replies; personalizes follow-ups |
  | `self_threads` | The agent's own successful posts | Narrative arc continuity |
  | `swipe_file` | High-engagement posts from the firehose | Pattern learning for content generation |
  | `knowledge_base` | Authoritative domain facts | Injected into content generation prompts for grounding |

  ---

  ### 4.7 `src/utils/warden.py` — Content Safety

  Every piece of generated content passes through a two-phase safety gate before any network write:

  1. **Hard-floor blocking** (deterministic): Rejects content containing em dashes, URLs, spam phrases (`"buy "`, `"dm me"`, `"click here"`), sensitive words (`"democrat"`, `"woke"`, `"sexual"`), or sensitive phrases (`"vote for"`, `"lose weight"`). These lists are code-level constants; `soul.yaml` can extend but never shorten them.

  2. **Groq Safeguard moderation** (LLM-driven): Passes the text through a custom policy that flags prompt injection, political content, NSFW material, and hate speech. Returns `{"is_safe": true/false}`.

  3. **Semantic summary generation**: If the text is destined for prompt injection (e.g., reply context), the warden strips formatting and generates a clean semantic summary before passing it downstream.

  ---

  ### 4.8 `src/platforms/bluesky.py` — Bluesky Adapter

  Thin wrapper around the `atproto` SDK. Implements the full `Platform` interface.

  **Read operations:** `follower_count`, `search_posts`, `search_actors`, `fetch_timeline`, `get_author_feed`, `get_profile`, `post_engagement`, `followed_back_by`, `recent_followers`, `get_all_follows`

  **Write operations:** `post`, `post_with_image`, `reply`, `quote_post`, `like`, `follow`, `repost`, `mute_actor`, `set_profile`, `create_list`, `add_to_list`

  **Crash-safety:** `find_post(content_hash)` reconciles write responses that were lost mid-flight by scanning the author feed for matching content.

  ---

  ### 4.9 `src/platforms/omni.py` — Multi-Platform Broadcaster

  Wraps a list of `Platform` instances. Delegates all read calls to the first platform (Bluesky). For write calls, iterates all platforms and catches per-platform exceptions as warnings, ensuring a failure on Threads never blocks the Bluesky write path.

  ---

  ### 4.10 `src/daemons/firehose_daemon.py` — Real-Time Telemetry

  A background thread that subscribes to the AT Protocol firehose via WebSocket. It filters incoming events using `soul.is_relevant_text()` and accumulates engagement signals into `network_telemetry.json`. The engine reads this file each tick to build its sector activity heatmap.

  ---

  ### 4.11 `newagent/` — Modernized Implementation (In Progress)

  A parallel rewrite of the core engine with key architectural improvements:

  - **SQLite-backed state** instead of flat JSON files.
  - **Explicit job queue with leases** (`queue.py`) for crash-safe execution.
  - **Provider abstraction** (`provider.py`) for pluggable LLM backends.
  - **Dry-run adapter** (`adapters/dryrun.py`) for full offline testing.
  - **Comprehensive test suite** (30+ test files, including end-to-end integration).

  This implementation is not yet the production entry point (`run.py` still boots `src/`).

  ---

  ## 5. Data Flow & State Management

  ### 5.1 Primary Data Lifecycle: Content Generation

  ```
  tick()
    │
    ├─ _sense()
    │    └── BlueskyPlatform.follower_count() → store.snapshots
    │
    ├─ _learn()
    │    ├── store.mature_actions()    → reads ledger, checks attribution window
    │    ├── compute follower delta    → reward = 1 if followers grew, else 0
    │    └── store.update(dim, arm, reward) → updates Beta(alpha, beta) posteriors
    │
    ├─ _decide()
    │    ├── Thompson sample sector   → betavariate(alpha, beta) for each arm
    │    ├── Thompson sample hooks    → same for post_hooks and reply_hooks
    │    └── apply nudges             → archetype_nudges from niche_insights.json
    │
    ├─ _act(sector, hook)
    │    ├── rate_budget.try_consume("post")
    │    ├── _original_post(sector, hook)
    │    │    ├── prompts.build_variant_prompt(soul, sector, archetypes, ...)
    │    │    │    └── injects: persona, trends, web_insights, learned_signals, kb_hist
    │    │    ├── llm.generate(prompt)            → raw JSON from LLM
    │    │    ├── warden.hard_floor_check(text)   → blocks bad phrases
    │    │    ├── warden.groq_safeguard(text)     → LLM moderation
    │    │    └── platform.post(text)             → network write
    │    │         └── store.log_action("post", sector, hook, uri)
    │    └── ...same pattern for reply, quote, like, follow
    │
    └─ store.save_engine()  → atomic write of engine_state.json
  ```

  ### 5.2 Learning Loop: Thompson Sampling

  ```
  INITIALIZE:  alpha=1, beta=1 for every arm (uniform prior)

  EACH TICK:
    1. DECAY:    alpha = 1 + (alpha-1)*0.9999
                beta  = 1 + (beta-1)*0.9999
                (keeps recent evidence relevant, decays old data)

    2. SAMPLE:   for each arm, draw s ~ Beta(alpha, beta)
                select arm with highest s

    3. ACT:      execute action tagged with (sector, hook)

  AFTER ATTRIBUTION WINDOW (24h):
    4. OBSERVE:  follower_delta = current_followers - snapshot_at_action_time
                reward = 1.0 if follower_delta > 0 else 0.0

    5. UPDATE:   alpha += reward
                beta  += (1 - reward)
  ```

  ### 5.3 Growth Phase Transitions

  The engine transitions through growth phases based on follower count. Each phase shifts the probability distribution over action types:

  | Phase | Followers | Follow% | Like% | Reply% | Post% | Quote% |
  |---|---|---|---|---|---|---|
  | cold_start | 1–4 | 55 | 20 | 10 | 10 | 5 |
  | first_proof | 5–9 | 45 | 20 | 15 | 15 | 5 |
  | early_traction | 10–19 | 35 | 20 | 20 | 15 | 10 |
  | momentum | 20–49 | 25 | 15 | 25 | 20 | 15 |
  | scaling | 50–99 | 15 | 10 | 30 | 25 | 20 |
  | authority | 100+ | 10 | 10 | 25 | 35 | 20 |

  ### 5.4 Crash-Safe Write Protocol

  All network writes use a pending-intent queue to prevent double-posting:

  ```
  1. generate content
  2. atomic_write({ intent_id, kind, text, content_hash, ts }) → pending_writes.json
  3. platform.write(text)    ← if this crashes, step 2 record survives
  4. on restart: scan pending_writes.json
    └── for each pending intent:
          └── platform.find_post(content_hash)
                ├── found → remove from pending (write landed, response was lost)
                └── not found → re-execute the write
  5. atomic_remove(intent_id) from pending_writes.json
  ```

  ---

  ## 6. External Dependencies & Infrastructure

  ### 6.1 Runtime Dependencies (`requirements.txt`)

  | Package | Version | Role |
  |---|---|---|
  | `atproto` | >=0.0.46 | AT Protocol SDK — Bluesky authentication, feed reads, post/follow/like writes, list management, firehose |
  | `groq` | >=0.11.0 | Groq SDK — fast LLM inference (content generation) and Groq Safeguard moderation |
  | `PyYAML` | >=6.0 | `soul.yaml` parsing |
  | `chromadb` | >=0.4.0 | Local persistent vector store — semantic memory for interactions and knowledge base |
  | `openai` | >=1.0.0 | OpenAI-compatible client (used for Gemini or fallback LLM routing via `clients/llm.py`) |

  > **Note:** `websockets` is an implicit dependency for `firehose_daemon.py`. It is not listed in `requirements.txt`. See [Documentation Gaps](#8-documentation-gaps).

  ### 6.2 External APIs

  | Service | Credential | Usage |
  |---|---|---|
  | Bluesky (AT Protocol) | `BLUESKY_HANDLE`, `BLUESKY_PASSWORD` | Primary platform — all reads, all writes, firehose |
  | Groq | `GROQ_API_KEY` | Content generation (fast inference) + Groq Safeguard content moderation |
  | Google Gemini | `GEMINI_API_KEY` | Versatile fallback LLM for longer reasoning tasks |
  | Meta Threads Graph API | `THREADS_USER_ID`, `THREADS_ACCESS_TOKEN` | Secondary broadcast platform (optional) |
  | Serper | (not in env.example, inferred from `serper.py`) | Live web search for industry trend research |
  | Klipy | `KLIPY_APP_KEY` (optional) | GIF attachment on original posts; degrades gracefully to text-only |

  ### 6.3 Infrastructure

  | Component | Details |
  |---|---|
  | Deployment target | Render.com (configured via `render.yaml`) |
  | Persistent storage | Render ephemeral disk mounted at `/state`, env `KF_STATE_DIR=/state` |
  | Python version | Pinned in `runtime.txt` |
  | Process model | Single long-running process; tick loop in main thread; firehose in background thread |
  | Logging | Structured stderr (ISO timestamp + level); captured by systemd journal or Render log viewer |
  | Kill switch | Write `"HALTED"` to `{STATE_DIR}/engine_status.txt` to pause execution without killing the process |

  ---

  ## 7. Setup & Deployment Guide

  ### 7.1 Local Installation

  ```bash
  # 1. Clone the repository
  git clone <repo-url>
  cd social-agent

  # 2. Create and activate a virtual environment
  python -m venv venv
  # Windows
  venv\Scripts\activate
  # macOS / Linux
  source venv/bin/activate

  # 3. Install dependencies
  pip install -r requirements.txt

  # Note: also install websockets for the firehose daemon
  pip install websockets
  ```

  ### 7.2 Environment Variables

  Copy `kiloforge.env.example` to `.env` in the project root and fill in real values.

  | Variable | Required | Description |
  |---|---|---|
  | `BLUESKY_HANDLE` | Yes | Your Bluesky handle, e.g. `yourname.bsky.social`. No leading `@`. |
  | `BLUESKY_PASSWORD` | Yes | A Bluesky **App Password** (not your login password). Generate at bsky.app/settings/app-passwords. |
  | `GROQ_API_KEY` | Yes | Groq API key. Obtain at console.groq.com/keys. Used for all LLM inference and content moderation. |
  | `GEMINI_API_KEY` | Recommended | Google Gemini API key. Used as a fallback LLM for longer reasoning prompts. |
  | `THREADS_USER_ID` | No | Meta Threads numeric user ID. Required only for OmniPlatform Threads broadcast. |
  | `THREADS_ACCESS_TOKEN` | No | Meta Graph API access token. Required only for Threads broadcast. |
  | `KLIPY_APP_KEY` | No | Klipy developer key for GIF attachments on posts. Omit for text-only posts. |
  | `KF_STATE_DIR` | No | Override state file directory. Defaults to `./data`. Set to `/state` on Render.com. |
  | `KILOFORGE_LOG_LEVEL` | No | Log verbosity. One of `DEBUG`, `INFO`, `WARNING`. Defaults to `INFO`. |

  ### 7.3 Customizing the Identity (`soul.yaml`)

  All niche, persona, and content strategy configuration lives in `soul.yaml`. Edit it to retarget the agent to any domain without touching Python code.

  Key fields to customize:

  | Field | Description |
  |---|---|
  | `name` | Display name used in the profile |
  | `bio` | Profile bio text |
  | `persona` | Full LLM system-prompt persona description |
  | `sectors` | List of topic sectors the agent targets (maps to bandit arms) |
  | `core_relevance_signals` | Keywords used by the firehose filter and relevance scoring |
  | `post_hooks` | List of content archetype names (maps to bandit arms) |
  | `post_hook_guidance` | Per-hook writing instructions injected into LLM prompts |
  | `reply_hooks` | Reply strategy names |
  | `reply_hook_guidance` | Per-reply-hook instructions |
  | `topic_angle_examples` | Seed examples for topic angle generation |

  ### 7.4 Running Locally

  ```bash
  # Dry run: generate content variants without any network writes
  # Requires only GROQ_API_KEY
  python run.py --dry-run
  python run.py --dry-run design_systems   # Specify a sector

  # Live run: full engine with all platform writes
  python run.py
  ```

  ### 7.5 Deployment on Render.com

  1. Push the repository to GitHub.
  2. Create a new **Background Worker** service on Render.com pointing at the repo.
  3. Set all environment variables in the Render dashboard (see table above).
  4. Add a persistent disk mounted at `/state` and set `KF_STATE_DIR=/state`.
  5. Render will use `render.yaml` and `runtime.txt` to configure the build and start commands.

  The engine writes a heartbeat to `status.json` each tick. You can monitor it via:

  ```bash
  cat /state/status.json
  ```

  ### 7.6 Kill Switch

  To pause the engine without killing the process:

  ```bash
  echo "HALTED" > /state/engine_status.txt
  ```

  To resume:

  ```bash
  rm /state/engine_status.txt
  ```

  ---

  ## 8. Documentation Gaps

  The following items were identified as ambiguous or missing during analysis:

  | Gap | Location | Notes |
  |---|---|---|
  | `websockets` not in `requirements.txt` | `requirements.txt` | `firehose_daemon.py` uses `websockets` for the AT Protocol firehose subscription but the package is not listed as a dependency. Installation will fail at runtime without it. |
  | `SERPER_API_KEY` not in `kiloforge.env.example` | `kiloforge.env.example` | `src/clients/serper.py` and `src/intelligence/web_research.py` reference a Serper API key, but no env variable name or documentation is provided for it. |
  | `newagent/` production readiness | `newagent/` | The `newagent/` directory contains a substantially complete parallel implementation (SQLite-backed, with 30+ tests), but it is unclear whether it is intended to replace `src/` or is an experimental branch. No `run.py` equivalent for `newagent/` was found at the root level. |
  | LLM model routing logic | `src/clients/llm.py` | `config.py` defines `LLM_MODEL_FAST`, `LLM_MODEL_REASONING`, and `LLM_MODEL_VERSATILE_FALLBACK` all pointing to `mistral/mistral-small-latest`. The actual routing logic between Groq, Gemini, and OpenAI inside `llm.py` was not explored in full detail. |
  | `src/intelligence/learner.py` and `llm_judge.py` | `src/intelligence/` | These files are referenced in the directory overview but their presence and contents were not confirmed in the explored source tree. They may not yet exist. |
  | Threads platform capabilities | `src/platforms/threads.py` | The Threads adapter exists but its full method surface was not explored. The README states that unsupported Threads API calls (e.g., list curation) should fail as no-ops; the extent of this coverage is unverified. |
  | Test coverage for `src/` | `tests/` | The test directory for the legacy `src/` implementation exists with 20+ files, but the tests were not read. It is unclear whether coverage is comprehensive or partial. |
  | `render.yaml` full contents | `render.yaml` | Referenced in deployment documentation but not fully explored. Build commands and disk mount configuration were not verified. |
