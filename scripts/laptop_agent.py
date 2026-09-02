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

Setup:
    pip install -r scripts/requirements.txt
    set LUMINA_URL=https://lumina.mmga.agency
    set STORAGE_SYNC_TOKEN=<token you set on Railway>       (jobs 1, 6, 7)
    set FLASK_SECRET_KEY=<Railway FLASK_SECRET_KEY>          (job 3)
    set ALERT_TARGETS=tgram://token/chatid,mailto://...      (job 4 alerts; apprise URLs)
    set DIGEST_TARGETS=tgram://token/chatid                  (job 6; falls back to ALERT_TARGETS)
    set ALERT_WEBHOOK=<Slack/Discord webhook>                (fallback if no *_TARGETS)
    set BACKUP_RCLONE_REMOTE=myremote:lumina-backups         (job 3 offsite, optional)
    set WHATSAPP_BRIDGE_TOKEN=<token you set on Railway>      (job 5)

Run:
    python scripts/laptop_agent.py --dir "C:\\Users\\me\\LuminaKnowledge"

Autostart: Task Scheduler → "At log on" →
    pythonw.exe <path>\\scripts\\laptop_agent.py --dir "C:\\Users\\me\\LuminaKnowledge"
"""

from __future__ import annotations

import os
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
_BRIDGE: dict = {"proc": None, "started": 0.0, "dead_at": 0.0, "backoff": 0.0, "fails": 0}


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
        _log(f"bridge exited (code {proc.returncode}, up {up:.0f}s); "
             f"restart in {_BRIDGE['backoff']:.0f}s")
        return

    if now < _BRIDGE["dead_at"] + _BRIDGE["backoff"]:
        return

    env = dict(os.environ)
    env.setdefault("LUMINA_URL", cfg["url"])
    try:
        _BRIDGE["proc"] = subprocess.Popen(
            [cfg["node"], "index.js"], cwd=str(cfg["bridge_dir"]), env=env,
        )
        _BRIDGE["started"] = now
        _log(f"bridge started (pid {_BRIDGE['proc'].pid})")
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


INTERVAL_JOBS = [
    ("sync", job_sync, "sync_every"),
    ("research", job_research, "research_every"),
    ("backup", job_backup, "backup_every"),
    ("health", job_health, "health_every"),
    ("bridge", job_bridge, "bridge_every"),
    ("sheets", job_sheets_watchdog, "sheets_every"),
]


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
    ap.add_argument("--sheets-autopull", action="store_true",
                    help="also force a reconcile when a sheet looks out of sync")
    ap.add_argument("--slow-ms", type=int, default=8000, help="alert if /api/health is slower than this")
    ap.add_argument("--digest-morning", default="09:00")
    ap.add_argument("--digest-evening", default="19:00")
    ap.add_argument("--no-digest", action="store_true")
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
        "bridge_every": 15,
    }

    daily_jobs = []
    if not args.no_digest:
        daily_jobs = [
            ("digest-morning", job_digest_morning, args.digest_morning),
            ("digest-evening", job_digest_evening, args.digest_evening),
        ]

    if not bridge_ok and not args.no_bridge:
        reason = ("node not on PATH" if not node
                  else "whatsapp-bridge/ missing or `npm install` not run"
                  if not (bridge_dir / "node_modules").exists() else "no token")
        bridge_note = f"OFF ({reason})"
    else:
        bridge_note = "OFF (--no-bridge)" if args.no_bridge else "on"

    tok = cfg["storage_token"] or cfg["db_secret"]
    _log(f"laptop_agent up — {root}  <->  {cfg['url']}")
    _log("  sync {} | research {} | backup {}{} | health on | sheets {} | digest {} | bridge {}".format(
        "on" if cfg["storage_token"] else "OFF (no STORAGE_SYNC_TOKEN)",
        "on" if (root / research_feed.SOURCES_FILE).exists() else "OFF (no research_sources.txt)",
        "on" if cfg["db_secret"] else "OFF (no FLASK_SECRET_KEY)",
        (" +mirror" if cfg["backup_mirror"] else "") + (" +rclone" if cfg["rclone_remote"] else ""),
        "on" if tok else "OFF (no token)",
        "OFF (--no-digest)" if args.no_digest else (
            f"{args.digest_morning}/{args.digest_evening}" if tok else "OFF (no token)"),
        bridge_note,
    ))
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
