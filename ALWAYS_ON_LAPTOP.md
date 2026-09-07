# Always-On Laptop Setup

**Read this file top to bottom and do everything in it.** This is the one
doc for turning a spare Windows laptop into Lumina's 24/7 companion machine
— WhatsApp bridge, knowledge sync, DB backups, health alerts, daily
digests, roll-call/standup nudges, and self-updates. After first setup you
never need to touch this laptop again for code changes — it pulls its own
updates from `main` every ~10 minutes (see "Self-update" below).

Everything here is scripts-only. The actual Lumina backend runs on
Railway, not this laptop — this machine is a companion, not a server.

## 0. What this laptop will run

One process, `scripts/laptop_agent.py`, supervises everything:
- WhatsApp bridge (`whatsapp-bridge/index.js`, Baileys) — replies to
  DMs/groups via `whatsapp_agent.py` on the backend
- Knowledge folder sync → Lumina's KB
- Optional research-URL snapshots → same KB
- Daily DB backup pulled off Railway (+ offsite copy)
- Health watchdog + daily brief + roll-call/standup/lunch/EOD/weekly-wrap
  nudges, all over WhatsApp (needs the bridge + a team group id)
- Its own self-update (git pull, ff-only, restarts only what changed)

Full job list + every flag: the docstring at the top of
`scripts/laptop_agent.py` — read that once, it's kept accurate.

## 1. Prerequisites

- **Git**, already configured to clone this repo
- **Python 3.11+** on PATH
- **Node.js ≥ 20** on PATH (bridge needs ≥20 even though its own
  `package.json` says `>=18` — the optional voice-call dependency needs 20)
- **ffmpeg** on PATH — only needed if you set up voice calls (§6); skip
  otherwise

```powershell
git --version
python --version
node --version
```

## 2. Clone and install

```powershell
cd C:\
git clone <this repo's URL> lumina-companion
cd lumina-companion

pip install -r scripts\requirements.txt

cd whatsapp-bridge
npm install
cd ..
```

If `npm install` fails on `baileys-caller` (the optional voice-calling
dependency), that's fine — it's `optionalDependencies`, the bridge runs
without it. Voice calls just won't work until it's sorted (§6).

## 3. Environment variables

Copy the template and fill it in:

```powershell
copy scripts\.env.example scripts\.env
notepad scripts\.env
```

Then actually **set these as real Windows environment variables** (User
scope, not just in the `.env` file — Task Scheduler in §5 inherits User-
scope vars, not a `.env` file the script doesn't auto-load unless you
add your own loader):

| Variable | Required for | Where to get it |
|---|---|---|
| `LUMINA_URL` | everything | `https://lumina.mmga.agency` |
| `STORAGE_SYNC_TOKEN` | KB sync, digest, sheets watchdog, roll-call | value you set for `STORAGE_SYNC_TOKEN` on Railway |
| `FLASK_SECRET_KEY` | DB backup | Railway's real `FLASK_SECRET_KEY` |
| `WHATSAPP_BRIDGE_TOKEN` | bridge auth, outbox delivery | pick a random string, set it here **and** on Railway |
| `ROLLCALL_GROUP_ID` | roll-call/lunch/EOD/weekly-wrap posts | the team WhatsApp group's id (bridge logs it when it sees the group) |
| `ALERT_TARGETS` | health/bridge-down alerts | comma-separated apprise URLs, e.g. `tgram://<bot-token>/<chat-id>` |
| `DIGEST_TARGETS` | daily brief delivery | apprise URL(s); falls back to `ALERT_TARGETS` |
| `BACKUP_RCLONE_REMOTE` | offsite DB backup (optional) | an `rclone` remote name, if you use rclone |

To set a User env var permanently:
```powershell
[Environment]::SetEnvironmentVariable("STORAGE_SYNC_TOKEN", "the-real-value", "User")
```
Do this for each row above that applies, then **close and reopen** any
terminal before testing (env vars only apply to new processes).

## 4. First run (foreground, watch it work)

```powershell
python scripts\laptop_agent.py --dir "C:\lumina-companion\knowledge" --rollcall-group %ROLLCALL_GROUP_ID%
```

Watch the console. You should see the banner listing which jobs are
active/skipped based on what env vars are set. First bridge start prints
a **QR code** — scan it with the dedicated WhatsApp number's phone (never
a personal/main number — see CLAUDE.md gotcha #100 for why). If the
number is brand new, WhatsApp may refuse pairing for a few hours
("try again later") — that's normal anti-abuse behavior, not a bug; use
the number normally (set a photo, send a couple messages) and retry.

Confirm it's actually working:
- Message the bridge number from your own phone → should get a reply
- `curl http://localhost:8787/health` → `{"connected":true,...}`

Ctrl+C to stop once confirmed, then move to autostart.

## 5. Autostart (Task Scheduler)

This needs to survive reboots and keep running with nobody logged in
watching it.

```powershell
$action = New-ScheduledTaskAction -Execute "pythonw.exe" `
  -Argument '"C:\lumina-companion\scripts\laptop_agent.py" --dir "C:\lumina-companion\knowledge"'
$trigger1 = New-ScheduledTaskTrigger -AtLogOn
$trigger2 = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBattery -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Lumina companion" -Action $action `
  -Trigger $trigger1,$trigger2 -Settings $settings -RunLevel Highest
```

`pythonw.exe` (not `python.exe`) so no console window sits open. Logs go
to `<knowledge-dir>\..\lumina-logs\agent.log` on its own — no
`-RedirectStandardOutput` needed.

Start it now instead of waiting for next login/reboot:
```powershell
Start-ScheduledTask -TaskName "Lumina companion"
```

Manage it later:
```powershell
Get-ScheduledTask -TaskName "Lumina companion"      # status
Restart-ScheduledTask -TaskName "Lumina companion"   # after a manual env-var change
```

## 6. Optional: voice calls

Only if you want the WhatsApp announcement-call feature (gotcha #113 in
CLAUDE.md) — skip this section otherwise, everything else works fine
without it.

1. Confirm Node ≥20 (`node --version`) and ffmpeg on PATH
2. Confirm `whatsapp-bridge/node_modules/baileys-caller` exists (from the
   `npm install` in §2 — if it didn't install, `cd whatsapp-bridge && npm
   install baileys-caller` again and check for errors)
3. Install **Piper** (local TTS) — see
   https://github.com/rhasspy/piper for the Windows binary, download one
   voice `.onnx` model
4. Set `PIPER_BIN` and `PIPER_VOICE_MODEL` env vars (same way as §3)
5. Restart the scheduled task
6. Trigger a real call once (ask the WhatsApp bot to place one) — the
   **first** call needs its own separate QR pairing scan, independent of
   the text bridge's pairing in §4

## 7. Optional: meeting notes → tasks

Also skip-able. If you want meeting transcripts turned into real Lumina
tasks (gotcha #115):

1. Install **meeting-scribe** or **TalkTrack** on whichever laptop
   attends meetings (not necessarily this one) — both are free, local,
   listen to system audio, export a markdown summary
2. Point that tool's export folder at, or match it to, a folder this
   laptop can reach
3. Run manually after a meeting (or leave it running):
   ```powershell
   python scripts\meeting_sync.py --dir "C:\path\to\exports" --user-id emp003 --watch
   ```
   `meeting_sync.py` is **not** one of `laptop_agent.py`'s auto-managed
   jobs — run it yourself, or wire your own Task Scheduler entry for it
   if you want it always-on too.

## 8. Self-update — how future changes reach this laptop

Once running, `laptop_agent.py` polls `origin/main` every ~10 minutes
(`git fetch` + `git merge --ff-only`) on its own checkout. If
`scripts/**.py` changed, it restarts itself; if `whatsapp-bridge/**`
changed, it restarts just the bridge child (your WhatsApp session stays
logged in across a scripts-only update — only a real bridge-code change
needs a fresh bridge start). Backend (`backend/`) changes deploy via
Railway automatically and need nothing from this laptop.

**This means: all editing happens on the dev machine, push to `main`,
this laptop picks it up on its own within ~10 minutes.** Don't hand-edit
files directly on this laptop — a local edit here will conflict with the
next `git pull` (fast-forward only; a diverged local checkout just stops
updating and alerts loudly instead of overwriting your change).

If it ever stops updating: check `lumina-logs\agent.log` for a
self-update failure, and check `git status` on this checkout for local
changes blocking the fast-forward.

## 9. If something looks dead

- **No WhatsApp replies at all** → `curl http://localhost:8787/health`;
  if `connected:false` or unreachable, check `Desktop\LUMINA-BRIDGE-
  ALERT.txt` (written automatically on a logout/crash-loop) and
  `lumina-logs\` for the reason. Usually means a re-scan of the QR code
  is needed (session logged out on WhatsApp's side).
- **Nothing running at all** → `Get-ScheduledTask -TaskName "Lumina
  companion"` for its state, then `Start-ScheduledTask` to kick it.
- **Full job list, every env var, every flag** → the docstring at the
  top of `scripts/laptop_agent.py` is the single source of truth, kept
  in sync with the code by convention. This file is the onboarding
  path; that docstring is the reference.
- **Anything deeper** → CLAUDE.md gotcha #100 (WhatsApp/companion
  build history), #113 (voice calls), #115 (meeting-to-tasks) have the
  full design rationale and known limitations for each piece.
