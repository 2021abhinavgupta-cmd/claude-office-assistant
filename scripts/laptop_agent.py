#!/usr/bin/env python3
"""
laptop_agent.py — one always-on process that gets the most out of a spare
machine sitting next to Lumina.

Jobs it runs (each skips itself automatically if not configured):

  1. knowledge sync    — mirror your knowledge folder up to Lumina        (30s)
  2. research feed      — refresh research_sources.txt web snapshots       (1h)
  3. DB backup          — pull logs/app.db off Railway to local disk       (24h)
                          + copy it offsite (mirror dir and/or rclone)
                          + weekly: pull the uploads/ archive too
  4. health check       — ping Lumina, time it, log it; alert on down/slow (5m)
  5. WhatsApp bridge    — keep whatsapp-bridge/index.js (Baileys) alive
  6. daily brief        — 09:00 & 19:00: fetch /api/companion/digest, send it
  7. sheets watchdog    — check Google-Sheet sync health, alert; optional  (20m)
                          auto-pull with --sheets-autopull
  8. attendance roll-call — 12:00: post who hasn't logged in yet to the
                          team WhatsApp group (needs --rollcall-group + bridge)
  9. standup nudge       — 11:30: DM anyone who hasn't added a task to today's
                          standup ("reply here and I'll add it") (needs bridge)
 10. WhatsApp outbox     — deliver queued proactive DMs (task-assigned nudges,
                          "remind X to do Y") via the local bridge            (20s)
 11. lunch nudge         — 14:00: post a "go for lunch" message to the team
                          WhatsApp group (needs --rollcall-group + bridge)

Setup:
    pip install -r scripts/requirements.txt
    set LUMINA_URL=https://lumina.mmga.agency
    set STORAGE_SYNC_TOKEN=<token you set on Railway>       (jobs 1, 6, 7, 8)
    set FLASK_SECRET_KEY=<Railway FLASK_SECRET_KEY>          (job 3)
    set ALERT_TARGETS=tgram://token/chatid,mailto://...      (job 4 alerts; apprise URLs)
    set DIGEST_TARGETS=tgram://token/chatid                  (job 6; falls back to ALERT_TARGETS)
    set ALERT_WEBHOOK=<Slack/Discord webhook>                (fallback if no *_TARGETS)
    set BACKUP_RCLONE_REMOTE=myremote:lumina-backups         (job 3 offsite, optional)
    set WHATSAPP_BRIDGE_TOKEN=<token you set on Railway>      (job 5)
    set ROLLCALL_GROUP_ID=<group id>                         (job 8; or --rollcall-group)

Run:
    python scripts/laptop_agent.py --dir "C:\\Users\\me\\LuminaKnowledge"

Autostart: Task Scheduler → "At log on" →
    pythonw.exe <path>\\scripts\\laptop_agent.py --dir "C:\\Users\\me\\LuminaKnowledge"
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import shutil
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import requests
except ImportError:
    sys.exit("laptop_agent: `pip install requests` first.")

try:
    import apprise  # 100+ notification services in one lib; optional
except ImportError:
    apprise = None

import kb_sync
import research_feed

DEFAULT_URL = "https://lumina.mmga.agency"
BACKUP_KEEP = 14
UPLOADS_EVERY_DAYS = 7


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _api_token(cfg: dict) -> str:
    """Token for the /api/companion/* + /api/storage/* endpoints."""
    return cfg["storage_token"] or cfg["db_secret"]


# ── notify ─────────────────────────────────────────────────────────────────

def _send(targets: list, webhook: str, title: str, body: str) -> bool:
    if targets and apprise is not None:
        try:
            ap = apprise.Apprise()
            for t in targets:
                ap.add(t)
            ap.notify(title=title, body=body)
            return True
        except Exception as e:
            _log(f"apprise failed: {e}")
    if webhook:
        try:
            requests.post(webhook, json={"text": f"{title}\n{body}"}, timeout=15)
            return True
        except Exception as e:
            _log(f"webhook failed: {e}")
    return False


def _notify(cfg: dict, msg: str) -> None:
    """An alert (something is wrong)."""
    if not _send(cfg["alert_targets"], cfg["alert_webhook"], "Lumina watchdog", msg):
        _log("ALERT (no channel): " + msg)


def _digest_notify(cfg: dict, title: str, body: str) -> None:
    """A routine report (the daily brief). Prefers DIGEST_TARGETS."""
    targets = cfg["digest_targets"] or cfg["alert_targets"]
    if not _send(targets, cfg["alert_webhook"], title, body):
        _log(f"{title} (no channel):\n{body}")


# ── job 1: knowledge sync ──────────────────────────────────────────────────

def job_sync(cfg: dict) -> None:
    if not cfg["storage_token"]:
        return
    try:
        kb_sync.sync_once(cfg["dir"], cfg["url"], cfg["storage_token"])
    except Exception as e:
        _log(f"sync error: {e}")


# ── job 2: research feed ───────────────────────────────────────────────────

def job_research(cfg: dict) -> None:
    if not (cfg["dir"] / research_feed.SOURCES_FILE).exists():
        return
    try:
        research_feed.run_once(cfg["dir"])
    except Exception as e:
        _log(f"research error: {e}")


# ── job 3: DB backup (+ offsite + uploads) ─────────────────────────────────

def _mirror(cfg: dict, path: Path) -> None:
    """Copy one backup file offsite: to a mirror dir and/or an rclone remote."""
    mdir = cfg["backup_mirror"]
    if mdir:
        try:
            mdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, mdir / path.name)
            _log(f"  mirrored -> {mdir / path.name}")
        except Exception as e:
            _log(f"  mirror copy failed: {e}")
    remote = cfg["rclone_remote"]
    if remote and shutil.which("rclone"):
        try:
            subprocess.run(["rclone", "copy", str(path), remote],
                           timeout=600, capture_output=True)
            _log(f"  rclone -> {remote}")
        except Exception as e:
            _log(f"  rclone failed: {e}")
    elif remote:
        _log("  rclone remote set but rclone not on PATH")


def job_backup(cfg: dict) -> None:
    secret = cfg["db_secret"]
    if not secret:
        return
    bdir = cfg["backup_dir"]
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    dest = bdir / f"app-{stamp}.db"
    try:
        r = requests.get(f"{cfg['url']}/admin/download-db",
                         params={"secret": secret}, timeout=120, stream=True)
        if not r.ok:
            _log(f"backup failed: HTTP {r.status_code} {r.text[:120]}")
            return
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        size = dest.stat().st_size
        if size < 1024:
            _log(f"backup suspiciously small ({size} B) — check the secret")
        _log(f"backup saved {dest.name} ({size // 1024} KB)")
        _mirror(cfg, dest)
        for p in sorted(bdir.glob("app-*.db"), key=lambda p: p.stat().st_mtime,
                        reverse=True)[BACKUP_KEEP:]:
            p.unlink(missing_ok=True)
    except Exception as e:
        _log(f"backup error: {e}")
        return

    # weekly: also grab the uploads/ archive (client files, voice notes)
    tok = _api_token(cfg)
    if not tok:
        return
    recent = sorted(bdir.glob("uploads-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if recent and (time.time() - recent[0].stat().st_mtime) < UPLOADS_EVERY_DAYS * 86400:
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/uploads-archive",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=300, stream=True)
        if r.status_code == 404:
            return  # nothing to archive
        if not r.ok:
            _log(f"uploads archive failed: HTTP {r.status_code}")
            return
        udest = bdir / f"uploads-{datetime.now(timezone.utc).strftime('%Y%m%d')}.zip"
        with open(udest, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        _log(f"uploads archive saved {udest.name} ({udest.stat().st_size // 1024} KB)")
        _mirror(cfg, udest)
        for p in sorted(bdir.glob("uploads-*.zip"), key=lambda p: p.stat().st_mtime,
                        reverse=True)[4:]:
            p.unlink(missing_ok=True)
    except Exception as e:
        _log(f"uploads archive error: {e}")


# ── job 4: health check (timed + logged) ───────────────────────────────────
_HEALTH = {"fails": 0}


def job_health(cfg: dict) -> None:
    t0 = time.monotonic()
    ok, code, err = False, 0, ""
    try:
        r = requests.get(f"{cfg['url']}/api/health", timeout=20)
        code = r.status_code
        ok = r.ok
    except Exception as e:
        err = str(e)[:200]
    ms = int((time.monotonic() - t0) * 1000)

    logf = cfg["log_dir"] / "health.jsonl"
    try:
        cfg["log_dir"].mkdir(parents=True, exist_ok=True)
        with open(logf, "a") as fh:
            fh.write(json.dumps({"t": datetime.now(timezone.utc).isoformat(),
                                 "ok": ok, "code": code, "ms": ms, "err": err}) + "\n")
    except Exception:
        pass

    if ok:
        if _HEALTH["fails"]:
            _log(f"health recovered ({ms} ms)")
        _HEALTH["fails"] = 0
        if ms > cfg["slow_ms"]:
            _log(f"ALERT: slow response {ms} ms")
            _notify(cfg, f"Lumina slow: /api/health took {ms} ms (threshold {cfg['slow_ms']})")
        return

    _HEALTH["fails"] += 1
    msg = f"health check failed: HTTP {code}" if code else f"unreachable: {err}"
    _log(f"ALERT: {msg} (fail #{_HEALTH['fails']})")
    if _HEALTH["fails"] == 2:      # alert once, on the 2nd consecutive miss
        _notify(cfg, msg)


# ── job 5: keep the Baileys WhatsApp bridge alive ─────────────────────────
_BRIDGE: dict = {"proc": None, "started": 0.0, "dead_at": 0.0, "backoff": 0.0,
                 "fails": 0, "logf": None}


def job_bridge(cfg: dict) -> None:
    """(Re)spawn `node index.js` in whatsapp-bridge/ and keep it running.
    Backs off 5s per consecutive fast (<15s) fail, capped 60s."""
    if not cfg["bridge_ok"]:
        return
    now = time.monotonic()
    proc = _BRIDGE["proc"]

    if proc is not None:
        if proc.poll() is None:
            return
        up = now - _BRIDGE["started"]
        _BRIDGE["fails"] = _BRIDGE["fails"] + 1 if up < 15 else 0
        _BRIDGE["backoff"] = min(60.0, 5.0 * _BRIDGE["fails"])
        _BRIDGE["dead_at"] = now
        _BRIDGE["proc"] = None
        try:
            if _BRIDGE.get("logf"):
                _BRIDGE["logf"].close()
                _BRIDGE["logf"] = None
        except Exception:
            pass
        _log(f"bridge exited (code {proc.returncode}, up {up:.0f}s); "
             f"restart in {_BRIDGE['backoff']:.0f}s")
        return

    if now < _BRIDGE["dead_at"] + _BRIDGE["backoff"]:
        return

    env = dict(os.environ)
    env.setdefault("LUMINA_URL", cfg["url"])
    try:
        cfg["log_dir"].mkdir(parents=True, exist_ok=True)
        logp = cfg["log_dir"] / "bridge.log"
        # keep the log from growing forever
        try:
            if logp.exists() and logp.stat().st_size > 2_000_000:
                logp.replace(cfg["log_dir"] / "bridge.log.1")
        except Exception:
            pass
        _BRIDGE["logf"] = open(logp, "a", buffering=1, encoding="utf-8", errors="replace")
        _BRIDGE["proc"] = subprocess.Popen(
            [cfg["node"], "index.js"], cwd=str(cfg["bridge_dir"]), env=env,
            stdout=_BRIDGE["logf"], stderr=subprocess.STDOUT,
        )
        _BRIDGE["started"] = now
        _log(f"bridge started (pid {_BRIDGE['proc'].pid}) -> {logp}")
    except Exception as e:
        _BRIDGE["dead_at"] = now
        _BRIDGE["backoff"] = 30.0
        _log(f"bridge failed to start: {e}")


def _stop_bridge() -> None:
    proc = _BRIDGE.get("proc")
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        if _BRIDGE.get("logf"):
            _BRIDGE["logf"].close()
            _BRIDGE["logf"] = None
    except Exception:
        pass


# ── job 6: daily brief ────────────────────────────────────────────────────

def _digest(cfg: dict, window: str) -> None:
    tok = _api_token(cfg)
    if not tok:
        _log("digest: no token (set STORAGE_SYNC_TOKEN)")
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/digest",
                         params={"window": window},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        if not r.ok:
            _log(f"digest {window}: HTTP {r.status_code}")
            return
        text = (r.json() or {}).get("text", "")
    except Exception as e:
        _log(f"digest {window} error: {e}")
        return
    if not text:
        return
    title = "Lumina — morning brief" if window == "morning" else "Lumina — end of day"
    _digest_notify(cfg, title, text)
    _log(f"digest {window} sent ({len(text)} chars)")


def job_digest_morning(cfg: dict) -> None:
    _digest(cfg, "morning")


def job_digest_evening(cfg: dict) -> None:
    _digest(cfg, "evening")


# ── job 7: Google Sheets sync watchdog ───────────────────────────────────
_SHEETS = {"alerted": ""}


def job_sheets_watchdog(cfg: dict) -> None:
    tok = _api_token(cfg)
    if not tok:
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/sheets-health",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=45)
        if not r.ok:
            return
        j = r.json() or {}
    except Exception as e:
        _log(f"sheets watchdog error: {e}")
        return

    bad = [l for l in j.get("links", []) if l.get("push_failing") or l.get("pull_stale")]
    if bad:
        names = ", ".join(l.get("client_name") or l.get("client_id") for l in bad)
        if names != _SHEETS["alerted"]:      # only re-alert when the set changes
            _notify(cfg, f"Google Sheet sync issue for: {names}")
            _SHEETS["alerted"] = names
        if cfg["sheets_autopull"]:
            try:
                pr = requests.post(f"{cfg['url']}/api/companion/sheets-pull-all",
                                   headers={"Authorization": f"Bearer {tok}"}, timeout=180)
                _log(f"sheets auto-pull: {pr.status_code}")
            except Exception as e:
                _log(f"sheets auto-pull error: {e}")
    else:
        _SHEETS["alerted"] = ""


# ── job 10: deliver queued proactive WhatsApp messages ───────────────────
#
# The Railway backend can't reach the Baileys bridge (loopback on this
# laptop), so whatsapp_agent queues "X assigned you a task" / "remind Y"
# messages in the DB. We poll for them and send via the local bridge.

def job_wa_outbox(cfg: dict) -> None:
    if not cfg["bridge_ok"]:
        return
    tok = _api_token(cfg)
    if not tok:
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/whatsapp-outbox",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        if not r.ok:
            return
        msgs = (r.json() or {}).get("messages") or []
    except Exception as e:
        _log(f"wa-outbox error: {e}")
        return
    if not msgs:
        return
    sent, failed = [], []
    for m in msgs:
        to, text, mid = m.get("to"), m.get("text"), m.get("id")
        if not (to and text and mid):
            continue
        (sent if _bridge_send(cfg, to, text) else failed).append(mid)
    try:
        requests.post(f"{cfg['url']}/api/companion/whatsapp-outbox/ack",
                      json={"sent": sent, "failed": failed},
                      headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    except Exception as e:
        _log(f"wa-outbox ack error: {e}")
    _log(f"wa-outbox: {len(sent)} sent, {len(failed)} failed")


INTERVAL_JOBS = [
    ("sync", job_sync, "sync_every"),
    ("research", job_research, "research_every"),
    ("backup", job_backup, "backup_every"),
    ("health", job_health, "health_every"),
    ("bridge", job_bridge, "bridge_every"),
    ("sheets", job_sheets_watchdog, "sheets_every"),
    ("wa-outbox", job_wa_outbox, "outbox_every"),
]


# ── job 8: noon attendance roll-call to the team WhatsApp group ───────────

def _bridge_send(cfg: dict, to: str, text: str) -> bool:
    """POST a proactive message to the local Baileys bridge's send endpoint."""
    tok = (os.getenv("WHATSAPP_BRIDGE_TOKEN") or cfg["storage_token"]
           or cfg["db_secret"])
    if not tok:
        _log("bridge send: no token")
        return False
    try:
        r = requests.post(
            f"http://127.0.0.1:{cfg['bridge_http_port']}/send",
            json={"to": to, "text": text},
            headers={"Authorization": f"Bearer {tok}"}, timeout=30,
        )
        if r.ok:
            _log(f"bridge send -> {to} ({len(text)} chars)")
            return True
        _log(f"bridge send: HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:
        _log(f"bridge send error: {e}")
    return False


def job_standup_nudge(cfg: dict) -> None:
    """11:30 — DM anyone who hasn't put a fresh task on today's standup."""
    tok = _api_token(cfg)
    if not tok:
        _log("standup nudge: no token")
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/standup-missing",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=45)
        if not r.ok:
            _log(f"standup nudge: HTTP {r.status_code}")
            return
        j = r.json() or {}
    except Exception as e:
        _log(f"standup nudge error: {e}")
        return
    if j.get("weekend"):
        _log("standup nudge: weekend — skipping")
        return
    missing = j.get("missing") or []
    if not missing:
        _log("standup nudge: everyone's added a task")
        return
    sent = 0
    for p in missing:
        wa = re.sub(r"\D", "", p.get("whatsapp", ""))
        if not wa:
            continue
        name = p.get("name") or "there"
        text = (
            f"Morning {name} — it's gone 11:30 and your standup for today is "
            "still a blank canvas. Add a task or two when you get a moment.\n\n"
            "Shortcut: just reply here with what you're working on and I'll drop "
            "it straight into your standup for you."
        )
        if _bridge_send(cfg, f"{wa}@s.whatsapp.net", text):
            sent += 1
    _log(f"standup nudge: {sent}/{len(missing)} DMs sent")


_LUNCH_LINES = [
    "It's 2 o'clock. Lunch. Step away from the screen, go eat something. 🍽",
    "2pm — lunch break, everyone. Food first, deadlines after.",
    "Lunchtime. Close the laptop for a bit and go grab a proper meal. 🍛",
    "It's 2. Go have lunch, the tasks will still be here when you're back.",
    "2pm lunch call — take the break, you've earned it. 🍴",
]


def job_lunch(cfg: dict) -> None:
    """14:00 — nudge the team WhatsApp group to go for lunch."""
    grp = cfg["rollcall_group"]
    if not grp:
        return
    if datetime.now().weekday() >= 5:      # Sat/Sun
        _log("lunch: weekend — skipping")
        return
    idx = datetime.now().timetuple().tm_yday % len(_LUNCH_LINES)
    if _bridge_send(cfg, grp, _LUNCH_LINES[idx]):
        _log("lunch: sent")


def job_rollcall(cfg: dict) -> None:
    grp = cfg["rollcall_group"]
    if not grp:
        return
    tok = _api_token(cfg)
    if not tok:
        _log("rollcall: no token (set STORAGE_SYNC_TOKEN)")
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/attendance-missing",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=45)
        if not r.ok:
            _log(f"rollcall: HTTP {r.status_code}")
            return
        j = r.json() or {}
    except Exception as e:
        _log(f"rollcall error: {e}")
        return
    if j.get("weekend"):
        _log("rollcall: weekend — skipping")
        return
    missing = j.get("missing") or []
    if not missing:
        _log("rollcall: everyone logged in — no message sent")
        return
    text = j.get("text") or ("Not logged in yet: " + ", ".join(missing))
    _bridge_send(cfg, grp, text)


# ── wall-clock daily jobs ────────────────────────────────────────────────
_daily_fired: dict = {}


def _daily_due(target_hhmm: str, now: datetime, window_min: int = 40) -> bool:
    try:
        h, m = (int(x) for x in target_hhmm.split(":"))
    except Exception:
        return False
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (now - target).total_seconds()
    return 0 <= delta < window_min * 60


def _run_daily(daily_jobs: list, cfg: dict) -> None:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    for name, fn, hhmm in daily_jobs:
        if _daily_fired.get(name) == today:
            continue
        if _daily_due(hhmm, now):
            _daily_fired[name] = today
            try:
                fn(cfg)
            except Exception as e:
                _log(f"{name} error: {e}")


# ── main ─────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Always-on companion for Lumina.")
    ap.add_argument("--dir", default="./knowledge", help="knowledge folder")
    ap.add_argument("--url", default=os.getenv("LUMINA_URL", DEFAULT_URL))
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--backup-mirror", default="",
                    help="second folder to copy each backup into (e.g. a OneDrive/Drive path)")
    ap.add_argument("--sync-every", type=int, default=30)
    ap.add_argument("--research-every", type=int, default=3600)
    ap.add_argument("--backup-every", type=int, default=86400)
    ap.add_argument("--health-every", type=int, default=300)
    ap.add_argument("--sheets-every", type=int, default=1200)
    ap.add_argument("--outbox-every", type=int, default=20,
                    help="how often to check for queued proactive WhatsApp messages")
    ap.add_argument("--sheets-autopull", action="store_true",
                    help="also force a reconcile when a sheet looks out of sync")
    ap.add_argument("--slow-ms", type=int, default=8000, help="alert if /api/health is slower than this")
    ap.add_argument("--digest-morning", default="09:00")
    ap.add_argument("--digest-evening", default="19:00")
    ap.add_argument("--no-digest", action="store_true")
    ap.add_argument("--rollcall-time", default="12:00",
                    help="daily time to post the attendance roll-call to the group")
    ap.add_argument("--rollcall-group", default=os.getenv("ROLLCALL_GROUP_ID", ""),
                    help="WhatsApp group id (digits or ...@g.us) for the roll-call; "
                         "empty = feature off")
    ap.add_argument("--no-rollcall", action="store_true")
    ap.add_argument("--lunch-time", default="14:00",
                    help="daily time to post the 'go for lunch' nudge to the group")
    ap.add_argument("--no-lunch", action="store_true")
    ap.add_argument("--standup-nudge-time", default="11:30",
                    help="daily time to DM people who haven't added a standup task")
    ap.add_argument("--no-standup-nudge", action="store_true")
    ap.add_argument("--bridge-dir", default="",
                    help="path to whatsapp-bridge/ (default: sibling of this repo's scripts/)")
    ap.add_argument("--no-bridge", action="store_true", help="don't supervise the WhatsApp bridge")
    args = ap.parse_args()

    root = Path(args.dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    bridge_dir = (Path(args.bridge_dir).expanduser().resolve() if args.bridge_dir
                  else Path(__file__).resolve().parent.parent / "whatsapp-bridge")
    node = shutil.which("node") or ""
    bridge_ok = (
        not args.no_bridge
        and bool(node)
        and (bridge_dir / "index.js").exists()
        and (bridge_dir / "node_modules").exists()
        and bool(os.getenv("WHATSAPP_BRIDGE_TOKEN") or os.getenv("STORAGE_SYNC_TOKEN")
                 or os.getenv("FLASK_SECRET_KEY"))
    )

    backup_dir = (Path(args.backup_dir).expanduser().resolve() if args.backup_dir
                  else root.parent / "lumina-backups")
    cfg = {
        "dir": root,
        "url": args.url.rstrip("/"),
        "backup_dir": backup_dir,
        "backup_mirror": Path(args.backup_mirror).expanduser().resolve() if args.backup_mirror else None,
        "rclone_remote": os.getenv("BACKUP_RCLONE_REMOTE", ""),
        "log_dir": root.parent / "lumina-logs",
        "storage_token": os.getenv("STORAGE_SYNC_TOKEN") or os.getenv("FLASK_SECRET_KEY") or "",
        "db_secret": os.getenv("FLASK_SECRET_KEY") or os.getenv("SECRET_KEY") or "",
        "alert_webhook": os.getenv("ALERT_WEBHOOK", ""),
        "alert_targets": [t.strip() for t in os.getenv("ALERT_TARGETS", "").split(",") if t.strip()],
        "digest_targets": [t.strip() for t in os.getenv("DIGEST_TARGETS", "").split(",") if t.strip()],
        "bridge_dir": bridge_dir,
        "bridge_ok": bridge_ok,
        "node": node,
        "slow_ms": args.slow_ms,
        "sheets_autopull": args.sheets_autopull,
        "sync_every": args.sync_every,
        "research_every": args.research_every,
        "backup_every": args.backup_every,
        "health_every": args.health_every,
        "sheets_every": args.sheets_every,
        "outbox_every": args.outbox_every,
        "bridge_every": 15,
        "bridge_http_port": int(os.getenv("BRIDGE_HTTP_PORT", "8787")),
        "rollcall_group": (lambda d: f"{d}@g.us" if d else "")(
            re.sub(r"\D", "", args.rollcall_group or "")),
    }

    daily_jobs = []
    if not args.no_digest:
        daily_jobs += [
            ("digest-morning", job_digest_morning, args.digest_morning),
            ("digest-evening", job_digest_evening, args.digest_evening),
        ]
    if not args.no_rollcall and cfg["rollcall_group"]:
        daily_jobs.append(("rollcall", job_rollcall, args.rollcall_time))
    if not args.no_lunch and cfg["rollcall_group"]:
        daily_jobs.append(("lunch", job_lunch, args.lunch_time))
    if not args.no_standup_nudge:
        daily_jobs.append(("standup-nudge", job_standup_nudge, args.standup_nudge_time))

    if not bridge_ok and not args.no_bridge:
        reason = ("node not on PATH" if not node
                  else "whatsapp-bridge/ missing or `npm install` not run"
                  if not (bridge_dir / "node_modules").exists() else "no token")
        bridge_note = f"OFF ({reason})"
    else:
        bridge_note = "OFF (--no-bridge)" if args.no_bridge else "on"

    tok = cfg["storage_token"] or cfg["db_secret"]
    _log(f"laptop_agent up — {root}  <->  {cfg['url']}")
    _log("  sync {} | research {} | backup {}{} | health on | sheets {} | digest {} | rollcall {} | bridge {}".format(
        "on" if cfg["storage_token"] else "OFF (no STORAGE_SYNC_TOKEN)",
        "on" if (root / research_feed.SOURCES_FILE).exists() else "OFF (no research_sources.txt)",
        "on" if cfg["db_secret"] else "OFF (no FLASK_SECRET_KEY)",
        (" +mirror" if cfg["backup_mirror"] else "") + (" +rclone" if cfg["rclone_remote"] else ""),
        "on" if tok else "OFF (no token)",
        "OFF (--no-digest)" if args.no_digest else (
            f"{args.digest_morning}/{args.digest_evening}" if tok else "OFF (no token)"),
        f"{args.rollcall_time}" if (cfg["rollcall_group"] and not args.no_rollcall and tok)
        else "OFF (no group)" if not cfg["rollcall_group"] else "OFF",
        bridge_note,
    ))
    _log("  standup-nudge {}".format(
        args.standup_nudge_time if (not args.no_standup_nudge and tok)
        else "OFF (--no-standup-nudge)" if args.no_standup_nudge else "OFF (no token)"))
    _log("  wa-outbox {}".format(
        f"every {args.outbox_every}s" if (cfg["bridge_ok"] and tok)
        else "OFF (needs bridge + token)"))
    _log("  lunch nudge {}".format(
        args.lunch_time if (cfg["rollcall_group"] and not args.no_lunch)
        else "OFF (--no-lunch)" if args.no_lunch else "OFF (no group)"))
    ch = (f"apprise x{len(cfg['alert_targets'])}" if cfg["alert_targets"] and apprise
          else "webhook" if cfg["alert_webhook"] else "console-only")
    _log(f"  alert channel: {ch}")

    next_run = {name: 0.0 for name, _, _ in INTERVAL_JOBS}
    try:
        while True:
            now = time.monotonic()
            for name, fn, every_key in INTERVAL_JOBS:
                if now >= next_run[name]:
                    try:
                        fn(cfg)
                    except Exception as e:
                        _log(f"{name} error: {e}")
                    next_run[name] = now + cfg[every_key]
            if daily_jobs:
                _run_daily(daily_jobs, cfg)
            time.sleep(5)
    except KeyboardInterrupt:
        _log("stopped.")
    finally:
        _stop_bridge()


if __name__ == "__main__":
    main()
