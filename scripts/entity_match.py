#!/usr/bin/env python3
"""Dictionary matcher: find known entities (places, products, brands...) in items by
alias, before any model call. Output = hints for the labelling prompt and a recall
check on the model's output.

Dictionary CSV with a header `alias,canonical,kind` (templates/entity_dict.example.csv):
one row per alias; the canonical name is also an alias of itself. Matching is case-
insensitive on word boundaries, longest alias first. Aliases in --common-words (names
that are also ordinary words: "Nice", "Mobile", "Reading") must appear capitalised in
the text; aliases in --exclude are ignored. Put diacritic variants in the dictionary
("München" and "Munchen"); the text is not folded.

Usage:
  python3 entity_match.py --dict entity_dict.csv --items items.ndjson --out hints.ndjson
  python3 entity_match.py ... --common-words common.txt --exclude exclude.txt --min-len 3
  python3 entity_match.py ... --inline --out-items items_with_hints.ndjson
Prints: items with >= 1 match, top canonical entities by items, and the aliases that
matched most (to spot false positives for the exclude list).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load_dict(path: Path, min_len: int) -> tuple[dict[str, tuple[str, str]], list[str]]:
    alias_map: dict[str, tuple[str, str]] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            alias = (row.get("alias") or "").strip()
            canonical = (row.get("canonical") or alias).strip()
            kind = (row.get("kind") or "").strip()
            if len(alias) < min_len or alias.startswith("<"):
                continue
            alias_map.setdefault(alias.lower(), (canonical, kind))
            alias_map.setdefault(canonical.lower(), (canonical, kind))
    aliases = sorted(alias_map, key=len, reverse=True)
    return alias_map, aliases


def read_words(path: Path | None) -> set[str]:
    if not path:
        return set()
    return {ln.strip().lower() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dict", type=Path, required=True)
    ap.add_argument("--items", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="hints NDJSON: {id, hints:[{canonical, kind, alias, n}]}")
    ap.add_argument("--text-keys", default="title,text")
    ap.add_argument("--common-words", type=Path, default=None, help="aliases that need a capital letter")
    ap.add_argument("--exclude", type=Path, default=None, help="aliases to ignore")
    ap.add_argument("--min-len", type=int, default=3)
    ap.add_argument("--inline", action="store_true", help="also write items with a hints list")
    ap.add_argument("--out-items", type=Path, default=None)
    a = ap.parse_args()

    alias_map, aliases = load_dict(a.dict, a.min_len)
    common, exclude = read_words(a.common_words), read_words(a.exclude)
    aliases = [x for x in aliases if x not in exclude]
    if not aliases:
        print("empty dictionary", file=sys.stderr)
        return 1
    pattern = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(x) for x in aliases) + r")(?![\w])", re.IGNORECASE)
    text_keys = [k for k in a.text_keys.split(",") if k]
    n_items = n_hit = 0
    by_canonical, by_alias = Counter(), Counter()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    fout_items = None
    if a.inline:
        out_items = a.out_items or a.items.with_name(a.items.stem + "_hints.ndjson")
        fout_items = out_items.open("w", encoding="utf-8")
    with a.items.open("r", encoding="utf-8") as fin, a.out.open("w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            it = json.loads(line)
            n_items += 1
            text = "\n".join(str(it.get(k) or "") for k in text_keys)
            found: dict[str, dict] = {}
            for m in pattern.finditer(text):
                raw = m.group(0)
                key = raw.lower()
                if key in common and not raw[0].isupper():
                    continue
                canonical, kind = alias_map[key]
                d = found.setdefault(canonical, {"canonical": canonical, "kind": kind, "alias": raw, "n": 0})
                d["n"] += 1
            hints = sorted(found.values(), key=lambda d: -d["n"])
            if hints:
                n_hit += 1
                for h in hints:
                    by_canonical[h["canonical"]] += 1
                    by_alias[h["alias"].lower()] += 1
            fout.write(json.dumps({"id": it["id"], "hints": hints}, ensure_ascii=False) + "\n")
            if fout_items:
                it["hints"] = [h["canonical"] for h in hints]
                fout_items.write(json.dumps(it, ensure_ascii=False) + "\n")
    if fout_items:
        fout_items.close()
    print(f"items={n_items:,d} with_match={n_hit:,d} ({100 * n_hit / max(n_items, 1):.0f}%) aliases={len(aliases):,d}", file=sys.stderr)
    print("top entities (items):", ", ".join(f"{k} {v}" for k, v in by_canonical.most_common(30)), file=sys.stderr)
    print("top aliases (check for false positives):", ", ".join(f"{k} {v}" for k, v in by_alias.most_common(30)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
