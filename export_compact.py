#!/usr/bin/env python3
"""Write a compact, git-friendly export of a backfill result directory.

    python export_compact.py results/ out/ 2026-09-01 2026-10-10 [slug,slug,...] [since]

Reads results/backfill.sqlite3 and results/backfill_status.json and writes
out/articles.jsonl.gz (source, url, title, published_at, summary, method; no
bodies), out/status.json and out/summary.json (stories per source per day).
Rows outside the date window (Türkiye time) are left out. With a slug list,
only those publishers are exported (a parallel job exports its own group).
With since (UTC, e.g. 2026-11-04T12:00:00), only rows fetched at or after it are
exported, so a recurring run commits just its new stories.
"""
import gzip, json, sqlite3, sys
from collections import Counter
from pathlib import Path

from backfill import parse_day
from datetime import date


def main(src, out, start, end, only="", since=""):
    src, out = Path(src), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    wanted = {x.strip() for x in only.split(",") if x.strip()}
    db = sqlite3.connect(src / "backfill.sqlite3")
    per = Counter()
    n = 0
    with gzip.open(out / "articles.jsonl.gz", "wt", encoding="utf-8") as f:
        for (payload,) in db.execute("SELECT payload FROM articles WHERE fetched_at >= ? ORDER BY published_at",
                                     (since,)):
            a = json.loads(payload)
            if wanted and a.get("source") not in wanted:
                continue
            day = parse_day(a.get("published_at"))
            if not day or not (start <= day <= end):
                continue
            f.write(json.dumps({k: a.get(k) for k in ("source", "url", "title", "published_at", "summary", "method")},
                               ensure_ascii=False) + "\n")
            per[(a["source"], str(day))] += 1
            n += 1
    status = src / "backfill_status.json"
    if status.exists():
        (out / "status.json").write_text(status.read_text(encoding="utf-8"), encoding="utf-8")
    by_source = {}
    for (s, d), c in per.items():
        by_source.setdefault(s, {})[d] = c
    (out / "summary.json").write_text(json.dumps({"articles": n, "per_source_day": by_source},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Exported {n} articles", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) not in (5, 6, 7):
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
