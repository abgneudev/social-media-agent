# Kiloforge

Autonomous multi-platform social media agent (Bluesky & Threads). Runs as a long-lived daemon featuring an OS-style Kernel, a Strategist brain, and a Resource-Constrained Priority Scheduling (RCPSP) loop. The agent learns which content angles and which sectors of its niche actually convert to engagement, and adjusts what it posts next.

**North star:** real followers, measured directly from the platform. The bandit only learns from outcomes attributable to actions the agent itself took.

**Niche (current soul):** UX design, frontend engineering, design systems, explained in plain language. Niche, voice, and persona are externalized to [`soul.yaml`](soul.yaml); the rest of this README applies to any soul.

## Repo Map

```
run.py              Entry point. Wires config + logging, loads Soul, detects
                    environment variables to build OmniPlatform, and boots
                    the FollowerEngine.
soul.yaml           Persona, niche keywords, hooks, creative mediums. Swap
                    to retarget voice and domain.

src/core/
  engine.py         FollowerEngine (the OS Kernel). Manages the main loop,
                    Token Buckets, the execution queue, and calls the Brain.
  config.py         Constants, logger setup, rate limit definitions, and 
                    safety floor definitions.
  soul.py           Parses soul.yaml, generates regex signals, dynamic getters.
  store.py          atomic_write_json + the Store class. Holds the bandit,
                    ledger, seen-sets, and engine scratch state.

src/intelligence/
  strategist.py     The Brain wrapper.
  prompts.py        LLM Prompt builders for the Strategist, Warden, and hooks.
  analyzer.py       Calculates Thompson Sampling posteriors for the bandit.
  web_research.py   Serper integration for pulling live industry trends.
  memory.py         Long-term vector-like semantic memory.

src/platforms/
  platform.py       Abstract base class defining the 23-method network interface.
  bluesky.py        Concrete adapter for ATProto (Bluesky).
  threads.py        Concrete adapter for Meta Graph API (Threads).
  omni.py           Broadcaster wrapper. Defers sensing to Bluesky, broadcasts
                    writes to all initialized platforms.

src/utils/
  warden.py         Content safety and verification.
  breaker.py        CircuitBreaker logic to halt execution on rapid failure.
```

## How the Architecture Works

Kiloforge runs on a completely decoupled architecture, separating **Thinking (Strategist)** from **Doing (Kernel)**.

### 1. The OS Kernel
The main execution loop in `engine.py` functions like a CPU scheduler. It does *not* make strategic decisions. It holds **Token Buckets** representing its budget for actions (e.g., 5 follows, 2 posts, 8 likes, 1 strategist call). Every tick, the Kernel pops the highest priority `Intent` from its queue and executes it, provided the token budget allows it.

### 2. The Strategist (Brain)
When the Kernel's execution queue is empty, and the `strategy_plan` cooldown is met, the Kernel wakes up the Strategist. 
The Strategist is fed raw telemetry: follower ratios, current rate limits, hot trending sectors, and recent memory. The Strategist outputs an `active_plan` (long-term strategy) and schedules a batch of `intents` (up to 15 actions like `follow`, `like`, `curate`, `post`, `quote`) graded by Priority (1-10) to drain its available budget.

### 3. The OmniPlatform (Multi-Network Sync)
Kiloforge is network-agnostic. The Kernel communicates with the `Platform` interface. If only Bluesky credentials are provided, it uses `BlueskyPlatform`. If `THREADS_USER_ID` is present, it wraps them both in an `OmniPlatform`. 
- **Read/Sense:** The `OmniPlatform` defers reading timelines and hot topics to the primary network (Bluesky) to keep its internal algorithms and feedback loops stable.
- **Write/Act:** The `OmniPlatform` broadcasts posts, replies, and quotes identically to all attached networks simultaneously.

## The Cognitive Cycle

Kiloforge operates as a fully autonomous agent, driven by a continuous cognitive cycle of Sensing, Planning, Acting, and Learning. Rather than running hardcoded scripts, the agent uses its "heartbeat" (defaulting to 150 seconds) to evaluate its environment and make dynamic, mathematically-backed decisions.

1. **Firehose & Sensing (The Eyes):** 
   - The agent connects to the platform's live data streams. It calculates its real follower count.
   - It semantically scans the network using the keywords defined in `soul.yaml` to identify trending sectors (e.g., is "frontend engineering" hot right now, while "design systems" is quiet?).
   - It scores potential targets for engagement, curating candidates based on reciprocity, bio quality, and context match.
   
2. **The Strategist's Planning (The Brain):**
   - The OS Kernel manages a queue of intents. If the queue needs replenishment, it calls upon the **Strategist**.
   - The Strategist evaluates the agent's current budgets (Token Buckets for limits like follows, likes, quotes per hour) and long-term memory.
   - It generates a sweeping `active_plan` (a master strategy) and outputs a dynamic batch of `intents` specifically selected to execute that strategy and maximize network velocity.
   
3. **Execution & Generation (The Hands):**
   - The Kernel pops the highest priority `intent` from the queue (e.g., `curate` or `quote`).
   - For content generation, the Kernel asks the LLM to draft content adhering strictly to the `soul.yaml` persona, leveraging the mathematically highest-performing "hook".
   
4. **Warden Gating (The Shield):**
   - Before any text touches the network, the **Warden** analyzes the generated content.
   - It checks against the code-enforced `SPAM_PHRASES_FLOOR` and `SENSITIVE_PHRASES_FLOOR` combined with the Soul's specific restrictions.
   - If the content violates formatting rules (like using an em dash or exceeding character limits), it is ruthlessly rejected and the Kernel skips the action.
   
5. **Omni-Broadcasting & Reconciliation:**
   - The `OmniPlatform` broadcasts the action. If both Bluesky and Threads are connected, it hits both APIs simultaneously.
   - The Kernel persists the intent's ID *before* the network call. If the script crashes mid-publish, the next Tick will scan the timeline, find the dropped intent, and gracefully recover without double-posting.

6. **Ledger & Thompson Sampling (The Evolution):**
   - The agent writes the action to its immutable `action_ledger.json`.
   - **Maturity:** Actions don't count instantly. A post needs 9 minutes to "mature", and a follow needs 25 minutes for the user to notice.
   - Once mature, the agent checks if the action resulted in a follower gain. It updates the **Thompson Sampling Bandit**, mathematically rewarding or punishing the specific Sector and Hook that was used. This ensures the agent is constantly evolving toward what actually works.

## Safety Model

`config.py` defines floor lists (`SENSITIVE_PHRASES_FLOOR`, `SPAM_PHRASES_FLOOR`). The `Soul` object extends these dynamically via `extra_sensitive_*` fields. The content gate checks all outputs. The code fails closed if safety checks fail.

## Running It

### Local

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt

# Bluesky Keys (Required)
export BLUESKY_HANDLE=yourhandle.bsky.social
export BLUESKY_PASSWORD=xxxx-xxxx-xxxx-xxxx

# Groq (Required)
export GROQ_API_KEY=gsk_...

# Threads Keys (Optional for OmniPlatform Broadcast)
export THREADS_USER_ID=123456789
export THREADS_ACCESS_TOKEN=EAAGxxxx...

python run.py --live
```

## Code Conventions

- No em dashes anywhere in code, comments, docs, or generated content. The content gate refuses to publish text containing an em dash.
- State is strictly decoupled from Globals. `Soul` must be explicitly passed down the call stack.
- Atomic writes only for state. Never write JSON state in place.
- All platforms must inherit from the `Platform` interface. Unsupported API actions (like `create_list` on Threads) must fail gracefully or act as no-ops rather than crashing the Kernel.
