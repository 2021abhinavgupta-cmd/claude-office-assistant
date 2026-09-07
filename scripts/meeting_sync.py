#!/usr/bin/env python3
"""
meeting_sync.py — turn local meeting transcripts/summaries into real Lumina
tasks, without a bot joining the call.

The actual capture happens on whoever's ATTENDING laptop, via a local tool
that listens to system audio during the call and exports a markdown summary
-- meeting-scribe (github.com/elmoghany/meeting-scribe) or TalkTrack
(github.com/ObscureAintSecure/TalkTrack) both work, both free/local/no API
key. This script watches wherever that tool writes its `.md` exports and
POSTs each new one to Lumina's POST /api/ai/meeting-to-tasks, which uses
one Haiku call to pull out real action items (title/owner/due date) and
creates a board task + standup entry for each.

Why markdown specifically, not the tool's JSON export: this script was
written without hands-on access to either tool's exact JSON schema, and
guessing at field names has bitten this codebase before (see CLAUDE.md).
A markdown/plain-text summary is universal -- every sane transcription
tool produces readable prose, and Lumina's own endpoint does the
structured extraction from that prose via Claude, not by trusting a
third-party tool's internal JSON shape.

Setup (once, on whichever laptop attends meetings):
    pip install requests
    Install meeting-scribe or TalkTrack, point it at wherever you'll run
    this script's --dir (or vice versa -- just make sure they agree).

Run after a meeting (or leave --watch running):
    python scripts/meeting_sync.py --dir "C:\\cornell\\meetingnotes" --user-id emp003
    python scripts/meeting_sync.py --dir ./meeting-notes --user-id emp003 --watch

Each .md file in --dir is processed once (tracked in a small state file in
that same folder) and never resubmitted, even if edited afterward --
resubmitting an edited transcript would create a second batch of tasks
rather than updating the first, which is worse than just not re-syncing.
Delete a line from the state file (or the whole file) to force a re-run
for one meeting.
"""

from __future__ import annotations

import os
import sys
import time
import json
import argparse
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("meeting_sync: `pip install requests` first.")

DEFAULT_URL = "https://lumina.mmga.agency"
STATE_FILE = ".meeting_sync_state.json"
MIN_BYTES = 20     # skip near-empty exports (e.g. a meeting that never started)
MAX_BYTES = 500_000


def _load_state(root: Path) -> dict:
    f = root / STATE_FILE
    if f.exists():
        try:
            return json.loads(f.read_text("utf-8"))
        except Exception:
            pass
    return {}


def _save_state(root: Path, state: dict) -> None:
    (root / STATE_FILE).write_text(json.dumps(state, indent=2), "utf-8")


def _title_from_filename(path: Path) -> str:
    # meeting-scribe/TalkTrack filenames are typically date/title-ish
    # already -- just clean it up a bit for use as a task-title suffix.
    stem = path.stem.replace("_", " ").replace("-", " ").strip()
    return stem[:80]


def process_one(base: str, path: Path, user_id: str, client: str) -> bool:
    try:
        text = path.read_text("utf-8", errors="replace").strip()
    except Exception as e:
        print(f"  skip (unreadable): {path.name} -- {e}")
        return False
    size = len(text.encode("utf-8"))
    if size < MIN_BYTES:
        print(f"  skip (too short, {size}B): {path.name}")
        return False
    if size > MAX_BYTES:
        text = text[:MAX_BYTES]
        print(f"  (truncated to {MAX_BYTES}B): {path.name}")

    payload = {
        "notes": text,
        "meeting_title": _title_from_filename(path),
        "client": client,
        "user_id": user_id,
    }
    try:
        r = requests.post(f"{base}/api/ai/meeting-to-tasks",
                          params={"user_id": user_id}, json=payload, timeout=90)
    except Exception as e:
        print(f"  FAIL {path.name}: {e}")
        return False
    if not r.ok:
        print(f"  FAIL {path.name}  [{r.status_code}] {r.text[:200]}")
        return False
    j = r.json()
    created = j.get("tasks_created", 0)
    failed = j.get("tasks_failed", 0)
    print(f"  ok   {path.name}  -> {created} task(s) created"
          + (f", {failed} failed" if failed else "")
          + (f" ({j.get('message')})" if not created and j.get("message") else ""))
    return True


def sync_once(root: Path, base: str, user_id: str, client: str) -> None:
    state = _load_state(root)
    md_files = sorted(p for p in root.glob("*.md") if p.name != STATE_FILE)
    new_state = dict(state)
    processed = 0
    for path in md_files:
        rel = path.name
        if rel in state:
            continue
        if process_one(base, path, user_id, client):
            new_state[rel] = int(time.time())
            processed += 1
    if new_state != state:
        _save_state(root, new_state)
    if processed:
        print(f"meeting_sync: {processed} new meeting export(s) processed "
              f"({len(md_files)} total .md files in {root})")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Turn local meeting-export .md files into real Lumina tasks.")
    ap.add_argument("--dir", required=True,
                    help="folder meeting-scribe/TalkTrack exports .md files into")
    ap.add_argument("--url", default=os.getenv("LUMINA_URL", DEFAULT_URL))
    ap.add_argument("--user-id", required=True,
                    help="employee id to attribute this run to (admin-gated, "
                         "same bool(user_id) convention as the rest of Lumina)")
    ap.add_argument("--client", default="",
                    help="client name to tag every task from this run with "
                         "(optional -- the extraction also tries to infer one "
                         "per item, this is just a fallback default)")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    root = Path(args.dir).expanduser()
    if not root.is_dir():
        sys.exit(f"meeting_sync: {root} doesn't exist or isn't a folder -- "
                 f"point --dir at wherever your transcription tool writes .md exports.")
    base = args.url.rstrip("/")

    print(f"meeting_sync: {root}  ->  {base}  (user_id={args.user_id})")
    sync_once(root, base, args.user_id, args.client)
    if not args.watch:
        return
    print(f"meeting_sync: watching every {args.interval}s (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(args.interval)
            sync_once(root, base, args.user_id, args.client)
    except KeyboardInterrupt:
        print("\nmeeting_sync: stopped.")


if __name__ == "__main__":
    main()
