# Deploying Kiloforge to Render

This document covers running the agent as a Render **background worker**
backed by a **persistent disk**. The systemd path in `DEPLOY.md` remains
the bare-metal fallback if you ever want to host yourself.

The single fact this document exists to drill in: **all engine state lives
on the disk, not in the repo dir.** Render's worker filesystem is wiped on
every deploy. Without the disk, the bandit, ledger, snapshots, seen-set,
pending intents, and breaker state all reset on push.

## 1. One-time setup

1. Push this repo to GitHub (or GitLab). Render needs a repo it can connect to.
2. In Render, click **New + > Blueprint** and point it at the repo.
3. Render reads `render.yaml` and proposes a worker named `kiloforge` with
   a 1 GB disk mounted at `/var/lib/kiloforge` and `KF_STATE_DIR` pointing
   at the same path. Accept.
4. After the first deploy, open the worker's **Environment** tab and fill
   in the three secrets:
   - `BLUESKY_HANDLE` (no leading `@`, no quotes)
   - `BLUESKY_PASSWORD` (a Bluesky **app password**, not your main password;
     create at https://bsky.app/settings/app-passwords)
   - `GROQ_API_KEY` (create at https://console.groq.com/keys)
5. Click **Manual Deploy > Deploy latest commit** so the worker restarts
   with the secrets present.

`runtime.txt` pins Python to 3.11.9. Bump deliberately, not opportunistically.

## 2. Daily operations

| What                  | Where                                                                |
|-----------------------|----------------------------------------------------------------------|
| Live logs             | Worker page > **Logs** (live tail in the dashboard, searchable)      |
| Heartbeat snapshot    | Worker page > **Shell**, then `cat /var/lib/kiloforge/status.json`   |
| Restart               | Worker page > **Manual Deploy > Clear build cache & deploy**         |
| Stop                  | Worker page > **Suspend Service**                                    |
| Resume                | Worker page > **Resume Service**                                     |

The structured logger writes to stderr with ISO timestamps and level tags;
Render captures stderr into the log stream automatically. No extra config
needed for logs to show up.

## 3. The HALTED kill switch on Render

To pause the agent without redeploying or suspending the service (the loop
keeps ticking, but performs no network calls):

1. Worker page > **Shell**.
2. `echo HALTED > /var/lib/kiloforge/engine_status.txt`

To resume:

```bash
rm /var/lib/kiloforge/engine_status.txt
```

Realistic alternatives if Shell is not available on your Render plan:
- Suspend Service from the dashboard. Loses the in-memory rate budgets;
  the persisted state still loads cleanly on resume.
- Deploy a tiny commit that flips `KILOFORGE_LOG_LEVEL` to something
  unmistakable, observe the worker's behavior, then revert.

`HALTED` is the lighter option when available; suspension is the
heavyweight one.

## 4. The one gotcha: the disk is the agent's memory

- **Redeploys preserve the disk.** Push code, the agent picks up where it
  left off (same tick counter, same bandit posteriors, same seen set).
- **Destroying the disk wipes the agent.** Render's "Delete Service" or
  detaching the disk both irreversibly reset all state. The bandit
  posteriors and the seen-set are the expensive ones to lose; if you need
  to migrate, copy the contents of `/var/lib/kiloforge` off the box first.
- **Local dev keeps state in the repo dir.** If `KF_STATE_DIR` is unset
  (default locally), state files appear alongside `run.py` exactly as
  before. Do not commit them; `.gitignore` already excludes them.

## 5. Confirming the routing on a fresh deploy

After the first successful tick, open the Render Shell and check that the
state files are landing on the disk:

```bash
ls -la /var/lib/kiloforge
# Expect: bandit_state.json, action_ledger.json, account_snapshots.json,
# seen_targets.json, engine_state.json, circuit_breaker.json,
# pending_writes.json, status.json
```

If anything from that list appears under `/opt/render/project/src` or
similar instead, a state path is bypassing `KF_STATE_DIR`. File a bug,
do not work around it.
