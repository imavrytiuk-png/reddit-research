#!/usr/bin/env python3
"""Build a human review page from items + labels: every field with its quote, ok/bad
per field, a note per item, export JSON. Works from a file in any browser, no Claude.

Usage:
  python3 build_review_page.py --items items.ndjson --results out/S_grounded.ndjson \
      --ids-file state/gold_ids.txt --title "Gold v1 posts" --out review/gold_v1.html
  python3 build_review_page.py --items ... --results ... --sample 50 --seed 7 --title ... --out ...
Reviewers export `review-<title>.json` = {marks: {item_id: {rows: {field: ok|bad}, note}}}.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read_ndjson(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--ids-file", type=Path, default=None)
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--title", default="Label review")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--template", type=Path, default=HERE.parent / "assets" / "review-page.html")
    ap.add_argument("--text-key", default="text")
    ap.add_argument("--title-key", default="title")
    a = ap.parse_args()
    items = {it["id"]: it for it in read_ndjson(a.items)}
    labels = {r["id"]: (r.get("result") if "result" in r else r) for r in read_ndjson(a.results)}
    ids = [i for i in labels if i in items]
    if a.ids_file:
        wanted = [ln.strip().split("\t")[0] for ln in a.ids_file.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
        ids = [i for i in wanted if i in labels and i in items]
    elif a.sample:
        ids = random.Random(a.seed).sample(ids, min(a.sample, len(ids)))
    data = {"title": a.title, "items": []}
    for i in ids:
        it = items[i]
        meta = " · ".join(str(it[k]) for k in ("subreddit", "post_title") if it.get(k))
        data["items"].append({"id": i, "title": it.get(a.title_key) or "", "text": it.get(a.text_key) or "",
                              "meta": meta, "labels": labels[i]})
    page = a.template.read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")  # no </script> or <!-- can come from the data
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(page.replace("/*DATA*/", blob), encoding="utf-8")
    print(f"{a.out}: {len(data['items'])} items", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
