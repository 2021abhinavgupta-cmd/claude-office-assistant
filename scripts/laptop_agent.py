#!/usr/bin/env python3
"""
laptop_agent.py — one always-on process that gets the most out of a spare
machine sitting next to Lumina.

It runs five jobs, each managed on its own:

  1. knowledge sync   — mirror your knowledge folder up to Lumina   (default 30s)
  2. research feed     — refresh research_sources.txt web snapshots  (default 1h)
  3. DB backup         — pull logs/app.db off Railway to local disk  (default 24h)
  4. health check      — ping Lumina; log + optional webhook alert   (default 5m)
  5. WhatsApp bridge   — keep  whatsapp-bridge/index.js  (Baileys) alive, restart on crash

Jobs 2–5 are skipped automatically if not configured (no research_sources.txt,
no FLASK_SECRET_KEY, no node / no whatsapp-bridge folder, etc.), so it's safe to
just run it — you get whatever you've set up, nothing else.

Setup:
    pip install -r scripts/requirements.txt
    set LUMINA_URL=https://lumina.mmga.agency
    set STORAGE_SYNC_TOKEN=<token you set on Railway>     (for job 1)
    set FLASK_SECRET_KEY=<Railway FLASK_SECRET_KEY>        (for job 3, optional)
    set ALERT_TARGETS=tgram://bottoken/chatid,mailto://... (for job 4, optional; apprise URLs)
    set ALERT_WEBHOOK=<Slack/Discord incoming webhook URL> (job 4 fallback if no ALERT_TARGETS)
    set WHATSAPP_BRIDGE_TOKEN=<same value you set on Railway> (for job 5)
      one-time first: cd whatsapp-bridge && npm install && node index.js   (scan the QR once)

Run (leave it running):
    python scripts/laptop_agent.py --dir "C:\\Users\\me\\LuminaKnowledge"

To keep it alive across reboots: Task Scheduler → "At log on" →
    pythonw.exe <path>\\scripts\\laptop_agent.py --dir "C:\\Users\\me\\LuminaKnowledge"
"""

from __future__ import annotations

import os
import sys
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


def _notify(cfg: dict, msg: str) -> None:
    """Fan out an alert: apprise targets (Telegram/Discord/email/ntfy/…) if
    set, else a plain JSON webhook, else just the console log."""
    text = "Lumina watchdog: " + msg
    targets = cfg["alert_targets"]
    if targets and apprise is not None:
        try:
            ap = apprise.Apprise()
            for t in targets:
                ap.add(t)
            ap.notify(title="Lumina watchdog", body=msg)
            return
        except Exception as e:
            _log(f"apprise notify failed: {e}")
    hook = cfg["alert_webhook"]
    if hook:
        try:
            requests.post(hook, json={"text": text}, timeout=15)
        except Exception as e:
            _log(f"alert webhook failed: {e}")


def job_health(cfg: dict) -> None:
    try:
        r = requests.get(f"{cfg['url']}/api/health", timeout=15)
        if r.ok:
            return
        msg = f"health check failed: HTTP {r.status_code}"
    except Exception as e:
        msg = f"unreachable: {e}"
    _log("ALERT: " + msg)
    _notify(cfg, msg)


# ── job 5: keep the Baileys WhatsApp bridge alive ───────────────────────────
_BRIDGE: dict = {"proc": None, "started": 0.0, "dead_at": 0.0, "backoff": 0.0, "fails": 0}


def job_bridge(cfg: dict) -> None:
    """(Re)spawn `node index.js` in whatsapp-bridge/ and keep it running.

    Skips entirely if node isn't installed, the folder/deps are missing, or
    no bridge token is set. On a fast crash (< 15s up) it backs off (5s per
    consecutive fast fail, capped 60s), so a misconfig doesn't hot-loop."""
    if not cfg["bridge_ok"]:
        return
    now = time.monotonic()
    proc = _BRIDGE["proc"]

    if proc is not None:
        if proc.poll() is None:
            return  # still running
        # just found it dead — account for the crash exactly once
        up = now - _BRIDGE["started"]
        _BRIDGE["fails"] = _BRIDGE["fails"] + 1 if up < 15 else 0
        _BRIDGE["backoff"] = min(60.0, 5.0 * _BRIDGE["fails"])
        _BRIDGE["dead_at"] = now
        _BRIDGE["proc"] = None
        _log(f"bridge exited (code {proc.returncode}, up {up:.0f}s); "
             f"restart in {_BRIDGE['backoff']:.0f}s")
        return

    if now < _BRIDGE["dead_at"] + _BRIDGE["backoff"]:
        return  # still cooling down after a crash

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
        proc.terminate()          # Baileys persists its session to ./auth on the way
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


JOBS = [
    ("sync", job_sync, "sync_every"),
    ("research", job_research, "research_every"),
    ("backup", job_backup, "backup_every"),
    ("health", job_health, "health_every"),
    ("bridge", job_bridge, "bridge_every"),
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

    cfg = {
        "dir": root,
        "url": args.url.rstrip("/"),
        "backup_dir": Path(args.backup_dir).expanduser().resolve() if args.backup_dir
        else root.parent / "lumina-backups",
        "storage_token": os.getenv("STORAGE_SYNC_TOKEN") or os.getenv("FLASK_SECRET_KEY") or "",
        "db_secret": os.getenv("FLASK_SECRET_KEY") or os.getenv("SECRET_KEY") or "",
        "alert_webhook": os.getenv("ALERT_WEBHOOK", ""),
        "alert_targets": [t.strip() for t in os.getenv("ALERT_TARGETS", "").split(",") if t.strip()],
        "bridge_dir": bridge_dir,
        "bridge_ok": bridge_ok,
        "node": node,
        "sync_every": args.sync_every,
        "research_every": args.research_every,
        "backup_every": args.backup_every,
        "health_every": args.health_every,
        "bridge_every": 15,
    }

    if not bridge_ok and not args.no_bridge:
        reason = ("node not on PATH" if not node
                  else "whatsapp-bridge/ missing or `npm install` not run" if not (bridge_dir / "node_modules").exists()
                  else "no WHATSAPP_BRIDGE_TOKEN")
        _bridge_note = f"OFF ({reason})"
    elif args.no_bridge:
        _bridge_note = "OFF (--no-bridge)"
    else:
        _bridge_note = "on"

    _log(f"laptop_agent up — {root}  <->  {cfg['url']}")
    _log(f"  sync {'on' if cfg['storage_token'] else 'OFF (no STORAGE_SYNC_TOKEN)'} | "
         f"research {'on' if (root / research_feed.SOURCES_FILE).exists() else 'OFF (no research_sources.txt)'} | "
         f"backup {'on' if cfg['db_secret'] else 'OFF (no FLASK_SECRET_KEY)'} | "
         f"health on"
         + (f" (apprise -> {len(cfg['alert_targets'])} target(s))" if cfg['alert_targets'] and apprise
            else " (webhook alert)" if cfg['alert_webhook'] else " (console-only alert)")
         + f" | bridge {_bridge_note}")

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
    finally:
        _stop_bridge()


if __name__ == "__main__":
    main()
