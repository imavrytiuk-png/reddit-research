#!/usr/bin/env python3
"""Score a gold standard: marks exported from the review page -> errors out of n per field.

Reviewers mark every row ok / bad on the page built by build_review_page.py and export
review-<title>.json = {title, exported_at, marks: {item_id: {rows: {row: ok|bad}, note}}}.
A row is a scalar field ("kind", "segment") or one list entry ("pains[0]", "entities[2]");
list rows are folded into their field, so a list field's n counts entries, not items.
A miss (something the text says and the label lacks) is not a row: it lives in the note.

Prints per reviewer: items reviewed (of the items in --results when given), rows marked,
and per field rows / bad / bad %; then the notes. With two or more marks files (owner,
colleague, model) it adds agreement per field on the rows every reviewer marked: agreed
ok, agreed bad, split. A split is a definition disagreement: resolve it in one paragraph
and write the rule into the prompt (quality-loop.md §3). No intervals: 50 items tell
which fields are broken, not the second decimal.

Usage:
  python3 gold_score.py --marks review/review-Gold-v1.json [--results out/S_grounded.ndjson] [--out out/gold_v1.json]
  python3 gold_score.py --marks owner.json colleague.json model.json
  python3 gold_score.py --selftest
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

FOLD = re.compile(r"\[\d+\]$")


def fold(row: str) -> str:
    return FOLD.sub("", row)


def load_marks(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    marks = data.get("marks", data)
    return {str(k): {"rows": dict((v or {}).get("rows") or {}), "note": ((v or {}).get("note") or "").strip()}
            for k, v in marks.items()}


def score(marks: dict) -> dict:
    per = defaultdict(lambda: {"rows": 0, "bad": 0})
    items = 0
    for _id, m in marks.items():
        if not m["rows"]:
            continue
        items += 1
        for row, mark in m["rows"].items():
            f = per[fold(row)]
            f["rows"] += 1
            if mark == "bad":
                f["bad"] += 1
    fields = {k: {**v, "bad_pct": round(100.0 * v["bad"] / v["rows"], 1) if v["rows"] else None}
              for k, v in per.items()}
    notes = [(i, m["note"]) for i, m in marks.items() if m["note"]]
    return {"items_reviewed": items, "rows": sum(v["rows"] for v in per.values()),
            "fields": fields, "notes": notes}


def agreement(all_marks: list[dict]) -> dict:
    keys = None
    for m in all_marks:
        k = {(i, r) for i, v in m.items() for r, mk in v["rows"].items() if mk in ("ok", "bad")}
        keys = k if keys is None else keys & k
    per = defaultdict(lambda: {"rows": 0, "all_ok": 0, "all_bad": 0, "split": 0})
    splits = []
    for i, r in sorted(keys or ()):
        marks = [m[i]["rows"][r] for m in all_marks]
        f = per[fold(r)]
        f["rows"] += 1
        if all(x == "ok" for x in marks):
            f["all_ok"] += 1
        elif all(x == "bad" for x in marks):
            f["all_bad"] += 1
        else:
            f["split"] += 1
            splits.append((i, r, marks))
    return {"rows_marked_by_all": len(keys or ()), "fields": dict(per), "splits": splits}


def fmt_table(fields: dict) -> str:
    if not fields:
        return "  (no rows marked)"
    w = max(len(k) for k in fields)
    lines = [f"  {'field'.ljust(w)}  rows   bad   bad%"]
    for k, v in sorted(fields.items(), key=lambda kv: (-(kv[1]["bad_pct"] or 0), kv[0])):
        pct = "-" if v["bad_pct"] is None else f"{v['bad_pct']:.0f}%"
        lines.append(f"  {k.ljust(w)}  {v['rows']:>4}  {v['bad']:>4}  {pct:>5}")
    return "\n".join(lines)


def run(marks_paths: list[Path], results: Path | None, out: Path | None, max_notes: int) -> int:
    versions, n_results = set(), 0
    if results:
        with results.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    n_results += 1
                    if rec.get("prompt_version"):
                        versions.add(rec["prompt_version"])
        print(f"results: {n_results} items, prompt version(s): {', '.join(sorted(versions)) or '-'}")
    all_marks, report = [], {"reviewers": {}, "results": str(results) if results else None,
                             "prompt_versions": sorted(versions)}
    for p in marks_paths:
        m = load_marks(p)
        s = score(m)
        all_marks.append(m)
        report["reviewers"][p.name] = {k: v for k, v in s.items() if k != "notes"} | {"notes": s["notes"]}
        of = f" (of {n_results} in results)" if n_results else ""
        print(f"\nreviewer {p.name}: {s['items_reviewed']} items reviewed{of}, {s['rows']} rows marked, {len(s['notes'])} notes")
        print(fmt_table(s["fields"]))
        for i, note in s["notes"][:max_notes]:
            print(f"  note {i}: {note}")
        if len(s["notes"]) > max_notes:
            print(f"  ... {len(s['notes']) - max_notes} more notes (--notes N)")
    if len(all_marks) >= 2:
        ag = agreement(all_marks)
        report["agreement"] = ag
        print(f"\nagreement of {len(all_marks)} reviewers on the {ag['rows_marked_by_all']} rows all of them marked:")
        if ag["fields"]:
            w = max(len(k) for k in ag["fields"])
            print(f"  {'field'.ljust(w)}  rows  all ok  all bad  split")
            for k, v in sorted(ag["fields"].items(), key=lambda kv: (-kv[1]["split"], kv[0])):
                print(f"  {k.ljust(w)}  {v['rows']:>4}  {v['all_ok']:>6}  {v['all_bad']:>7}  {v['split']:>5}")
        for i, r, marks in ag["splits"][:max_notes]:
            print(f"  split {i} {r}: {' / '.join(marks)}")
        if len(ag["splits"]) > max_notes:
            print(f"  ... {len(ag['splits']) - max_notes} more splits")
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwritten {out}")
    return 0


def selftest() -> int:
    owner = {"a": {"rows": {"kind": "ok", "segment": "bad", "pains[0]": "ok", "pains[1]": "bad"}, "note": "misses the cost pain"},
             "b": {"rows": {"kind": "ok", "segment": "ok", "pains": "ok"}, "note": ""},
             "c": {"rows": {}, "note": ""}}
    colleague = {"a": {"rows": {"kind": "ok", "segment": "ok", "pains[0]": "ok", "pains[1]": "bad"}, "note": ""},
                 "b": {"rows": {"kind": "bad", "segment": "ok"}, "note": "promo, not own"}}
    s = score(owner)
    bad = 0
    checks = [
        (s["items_reviewed"] == 2, "items reviewed = 2 (empty rows do not count)"),
        (s["fields"]["pains"]["rows"] == 3 and s["fields"]["pains"]["bad"] == 1, "list rows folded: pains 3 rows, 1 bad"),
        (s["fields"]["segment"]["bad_pct"] == 50.0, "segment 1 of 2 bad = 50%"),
        (len(s["notes"]) == 1, "one note"),
    ]
    ag = agreement([owner, colleague])
    checks += [
        (ag["rows_marked_by_all"] == 6, f"6 rows marked by both, got {ag['rows_marked_by_all']}"),
        (ag["fields"]["segment"]["split"] == 1 and ag["fields"]["kind"]["split"] == 1, "segment a and kind b are splits"),
        (ag["fields"]["pains"]["all_bad"] == 1 and ag["fields"]["pains"]["all_ok"] == 1, "pains: one agreed bad, one agreed ok"),
    ]
    for ok, label in checks:
        if not ok:
            bad += 1
            print(f"FAIL {label}: {s} {ag}")
    print("selftest:", "OK" if bad == 0 else f"{bad} failures")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--marks", type=Path, nargs="+", help="exported review JSON file(s), one per reviewer")
    ap.add_argument("--results", type=Path, default=None, help="the grounded results the page was built from (prompt version, item count)")
    ap.add_argument("--out", type=Path, default=None, help="write the report as JSON")
    ap.add_argument("--notes", type=int, default=20, help="print up to N notes and splits per list")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.marks:
        ap.error("--marks is required (or --selftest)")
    return run(a.marks, a.results, a.out, a.notes)


if __name__ == "__main__":
    sys.exit(main())
