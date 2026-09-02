#!/usr/bin/env python3
"""
kb_sync.py — keep a local folder in sync with the Lumina knowledge store.

Drop documents into a folder on this machine; this script uploads new/changed
ones to Lumina and deletes ones you removed. The WhatsApp agent and the
in-app knowledge base then answer from them.

Setup (once):
    pip install requests
    set LUMINA_URL=https://lumina.mmga.agency        (default; override for local)
    set STORAGE_SYNC_TOKEN=<the token you set on Railway>
        (if you didn't set STORAGE_SYNC_TOKEN on the server, use FLASK_SECRET_KEY)

Run:
    python scripts/kb_sync.py --dir "C:\\Users\\me\\LuminaKnowledge"
    python scripts/kb_sync.py --dir ./knowledge --watch          # loop forever

Supported files: .txt .md .pdf .docx .csv .xlsx .json  (others are skipped).
Nested folders are fine — the relative path is used as the document name,
so clients\\omotec\\brief.pdf and clients\\mmga\\brief.pdf don't collide.
"""

from __future__ import annotations

import os
import sys
import time
import json
import hashlib
import argparse
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("kb_sync: `pip install requests` first.")

DEFAULT_URL = "https://lumina.mmga.agency"
ALLOWED_EXT = {".txt", ".md", ".pdf", ".docx", ".csv", ".xlsx", ".json"}
MAX_BYTES = 20 * 1024 * 1024          # 20 MB per file
STATE_FILE = ".kb_sync_state.json"
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".obsidian"}


def _token() -> str:
    tok = os.getenv("STORAGE_SYNC_TOKEN") or os.getenv("FLASK_SECRET_KEY") or ""
    if not tok:
        sys.exit("kb_sync: set STORAGE_SYNC_TOKEN (or FLASK_SECRET_KEY) in the environment.")
    return tok


def _headers(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _local_files(root: Path) -> dict:
    """{relative_posix_path: Path} for every syncable file under root."""
    out = {}
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name.startswith(".") or p.name == STATE_FILE:
            continue
        if p.suffix.lower() not in ALLOWED_EXT:
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                print(f"  skip (too big): {p}")
                continue
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        out[rel] = p
    return out


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


def _server_docs(base: str, tok: str) -> set:
    r = requests.get(f"{base}/api/storage/docs", headers=_headers(tok), timeout=30)
    r.raise_for_status()
    return {d["filename"] for d in r.json().get("docs", [])}


def _upload(base: str, tok: str, rel: str, path: Path) -> bool:
    with open(path, "rb") as fh:
        files = {"file": (path.name, fh)}
        data = {"filename": rel}
        r = requests.post(f"{base}/api/storage/doc", headers=_headers(tok),
                          files=files, data=data, timeout=120)
    if r.ok:
        j = r.json()
        print(f"  up   {rel}  ({j.get('chars', '?')} chars)")
        return True
    print(f"  FAIL {rel}  [{r.status_code}] {r.text[:200]}")
    return False


def _delete(base: str, tok: str, rel: str) -> bool:
    r = requests.delete(f"{base}/api/storage/doc", headers=_headers(tok),
                        params={"name": rel}, timeout=30)
    if r.ok:
        print(f"  del  {rel}")
        return True
    print(f"  FAIL del {rel}  [{r.status_code}] {r.text[:200]}")
    return False


def sync_once(root: Path, base: str, tok: str) -> None:
    local = _local_files(root)
    state = _load_state(root)
    try:
        server = _server_docs(base, tok)
    except Exception as e:
        print(f"kb_sync: cannot reach {base}: {e}")
        return

    new_state = {}
    uploaded = deleted = unchanged = 0

    for rel, path in sorted(local.items()):
        digest = _hash(path)
        new_state[rel] = digest
        if state.get(rel) == digest and rel in server:
            unchanged += 1
            continue
        if _upload(base, tok, rel, path):
            uploaded += 1
        else:
            new_state[rel] = state.get(rel, "")   # retry next pass

    for rel in sorted(server - set(local)):
        if _delete(base, tok, rel):
            deleted += 1

    _save_state(root, new_state)
    print(f"kb_sync: {uploaded} uploaded, {deleted} deleted, {unchanged} unchanged "
          f"({len(local)} local, {len(server)} on server)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync a folder to the Lumina knowledge store.")
    ap.add_argument("--dir", default="./knowledge", help="folder to sync (default ./knowledge)")
    ap.add_argument("--url", default=os.getenv("LUMINA_URL", DEFAULT_URL))
    ap.add_argument("--watch", action="store_true", help="keep running, re-sync on an interval")
    ap.add_argument("--interval", type=int, default=30, help="seconds between passes in --watch")
    args = ap.parse_args()

    root = Path(args.dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    base = args.url.rstrip("/")
    tok = _token()

    print(f"kb_sync: {root}  <->  {base}")
    sync_once(root, base, tok)
    if not args.watch:
        return
    print(f"kb_sync: watching every {args.interval}s (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(args.interval)
            sync_once(root, base, tok)
    except KeyboardInterrupt:
        print("\nkb_sync: stopped.")


if __name__ == "__main__":
    main()
