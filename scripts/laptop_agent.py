#!/usr/bin/env python3
"""
laptop_agent.py — one always-on process that gets the most out of a spare
machine sitting next to Lumina.

It runs four jobs on their own intervals:

  1. knowledge sync   — mirror your knowledge folder up to Lumina   (default 30s)
  2. research feed     — refresh research_sources.txt web snapshots  (default 1h)
  3. DB backup         — pull logs/app.db off Railway to local disk  (default 24h)
  4. health check      — ping Lumina; log + optional webhook alert   (default 5m)

Jobs 2–4 are skipped automatically if not configured (no research_sources.txt,
no FLASK_SECRET_KEY, etc.), so it's safe to just run it.

Setup:
    pip install requests
    set LUMINA_URL=https://lumina.mmga.agency
    set STORAGE_SYNC_TOKEN=<token you set on Railway>     (for job 1)
    set FLASK_SECRET_KEY=<Railway FLASK_SECRET_KEY>        (for job 3, optional)
    set ALERT_WEBHOOK=<Slack/Discord incoming webhook URL> (for job 4, optional)

Run (leave it running):
    python scripts/laptop_agent.py --dir "C:\\Users\\me\\LuminaKnowledge"

To keep it alive across reboots: Task Scheduler → "At log on" →
    pythonw.exe <path>\\scripts\\laptop_agent.py --dir "C:\\Users\\me\\LuminaKnowledge"
"""

from __future__ import annotations

import os
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import requests
except ImportError:
    sys.exit("laptop_agent: `pip install requests` first.")

import kb_sync
import research_feed

DEFAULT_URL = "https://lumina.mmga.agency"
BACKUP_KEEP = 14


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── jobs ────────────────────────────────────────────────────────────────────

def job_sync(cfg: dict) -> None:
    tok = cfg["storage_token"]
    if not tok:
        return
    try:
        kb_sync.sync_once(cfg["dir"], cfg["url"], tok)
    except Exception as e:
        _log(f"sync error: {e}")


def job_research(cfg: dict) -> None:
    if not (cfg["dir"] / research_feed.SOURCES_FILE).exists():
        return
    try:
        research_feed.run_once(cfg["dir"])
    except Exception as e:
        _log(f"research error: {e}")


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
            _log(f"backup suspiciously small ({size} B) — check the secret: {dest.name}")
        _log(f"backup saved {dest.name} ({size // 1024} KB)")
        # retention
        olds = sorted(bdir.glob("app-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in olds[BACKUP_KEEP:]:
            p.unlink(missing_ok=True)
    except Exception as e:
        _log(f"backup error: {e}")


def job_health(cfg: dict) -> None:
    try:
        r = requests.get(f"{cfg['url']}/api/health", timeout=15)
        if r.ok:
            return
        msg = f"Lumina health check failed: HTTP {r.status_code}"
    except Exception as e:
        msg = f"Lumina unreachable: {e}"
    _log("ALERT: " + msg)
    hook = cfg["alert_webhook"]
    if hook:
        try:
            requests.post(hook, json={"text": "Lumina watchdog: " + msg}, timeout=15)
        except Exception as e:
            _log(f"alert webhook failed: {e}")


JOBS = [
    ("sync", job_sync, "sync_every"),
    ("research", job_research, "research_every"),
    ("backup", job_backup, "backup_every"),
    ("health", job_health, "health_every"),
]


def main() -> None:
    ap = argparse.ArgumentParser(description="Always-on companion for Lumina.")
    ap.add_argument("--dir", default="./knowledge", help="knowledge folder")
    ap.add_argument("--url", default=os.getenv("LUMINA_URL", DEFAULT_URL))
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--sync-every", type=int, default=30)
    ap.add_argument("--research-every", type=int, default=3600)
    ap.add_argument("--backup-every", type=int, default=86400)
    ap.add_argument("--health-every", type=int, default=300)
    args = ap.parse_args()

    root = Path(args.dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    cfg = {
        "dir": root,
        "url": args.url.rstrip("/"),
        "backup_dir": Path(args.backup_dir).expanduser().resolve() if args.backup_dir
        else root.parent / "lumina-backups",
        "storage_token": os.getenv("STORAGE_SYNC_TOKEN") or os.getenv("FLASK_SECRET_KEY") or "",
        "db_secret": os.getenv("FLASK_SECRET_KEY") or os.getenv("SECRET_KEY") or "",
        "alert_webhook": os.getenv("ALERT_WEBHOOK", ""),
        "sync_every": args.sync_every,
        "research_every": args.research_every,
        "backup_every": args.backup_every,
        "health_every": args.health_every,
    }

    _log(f"laptop_agent up — {root}  <->  {cfg['url']}")
    _log(f"  sync {'on' if cfg['storage_token'] else 'OFF (no STORAGE_SYNC_TOKEN)'} | "
         f"research {'on' if (root / research_feed.SOURCES_FILE).exists() else 'OFF (no research_sources.txt)'} | "
         f"backup {'on' if cfg['db_secret'] else 'OFF (no FLASK_SECRET_KEY)'} | "
         f"health on")

    next_run = {name: 0.0 for name, _, _ in JOBS}
    try:
        while True:
            now = time.monotonic()
            for name, fn, every_key in JOBS:
                if now >= next_run[name]:
                    fn(cfg)
                    next_run[name] = now + cfg[every_key]
            time.sleep(5)
    except KeyboardInterrupt:
        _log("stopped.")


if __name__ == "__main__":
    main()
