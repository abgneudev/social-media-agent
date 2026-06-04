# Deploying Kiloforge

This document covers running `run.py` as a long-lived systemd service on a
small Linux box (Debian/Ubuntu/Fedora; any distro with systemd).

The agent is a small set of Python modules (`run.py` is the entry; the
modules it imports are `config`, `store`, `governance`, `adapter`, `engine`)
plus a `soul.yaml` that holds the persona, niche keywords, and hook guidance.
The state files live next to the code. It is designed to crash, be restarted by systemd, and resume from
disk cleanly. Pending writes are reconciled against Bluesky on every tick, so
a crash mid-publish does not double-post.

## 1. One-time install

Run as root on the host:

```bash
# Dedicated unprivileged user. No login, no home shell.
sudo useradd --system --create-home --home-dir /opt/kiloforge --shell /usr/sbin/nologin kiloforge

# Pull the code in. Replace with your real deployment method (git clone,
# rsync, scp, etc.). The example assumes the repo is unpacked at /opt/kiloforge.
sudo mkdir -p /opt/kiloforge
sudo chown kiloforge:kiloforge /opt/kiloforge
# ... copy the repo (run.py, config.py, store.py, governance.py, adapter.py,
# engine.py, soul.yaml, kiloforge.service, requirements.txt) into
# /opt/kiloforge ...

# Virtualenv + deps as the service user.
sudo -u kiloforge python3 -m venv /opt/kiloforge/venv
sudo -u kiloforge /opt/kiloforge/venv/bin/pip install --upgrade pip
sudo -u kiloforge /opt/kiloforge/venv/bin/pip install -r /opt/kiloforge/requirements.txt
```

## 2. Secrets

The service reads `BLUESKY_HANDLE`, `BLUESKY_PASSWORD`, and `GROQ_API_KEY`
from an `EnvironmentFile` at `/etc/kiloforge/kiloforge.env`. Never put these
on the systemd `ExecStart` line (they would leak into `ps` and the journal).

```bash
sudo mkdir -p /etc/kiloforge
sudo cp /opt/kiloforge/kiloforge.env.example /etc/kiloforge/kiloforge.env
sudo chown root:root /etc/kiloforge/kiloforge.env
sudo chmod 600 /etc/kiloforge/kiloforge.env
sudo "$EDITOR" /etc/kiloforge/kiloforge.env   # fill in real values
```

Use a Bluesky **app password** (https://bsky.app/settings/app-passwords), not
the account password. You can revoke it from that page without affecting
other logins.

## 3. Install and enable the unit

```bash
sudo cp /opt/kiloforge/kiloforge.service /etc/systemd/system/kiloforge.service
sudo systemctl daemon-reload
sudo systemctl enable --now kiloforge.service
```

`enable --now` enables on boot AND starts immediately.

## 4. Daily operations

| What                          | Command                                                            |
|-------------------------------|--------------------------------------------------------------------|
| Status                        | `systemctl status kiloforge`                                       |
| Live logs                     | `journalctl -u kiloforge -f`                                       |
| Last hour of logs             | `journalctl -u kiloforge --since '1 hour ago'`                     |
| Heartbeat snapshot            | `cat /opt/kiloforge/status.json`                                   |
| Restart                       | `sudo systemctl restart kiloforge`                                 |
| Stop                          | `sudo systemctl stop kiloforge`                                    |
| Disable on boot               | `sudo systemctl disable kiloforge`                                 |

`status.json` is rewritten atomically at the end of every tick. It is the
fastest way to confirm the daemon is alive without scrolling logs:

```json
{
  "ts_iso": "2026-06-03T20:45:56",
  "tick": 42,
  "phase": "explore",
  "followers": 13,
  "last_action": { "kind": "follow", "target_handle": "alice.bsky.social" },
  "breaker_state": "CLOSED",
  "consecutive_empty_ticks": 0,
  "pending_writes": 0
}
```

If `breaker_state` is `OPEN` or `consecutive_empty_ticks` is climbing, check
the journal: something is wrong and the agent has paused itself.

## 5. The HALTED kill switch

To pause the agent WITHOUT stopping the systemd unit (the loop keeps ticking
but performs no network calls):

```bash
echo HALTED | sudo -u kiloforge tee /opt/kiloforge/engine_status.txt
```

To resume:

```bash
sudo -u kiloforge rm /opt/kiloforge/engine_status.txt
```

Use this when you want to investigate state without the daemon racing you,
or when you need to take Bluesky offline (rate limit incident, account
review) without losing the process and its in-memory rate budgets.

For a hard stop, use `sudo systemctl stop kiloforge` instead.

## 6. Crash recovery, by design

The unit sets `Restart=always` with `RestartSec=10s` and a burst limit of 5
restarts per 5 minutes. If the agent crashes:

1. systemd restarts it after 10 seconds.
2. On start, `_reconcile_pending` scans `pending_writes.json` and reconciles
   any unfinalized publish against the Bluesky author feed; posts that
   landed before the crash are recorded in the ledger instead of being
   retried.
3. The bandit, ledger, snapshots, seen set, and engine state all reload
   from disk; the agent resumes the same tick counter, the same phase, and
   the same stall counter it had before.

If it crashes more than 5 times in 5 minutes, systemd stops trying and the
unit goes into `failed` state. That is a signal to investigate, not to
auto-retry harder.

## 7. Upgrading

```bash
sudo systemctl stop kiloforge
# ... pull or copy in the new files ...
sudo -u kiloforge /opt/kiloforge/venv/bin/pip install -r /opt/kiloforge/requirements.txt
sudo systemctl daemon-reload     # only if the unit file changed
sudo systemctl start kiloforge
```

State files are upward-compatible: new fields default in, removed fields are
ignored on load.

## 8. Swapping the soul

`soul.yaml` controls voice and niche: display name, bio, persona prompt,
post and reply hooks with their guidance, niche search keywords, and
relevance signals. Edit it and restart to retarget.

What the soul CANNOT change: the safety gate floors (sensitive phrases,
sensitive words, spam phrases) and the follow-scoring thresholds. Those
stay code-enforced. You can ADD safety entries from the soul via
`extra_sensitive_phrases`, `extra_sensitive_words`, `extra_spam_phrases`,
but you cannot remove or weaken the floor. A new domain has not been
validated end-to-end; treat the first run in a new niche as a manual
review pass.

If `soul.yaml` is missing or malformed at startup, the process refuses to
start (fail-closed). Check the log.

