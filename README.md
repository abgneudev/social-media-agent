# Kiloforge

Autonomous Bluesky follower-growth agent. Runs as a long-lived daemon that
ticks every 2.5 minutes, learns which content angles and which sectors of
its niche actually convert to engagement, and adjusts what it posts next.

**North star:** real followers, measured directly from the platform, not a
proxy metric. The bandit only learns from outcomes attributable to actions
the agent itself took.

**Niche (current soul):** UX design, frontend engineering, design systems,
explained in plain language. Niche, voice, and persona are externalized to
[`soul.yaml`](soul.yaml); the rest of this README applies to any soul.

## Repo map

```
run.py              Entry point. Wires config + logging, builds the engine,
                    loops forever. Stall counter and heartbeat run from
                    here so they fire even when a tick raises.
config.py           All constants, file paths, the logger, the soul loader,
                    KF_STATE_DIR routing, and the safety floor lists.
soul.yaml           Persona, niche keywords, hooks. Swap to retarget voice
                    and domain. Cannot weaken the code-defined safety lists.
store.py            atomic_write_json + load_json + the Store class.
                    Store holds bandit, ledger, snapshots, seen-set, engine
                    scratch state, and pending writes.
governance.py       RateBudget (token bucket per action) and CircuitBreaker
                    (persisted, auto-cools, trippable from stall detector).
adapter.py          BlueskyAdapter. One concrete adapter, no protocol-layer.
                    Wraps the atproto client; provides find_post() for the
                    reconcile-before-retry mechanism.
engine.py           FollowerEngine (orchestration), write_status (heartbeat),
                    wilson_lower_bound, hook_strength.

tests/              29 tests, all passing.
  test_reconcile.py   idempotent publish, find_post reconciliation
  test_stall.py       stall detector trips the breaker after N empty ticks
  test_status.py      status.json heartbeat shape and atomicity
  test_soul.py        soul loader fails closed; safety floor cannot be weakened
  test_state_dir.py   KF_STATE_DIR routes every state path

requirements.txt    atproto, groq, PyYAML.
runtime.txt         python-3.11.9 (for Render).
render.yaml         Render Blueprint: worker + persistent disk + env vars.
kiloforge.service   systemd unit for bare-metal hosts.
kiloforge.env.example   Env template for systemd path.

DEPLOY.md           Bare-metal/systemd deploy guide.
DEPLOY_RENDER.md    Render deploy guide. Read this first if deploying.

.gitignore          Excludes secrets, state files, newagent/, .claude/.
.renderignore       Same idea for Render's build context.

newagent/           Unrelated experimental project. Gitignored, render-ignored,
                    not part of the Kiloforge deploy.
```

## How a tick works

Each tick (default every 150 s):

1. **Reconcile pending writes.** Any publish from a previous tick that
   started but did not finalize (process crashed between the network
   write and the ledger entry) is matched against the author feed by
   content hash. If found, the ledger entry is recorded and the intent
   cleared. Prevents double-posting.

2. **Sense.** Fetch the current follower count. Every other tick, scan
   each sector's keywords against the Bluesky search to build candidate
   pools and an activity heatmap. Every 5th tick, ask the LLM to pull
   three trending sub-keywords from the hottest sector for future targeting.

3. **Learn.** Score matured actions. Posts/replies/quotes mature after
   9 minutes (engagement window); follows mature after 25 minutes (long
   enough for a human to see the notification). Beta-distribution
   alpha/beta counters update per sector and per hook.

4. **Decide.** Thompson-sample sector, post-hook, and reply-hook from
   their respective Beta posteriors. Dead sectors (zero activity this
   tick) are excluded from the sector pool unless all are dead.

5. **Act.** Phase-weighted action plan: cold accounts lean on follows,
   later phases lean on posting. Each action passes through a per-kind
   token bucket. The publishing paths (post, reply, quote) go through
   `_publish_with_reconcile`, which persists the intent before the
   network call so a crash mid-publish recovers cleanly.

6. **Update stall counter + write heartbeat.** If the tick produced zero
   successful network actions but was genuinely active (not halted, not
   breaker-open), bump `consecutive_empty_ticks`. After 8 in a row, force
   the breaker open. Then atomically write `status.json`.

The runtime loop in `run.py` catches `Exception` (with a full traceback
logged) but re-raises `KeyboardInterrupt`/`SystemExit` so operator-driven
stops are clean. The stall counter and heartbeat run in their own
try/except after the tick, so a tick that raises still gets accounted for
and the heartbeat still updates.

## State

All persistent state lives under `STATE_DIR`:

- `bandit_state.json`: Beta(alpha, beta) per sector / post_hook / reply_hook.
- `action_ledger.json`: every action taken, with attribution timestamp.
- `account_snapshots.json`: per-tick follower count history.
- `seen_targets.json`: dedup set (avoid liking, following, replying twice).
- `engine_state.json`: tick counter, phase, anchor posts, trends, stall counter.
- `circuit_breaker.json`: breaker state across restarts.
- `pending_writes.json`: unfinalized publish intents (drives reconcile).
- `status.json`: heartbeat snapshot.
- `engine_status.txt`: `HALTED` kill switch (operator-written).

`STATE_DIR` is set by the `KF_STATE_DIR` env var. On Render it points at
the mounted persistent disk. Locally (unset), it falls back to the repo
directory; existing dev state remains findable.

`soul.yaml` is read-only config that ships with the deploy; it lives in
the code dir even when state lives on a mounted disk.

## Safety model

`config.py` defines floor lists (`SENSITIVE_PHRASES_FLOOR`,
`SENSITIVE_WORDS_FLOOR`, `SPAM_PHRASES_FLOOR`) that the content gate
checks. The soul file MAY add to these via `extra_sensitive_*` fields,
which are merged by union. The soul CANNOT remove or weaken a floor
entry: there is no API for it. The soul loader fails closed if the file
is missing, malformed, or incomplete; it does not degrade to defaults.

Follow-scoring thresholds in `FollowerEngine._score_follow_target` also
stay code-enforced. Both are marked as future externalization candidates.

## Robustness pieces (items 1 to 4)

- **Idempotent publish** (`_publish_with_reconcile` + `BlueskyAdapter.find_post`):
  intent persisted before write; on a raised write, scan the author feed
  by content hash; treat a found post as success. No double-posts on
  network drops between commit and response.
- **Stall detector** (`update_stall_counter`): after `STALL_THRESHOLD` (8)
  active-but-empty ticks, force the breaker open. The daemon stops
  chewing cycles silently when something upstream is wrong.
- **Heartbeat** (`write_status`): atomic `status.json` written every loop
  iteration. Operator's first-line healthcheck.
- **Structured logging**: ISO timestamp + level on every line, to stderr,
  so journald and Render dashboards capture it cleanly.

## Running it

### Local

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt
export BLUESKY_HANDLE=yourhandle.bsky.social
export BLUESKY_PASSWORD=xxxx-xxxx-xxxx-xxxx   # app password
export GROQ_API_KEY=gsk_...
python run.py
```

### Render (recommended)

See [`DEPLOY_RENDER.md`](DEPLOY_RENDER.md). One blueprint
(`render.yaml`) creates the worker, attaches the persistent disk, and
routes state through `KF_STATE_DIR`. Set the three secrets in the
dashboard.

### Systemd (bare metal)

See [`DEPLOY.md`](DEPLOY.md). Drop the unit at
`/etc/systemd/system/kiloforge.service`, put secrets in
`/etc/kiloforge/kiloforge.env` (mode 600), `systemctl enable --now`.

## Tests

```bash
venv/Scripts/python -m unittest discover -s tests
```

29 tests, organized by item:

| File              | What it asserts                                                    |
|-------------------|--------------------------------------------------------------------|
| `test_reconcile`  | Successful publish + crash before recording does not double-post.  |
| `test_stall`      | N empty active ticks trip the breaker; inactive ticks do not.      |
| `test_status`     | `status.json` shape, atomicity, last_action surface.               |
| `test_soul`       | Loader fails closed; safety floor cannot be weakened from soul.    |
| `test_state_dir`  | `KF_STATE_DIR` routes every state path; `soul.yaml` stays with code.|

Tests inject a `MockAdapter` instead of touching the live network.

## Swapping the soul

Edit `soul.yaml`: change `name`, `bio`, `persona`, `post_hooks` and their
guidance, `reply_hooks` and their guidance, `keyword_map` (sectors come
from its keys), and `relevance_signals`. Restart. The agent retargets to
the new domain.

What you cannot change from the soul: the safety floor (you can extend
it but not narrow it), the follow-scoring thresholds, the bandit and
governance logic. Those are code-enforced and a new domain has not been
validated end-to-end. Treat the first run in a new niche as a manual
review pass.

## Code conventions

- No em dashes anywhere in code, comments, docs, or generated content.
  The content gate refuses to publish text containing an em dash.
- Print is forbidden in production code; everything goes through the
  module logger so it lands in journald / Render logs with level and
  timestamp.
- Atomic writes only for state. Never write JSON state in place.
- One concrete adapter. If a second platform ever lands, add a sibling
  adapter; do not extract a protocol class until there is real demand.
