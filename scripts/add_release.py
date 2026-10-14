"""Add a "what's new" entry to config/releases.json.

Every time a new feature is built, run this (or edit the file by hand), commit and
push. After the deploy, Railway announces it: a short WhatsApp to every active
employee and a popup in Lumina the next time each person opens it
(backend/releases.py).

    python scripts/add_release.py "Title" "First point" "Second point" [--silent]

Write for the team, not for developers: plain words, what they can now do, 2 to 5
points, no jargon. --silent = popup only, no WhatsApp.
"""
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

FILE = os.path.join(os.path.dirname(__file__), "..", "config", "releases.json")


def main(argv):
    silent = "--silent" in argv
    args = [a for a in argv if a != "--silent"]
    if len(args) < 2:
        print(__doc__)
        return 2
    title, bullets = args[0].strip(), [b.strip() for b in args[1:] if b.strip()]
    ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    slug = ist.strftime("%Y-%m-%d") + "-" + (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "update")
    try:
        with io.open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = []
    if any(e.get("id") == slug for e in data):
        slug += "-2"
    entry = {"id": slug, "date": ist.strftime("%Y-%m-%d"), "title": title, "bullets": bullets}
    if silent:
        entry["notify"] = False
    data.append(entry)
    with io.open(FILE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Added {slug} ({len(bullets)} points). Commit and push; it is announced after the deploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
