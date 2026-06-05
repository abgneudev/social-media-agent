# Kiloforge: Autonomous Cognitive Agent Architecture

Kiloforge is an autonomous, multi-platform AI agent (currently supporting Bluesky and Threads). It does not operate on hardcoded scripts or simple cron jobs. Instead, it utilizes a state-of-the-art **Cognitive Architecture** driven by a continuous **Perception–Reasoning–Action (PRA) loop**. 

The agent learns autonomously using a Reinforcement Learning algorithm (Thompson Sampling) to discover which content angles and niche sectors actually convert to real engagement, dynamically adjusting its future strategies without human intervention.

**North star:** Real followers, measured directly from the platform. The AI only learns from outcomes mathematically attributable to its own actions.

**Niche (current soul):** UX design, frontend engineering, design systems, explained in plain language. Identity, voice, and persona are entirely decoupled into [`soul.yaml`](soul.yaml); the underlying cognitive engine applies to any domain.

---

## The Cognitive Architecture (The PRA Loop)

Kiloforge is built on the industry-standard design patterns for autonomous agents, strictly separating *Thinking* from *Doing*.

### 1. Perception (The Eyes)
The agent continuously ingests and interprets signals from its environment to build context:
- **Telemetry:** It polls the live network for real follower metrics and engagement stats.
- **Semantic Scanning:** It queries the platform firehose using keyword parameters defined in `soul.yaml` to build a real-time activity heatmap (e.g., determining if "frontend engineering" is currently trending).
- **Target Curation:** It identifies potential human targets, scoring them heuristically based on bio quality, reciprocity, and contextual relevance.

### 2. Reasoning Engine (The Brain)
Kiloforge utilizes a **Plan-and-Act** design pattern powered by its `Strategist` module.
- The OS Kernel wakes up the Strategist when its execution queue is depleted (subject to a cooldown).
- The Brain evaluates short-term context (Token Buckets defining current rate limits for follows/posts/likes) and retrieves semantic context from its Long-Term Memory (`memory.py`).
- It outputs an `active_plan` (a multi-step strategic narrative) and schedules an `IntentQueue` of up to 15 prioritized micro-actions (e.g., `curate`, `quote`, `like`) designed to exhaust the available budget and maximize network velocity.

### 3. Action Module (The Hands)
The **OS Kernel** functions as the execution orchestrator, completely devoid of strategic decision-making. 
- It uses a Resource-Constrained Priority Scheduling (RCPSP) algorithm to pop intents from the queue.
- If content generation is required, it calls the LLM with strict formatting rules based on the `soul.yaml` persona and the highest-performing psychological "hook."
- **Omni-Broadcasting:** The Action Module executes through the `OmniPlatform` interface, safely broadcasting payloads to all connected networks (Bluesky & Threads) simultaneously while persisting intent IDs to prevent double-posting in case of network failure.

### 4. Learning & Evolution (Thompson Sampling)
Kiloforge possesses a mathematical feedback loop:
- Actions are recorded to an immutable ledger.
- The system waits for actions to "mature" (e.g., 9 minutes for a post, 25 minutes for a follow).
- Matured actions are graded against real follower growth. The agent uses a **Thompson Sampling Bandit** to update Beta distribution posteriors for specific Topics and Hooks. The agent mathematically evolves toward what works and abandons what doesn't.

### 5. Guardrails (The Shield)
To ensure safety and brand alignment, the **Warden** module intercepts all generated content before it touches the network. It enforces hardcoded `SPAM_PHRASES_FLOOR` and `SENSITIVE_PHRASES_FLOOR` arrays, overriding the LLM and rejecting the intent if safety constraints are breached.

---

## Repo Map

```
run.py              Entry point. Wires config + logging, loads Soul, detects
                    environment variables to build OmniPlatform, and boots
                    the FollowerEngine.
soul.yaml           Persona, niche keywords, hooks, creative mediums. Swap
                    to retarget voice and domain.

src/core/
  engine.py         FollowerEngine (the OS Kernel). Manages the main PRA loop,
                    Token Buckets, the intent queue, and calls the Brain.
  config.py         Constants, logger setup, rate limit definitions, and 
                    safety floor definitions.
  soul.py           Parses soul.yaml, generates regex signals, dynamic getters.
  store.py          atomic_write_json + the Store class. Holds the bandit,
                    ledger, seen-sets, and engine scratch state.

src/intelligence/
  strategist.py     The Brain wrapper (Reasoning Engine).
  prompts.py        LLM Prompt builders for the Strategist, Warden, and hooks.
  analyzer.py       Calculates Thompson Sampling posteriors for the bandit.
  web_research.py   Serper integration for pulling live industry trends.
  memory.py         Long-term vector-like semantic memory.

src/platforms/
  platform.py       Abstract base class defining the network interface.
  bluesky.py        Concrete adapter for ATProto (Bluesky).
  threads.py        Concrete adapter for Meta Graph API (Threads).
  omni.py           Broadcaster wrapper. Defers sensing to Bluesky, broadcasts
                    writes to all initialized platforms.

src/utils/
  warden.py         Content safety and verification.
  breaker.py        CircuitBreaker logic to halt execution on rapid failure.
```

---

## Running It

### Local Environment

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt

# Bluesky Keys (Required)
export BLUESKY_HANDLE=yourhandle.bsky.social
export BLUESKY_PASSWORD=xxxx-xxxx-xxxx-xxxx

# Groq (Required for Reasoning Engine)
export GROQ_API_KEY=gsk_...

# Threads Keys (Optional for OmniPlatform Broadcast)
export THREADS_USER_ID=123456789
export THREADS_ACCESS_TOKEN=EAAGxxxx...

python run.py --live
```

## Code Conventions

- **State Decoupling:** Global variables are prohibited. The `Soul` object and State representations must be passed down the call stack to maintain modularity.
- **No Em Dashes:** Em dashes are strictly forbidden in code, comments, docs, or generated content. The Warden will refuse to publish text containing an em dash.
- **Atomic Operations:** State must only be updated via atomic writes. Never write JSON state in place.
- **Graceful Degradation:** All platform adapters inherit from the `Platform` interface. Unsupported API actions (e.g., list curation on Threads) must fail gracefully as no-ops rather than crashing the Kernel.
