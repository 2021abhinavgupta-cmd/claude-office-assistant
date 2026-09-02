#!/usr/bin/env python3
"""
research_feed.py — unattended web research that feeds the knowledge base.

Give it a list of URLs (competitor blogs, news pages, a client's site, a
pricing page…). Each run it fetches them, strips the HTML to plain text, and
writes a dated Markdown snapshot into your knowledge folder. kb_sync.py then
uploads those snapshots, so the WhatsApp agent and in-app chat can answer
"what has <competitor> posted lately?" or "what changed on <page>?".

Only depends on the standard library + `requests`.

Setup:
    Put one source per line in <knowledge-dir>/research_sources.txt :
        Acme blog        | https://acme.example/blog
        Industry news    | https://news.example/topic/marketing
    (the label is optional; "label | url" or just "url")

Run:
    python scripts/research_feed.py --dir "C:\\Users\\me\\LuminaKnowledge"
    python scripts/research_feed.py --dir ./knowledge --watch --interval 3600

Snapshots land in <dir>/research/<slug>.md and are overwritten each run
(always "latest"), so the knowledge base never accumulates stale copies.
"""

from __future__ import annotations

import re
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    sys.exit("research_feed: `pip install requests` first.")

SOURCES_FILE = "research_sources.txt"
OUT_SUBDIR = "research"
MAX_CHARS = 8000
UA = "Mozilla/5.0 (compatible; LuminaResearchFeed/1.0)"
_DROP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            t = data.strip()
            if t:
                self.parts.append(t)


def _html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    return re.sub(r"\n{3,}", "\n\n", " ".join(p.parts)).strip()


def _slug(label: str, url: str) -> str:
    base = label or urlparse(url).netloc or "source"
    s = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return s or "source"


def _parse_sources(path: Path) -> list[tuple[str, str]]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            label, url = (x.strip() for x in line.split("|", 1))
        else:
            label, url = "", line
        if url.startswith(("http://", "https://")):
            out.append((label, url))
    return out


def _fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    if "html" not in ctype and "text" not in ctype:
        return ""
    return _html_to_text(r.text)[:MAX_CHARS]


def run_once(root: Path) -> None:
    sources = _parse_sources(root / SOURCES_FILE)
    if not sources:
        print(f"research_feed: no sources — create {root / SOURCES_FILE}")
        return
    outdir = root / OUT_SUBDIR
    outdir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ok = 0
    for label, url in sources:
        try:
            text = _fetch(url)
        except Exception as e:
            print(f"  FAIL {url}  {e}")
            continue
        if not text:
            print(f"  skip (no text) {url}")
            continue
        md = f"# {label or urlparse(url).netloc}\n\nSource: {url}\nFetched: {now}\n\n{text}\n"
        (outdir / f"{_slug(label, url)}.md").write_text(md, "utf-8")
        print(f"  saved {_slug(label, url)}.md  ({len(text)} chars)  <- {url}")
        ok += 1
    print(f"research_feed: {ok}/{len(sources)} sources updated in {outdir}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch web sources into the knowledge folder.")
    ap.add_argument("--dir", default="./knowledge", help="knowledge folder (same as kb_sync)")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=3600, help="seconds between passes in --watch")
    args = ap.parse_args()

    root = Path(args.dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    print(f"research_feed: {root}")
    run_once(root)
    if not args.watch:
        return
    print(f"research_feed: watching every {args.interval}s (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(args.interval)
            run_once(root)
    except KeyboardInterrupt:
        print("\nresearch_feed: stopped.")


if __name__ == "__main__":
    main()
