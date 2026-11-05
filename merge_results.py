#!/usr/bin/env python3
"""Merge backfill parts (one directory per parallel job) into one result set.

    python merge_results.py parts/ results/

Each part directory may hold backfill.sqlite3 and backfill_status.json. The
output directory receives one backfill.sqlite3 (articles deduplicated by
source and URL; checked-URL bookkeeping kept so the merged database can resume
a later run), backfill.jsonl exported from it, and backfill_status.json with
every publisher's status.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SCHEMA = '''CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY, source TEXT, url TEXT, title TEXT, published_at TEXT,
    fetched_at TEXT, payload TEXT NOT NULL, UNIQUE(source, url));
CREATE TABLE IF NOT EXISTS backfill_checked (
    source TEXT, url TEXT, start TEXT, end TEXT, PRIMARY KEY(source, url, start, end));'''


def merge(parts: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    target = out / "backfill.sqlite3"
    db = sqlite3.connect(target)
    db.executescript(SCHEMA)
    statuses, checked_at = {}, None
    for part in sorted(p for p in parts.rglob("backfill.sqlite3") if p.resolve() != target.resolve()):
        src = sqlite3.connect(part)
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "articles" in tables:
            with db:
                db.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?)",
                               src.execute("SELECT id, source, url, title, published_at, fetched_at, payload FROM articles"))
        if "backfill_checked" in tables:
            with db:
                db.executemany("INSERT OR IGNORE INTO backfill_checked VALUES (?,?,?,?)",
                               src.execute("SELECT source, url, start, end FROM backfill_checked"))
        src.close()
    for path in sorted(parts.rglob("backfill_status.json")):
        if path.resolve() == (out / "backfill_status.json").resolve():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        checked_at = max(filter(None, [checked_at, data.get("checked_at")]), default=None)
        for s in data.get("sources", []):
            prev = statuses.get(s["source"])
            if prev is None or s.get("checked_at", "") >= prev.get("checked_at", ""):
                statuses[s["source"]] = s
    counts = dict(db.execute("SELECT source, COUNT(*) FROM articles GROUP BY source"))
    for slug, s in statuses.items():
        s["stored_total"] = counts.get(slug, 0)
    with (out / "backfill.jsonl").open("w", encoding="utf-8") as f:
        for (payload,) in db.execute("SELECT payload FROM articles ORDER BY published_at, source"):
            f.write(payload + "\n")
    (out / "backfill_status.json").write_text(json.dumps(
        {"checked_at": checked_at, "sources": sorted(statuses.values(), key=lambda s: s["source"])},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(counts.values())
    db.close()
    return {"articles": total, "sources": len(statuses)}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    result = merge(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"Merged {result['articles']} articles from {result['sources']} publishers", file=sys.stderr)
