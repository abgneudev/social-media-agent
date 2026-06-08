# Kiloforge: Autonomous Cognitive Agent

Kiloforge is a fully autonomous, multi-platform social media growth agent currently targeting Bluesky and Meta Threads. It does not operate on hardcoded scripts or simple cron-triggered posting schedules. Instead, it utilizes a state-of-the-art Cognitive Architecture driven by a continuous Perception-Reasoning-Action (PRA) loop.

The agent learns autonomously using a Thompson Sampling reinforcement learning algorithm to discover which content angles and niche sectors convert to real engagement, dynamically adjusting its future strategies without human intervention.

**North Star:** Real followers, measured directly from the platform API. The AI only learns from outcomes mathematically attributable to its own actions.
**Identity and Niche:** Fully decoupled into `soul.yaml`. The current configuration targets UX Design, Frontend Engineering, and Design Systems. Swap the file to retarget any domain.

## Table of Contents

1. System Architecture
2. Directory Structure
3. Core Modules
4. Data Flow and State Management
5. External Dependencies
6. Setup and Deployment
7. Known Issues and Migration Status

---

## System Architecture

Kiloforge follows a Plan-and-Act Cognitive Architecture, strictly separating perception, reasoning, and execution.

```text
┌──────────────────────────────────────────────────────────────────────┐
│                          KILOFORGE ENGINE                            │
│                                                                      │
│  ┌──────────┐    ┌─────────────┐    ┌──────────────┐                 │
│  │ PERCEIVE │───▶│    REASON   │───▶│     ACT      │                 │
│  │          │    │  (Strategist│    │  (OS Kernel) │                 │
│  │ Telemetry│    │  + Bandit)  │    │  Intent Queue│                 │
│  │ Firehose │    │             │    │  Rate Budgets│                 │
│  │ Trends   │    │  sector +   │    │  Platforms   │                 │
│  └──────────┘    │  hook select│    └──────────────┘                 │
│        ▲         └─────────────┘           │                         │
│        │               ▲                   │                         │
│        │               │                   ▼                         │
│        │         ┌──────────────┐  ┌──────────────┐                  │
│        │         │    LEARN     │  │  SAFETY GATE │                  │
│        └─────────│  Thompson    │  │  Warden +    │                  │
│  Platform API    │  Sampling    │  │  Hard Floors │                  │
│  Follower Diff   │  Bandit      │  └──────────────┘                  │
│                  └──────────────┘                                    │
└──────────────────────────────────────────────────────────────────────┘

```

### Multi-Platform Broadcasting

The `OmniPlatform` adapter delegates all read operations to Bluesky (the primary signal source) and fans out write operations to every initialized platform simultaneously. Platform errors on secondary platforms fail gracefully as no-ops, ensuring the primary Bluesky path is never blocked.

### Persistence Model

All mutable state is JSON, written atomically via a temporary file replacement pattern. This guarantees no partial writes are visible to readers, process restarts recover without data corruption, and state survives dyno rotation on cloud hosts.

---

## Directory Structure

```text
/
├── run.py                       # Entry point: wires config, boots engine, runs main loop
├── soul.yaml                    # Identity, persona, hooks, sectors, niche keywords
├── requirements.txt             # Python dependencies
├── kiloforge.env.example        # Environment variable template
├── render.yaml                  # Deployment configuration
├── runtime.txt                  # Python version pin
├── src/
│   ├── core/                    # Engine loop, state storage, governance, soul parsing
│   ├── intelligence/            # Bandit analyzer, LLM prompts, semantic memory
│   ├── platforms/               # API adapters (Bluesky, Threads, Omni)
│   ├── clients/                 # Unified LLM wrapper, web search integration
│   ├── utils/                   # Warden safety gate, utilities, circuit breakers
│   └── daemons/                 # Real-time AT Protocol firehose consumption
├── data/                        # Persistent JSON state and ChromaDB vector store
├── tests/                       # Test suite for legacy implementation
└── newagent/                    # Parallel modernized implementation (SQLite-backed)

```

---

## Core Modules

### FollowerEngine (The OS Kernel)

Located in `src/core/engine.py`. The central orchestrator that manages the PRA loop, the intent queue, and token budgets. It delegates strategic reasoning entirely to the Bandit and Analyzer.

### Soul and Identity

Located in `src/core/soul.py`. Parses `soul.yaml` into a typed dataclass. Validates all required fields at load time and compiles relevance signals into pre-built regex for fast content filtering.

### Persistent State Store

Located in `src/core/store.py`. Manages all durable engine state using atomic JSON writes. Key states include the bandit posteriors, action ledger, follower snapshots, deduplication sets, and the active tick counter.

### Governance and Safety

* **Rate Budgets:** Limits per-action-type throughput using independent token buckets.
* **Circuit Breaker:** Halts execution for a cooldown period after consecutive network failures.
* **Warden:** A strict two-phase safety gate. Enforces hard-floor blocking (rejecting em dashes, URLs, and hardcoded sensitive phrases) followed by LLM-driven safeguard moderation.

### Intelligence

* **Niche Analyzer:** Samples high-engagement posts, classifies archetypes, and applies exploration nudges to bandit alpha values.
* **Semantic Memory:** Uses ChromaDB for episodic and knowledge-base memory, preventing repetitive interactions and maintaining narrative continuity.

---

## Data Flow and State Management

### Content Generation Lifecycle

1. **Sense:** Read follower counts and poll sector keywords.
2. **Learn:** Mature actions from the ledger and update Beta-Binomial posteriors based on follower delta.
3. **Decide:** Sample sectors and hooks using Thompson sampling.
4. **Act:** Consume token budgets, generate content variants, pass through Warden safety checks, and execute network writes.
5. **Persist:** Atomically save engine state.

### Crash-Safe Write Protocol

All network writes use a pending-intent queue to prevent double-posting during unpredictable failures.

```text
1. Generate content.
2. Atomic write to pending_writes.json.
3. Attempt network platform write.
4. On restart: Scan pending_writes.json. If content hash exists on timeline, drop intent. If missing, re-execute.
5. Atomic remove from pending_writes.json upon success.

```

---

## External Dependencies

| Package / Service | Role | Credentials Required |
| --- | --- | --- |
| **AT Protocol (Bluesky)** | Primary platform for reads, writes, and firehose telemetry. | `BLUESKY_HANDLE`, `BLUESKY_PASSWORD` |
| **Groq** | Fast LLM inference for content generation and safeguard moderation. | `GROQ_API_KEY` |
| **Google Gemini** | Versatile fallback LLM for complex reasoning tasks. | `GEMINI_API_KEY` |
| **Meta Graph API** | Secondary broadcast platform for Threads. | `THREADS_USER_ID`, `THREADS_ACCESS_TOKEN` |
| **ChromaDB** | Local persistent vector store for semantic memory. | None |

---

## Setup and Deployment

### Local Installation

```bash
git clone <repo-url>
cd kiloforge
python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
pip install websockets

```

### Environment Configuration

Copy `kiloforge.env.example` to `.env` and populate the required keys.

* `BLUESKY_HANDLE`: Handle without the `@` symbol.
* `BLUESKY_PASSWORD`: App Password, not the account login password.
* `KF_STATE_DIR`: Directory for state files (defaults to `./data`).

### Execution Commands

```bash
# Dry run: Generates content variants without network writes
python run.py --dry-run

# Live run: Full autonomous execution
python run.py --live

```

### Kill Switch

To safely pause the engine without terminating the process, write a halt state to the configuration directory:

```bash
echo "HALTED" > data/engine_status.txt

```
