#!/usr/bin/env python3
"""Person-level aggregation of grounded labels into segment x entity matrices.

Counting rules:
- Unit = person (author_hash from the items). One person = one vote per entity per group.
- A person's segment = the most frequent stated segment over their own items; "unknown"
  when none is stated. Never from the subreddit or the thread.
- Groups, at least one of the two (the scheme does not guess which values count):
  * entity statuses: --groups considering,using  → one matrix per status, from own-case
    rows only (--kind-key / --own-values);
  * whole lists without a status: --extra-group pain=pains@pain → one matrix per list;
    the key after "@" holds the name (default --name-key); an optional :filter_key=value
    keeps matching entries only. Extra groups count every row (advice from non-own posts
    is the usual reason); --extra-own-only restricts them to own-case rows (pains and
    practices of the author).
- Entities with an excluded flag (--exclude-flag <key>) are skipped.
- The base: a person enters the per-segment base only through an own-case row (or, when an
  every-row extra group exists, through any row). People the prefilter kept whose rows are
  all general / promo / other_person stay outside the base and are counted separately
  ("people_outside_base"). The same rules define the population recall_check.py measures
  the prefilter against (is_own, counted_names below).
- Shares are within segment: people of the segment who named the entity / people of the
  segment. Segments are never pooled.
- Weights: items carry "weight" from export_items.py (population / drawn). A person's
  weight is the smallest weight among their items (the slice that most surely included
  them). Shares use weights; the thresholds use plain people counts. With every weight at
  1.0 nothing changes; the summary says whether weights were in play.
- Matrix rows: segments with >= --row-min people, columns: top --top entities by the mean
  of per-segment shares (an ordering device, never shown). Cells with < --cell-min people
  are empty; --fill-min marks cells solid enough for colour. "top3" = sum of the row's
  three largest shares among cells >= cell-min.
- Sensitivity: fractional votes (1/k per person naming k entities) re-rank the columns;
  the report says whether the order changes.
- Dynamics (first half of the window against the second) is not computed: a year split
  in two is weak evidence and seasonality confounds it (statistics.md).

Inputs: results NDJSON from evidence_check.py ({"id","result":{...}}) and items NDJSON
with "id", "author_hash" [, "weight"]. Several files of each are allowed (posts + comments).

Usage:
  python3 aggregate_matrix.py --results out/P_grounded.ndjson --items items/posts.ndjson \
      --out report_data --groups considering,using --exclude-flag is_comparison
  python3 aggregate_matrix.py --results out/P_grounded.ndjson --items items/posts.ndjson \
      --out report_data --extra-group tool=tools --extra-group pain=pains@pain --extra-own-only
  python3 aggregate_matrix.py --selftest
Writes report_data/summary.json, matrix_<group>_<level>.{json,md,csv}, cards_<group>.json
and prints the markdown matrices.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from _common import read_ndjson


def parse_extra(spec: str):
    """name=list_key[@name_key][:filter_key=value]"""
    name, _, rest = spec.partition("=")
    list_part, _, flt = rest.partition(":")
    list_key, _, name_key = list_part.partition("@")
    fk = fv = None
    if flt:
        fk, _, fv = flt.partition("=")
    if not name or not list_key:
        raise SystemExit(f"bad --extra-group {spec!r}: expected name=list_key[@name_key][:filter_key=value]")
    return name, list_key, (name_key or None), fk, fv


def is_own(r: dict, kind_key: str, own_values: set) -> bool:
    """Own-case row: the kind is one of --own-values (or the schema has no kind at all)."""
    return (kind_key not in r) or (r.get(kind_key) in own_values)


def counted_names(r: dict, a: argparse.Namespace) -> list[tuple[str, str, str | None]]:
    """(group, name, sub) of every item in this result that enters a counted group under the
    aggregator's rules: an entity whose status is in --groups (own rows only), or an entry of an
    --extra-group list (every row, or own rows only with --extra-own-only). Shared with
    recall_check.py so the prefilter's recall is measured against the same population."""
    own = is_own(r, a.kind_key, set(a.own_values.split(",")))
    status_groups = [g for g in (a.groups or "").split(",") if g]
    excl = list(a.exclude_flag or [])
    out: list[tuple[str, str, str | None]] = []
    if own:
        for e in r.get(a.entity_key) or []:
            if isinstance(e, dict) and not any(e.get(fl) for fl in excl):
                if e.get(a.status_key) in status_groups and e.get(a.name_key):
                    out.append((e[a.status_key], e[a.name_key], e.get(a.sub_key)))
    if own or not a.extra_own_only:
        for gname, list_key, nk, fk, fv in [parse_extra(x) for x in (a.extra_group or [])]:
            for e in r.get(list_key) or []:
                if not isinstance(e, dict) or (fk and str(e.get(fk)) != fv) or any(e.get(fl) for fl in excl):
                    continue
                name = e.get(nk or a.name_key)
                if name:
                    out.append((gname, name, e.get(a.sub_key)))
    return out


def aggregate(a: argparse.Namespace) -> dict:
    own_values = set(a.own_values.split(","))
    status_groups = [g for g in (a.groups or "").split(",") if g]
    extras = [parse_extra(s) for s in a.extra_group]
    if not status_groups and not extras:
        raise SystemExit("nothing to count: name the status values (--groups considering,using) or a list "
                         "(--extra-group pain=pains@pain). The scheme does not guess which values count.")

    people: dict[str, tuple[str, float]] = {}
    for p in a.items:
        for it in read_ndjson(p):
            if it.get("author_hash"):
                people[it["id"]] = (it["author_hash"], float(it.get("weight") or 1.0))
    seg_votes: dict[str, Counter] = defaultdict(Counter)
    persons: set[str] = set()
    pweight: dict[str, float] = {}
    names: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))      # group -> person -> {name}
    subs: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))       # group -> person -> {(name, sub)}
    n_rows = n_skipped = 0
    outside: set[str] = set()
    every_row = bool(extras) and not a.extra_own_only
    for p in a.results:
        for row in read_ndjson(p):
            ph = people.get(row["id"])
            if not ph:
                n_skipped += 1
                continue
            person, w = ph
            n_rows += 1
            r = row.get("result") or {}
            own = is_own(r, a.kind_key, own_values)
            if not (own or every_row):
                outside.add(person)  # kept by the prefilter, but no own-case post: not in any base
                continue
            persons.add(person)
            pweight[person] = min(pweight.get(person, w), w)
            if own:
                seg = r.get(a.segment_key)
                if seg and seg != a.unknown:
                    seg_votes[person][seg] += 1
            for g, name, sub in counted_names(r, a):
                names[g][person].add(name)
                if sub:
                    subs[g][person].add((name, sub))
    outside -= persons
    if n_skipped:
        print(f"warning: {n_skipped} result rows without author_hash were skipped", file=sys.stderr)
    segment_of = {p: (seg_votes[p].most_common(1)[0][0] if seg_votes.get(p) else a.unknown) for p in persons}
    base = Counter(segment_of.values())
    base_w: dict[str, float] = defaultdict(float)
    for p in persons:
        base_w[segment_of[p]] += pweight[p]
    weighted = any(abs(w - 1.0) > 1e-9 for w in pweight.values())
    rows = [s for s, n in sorted(base.items(), key=lambda x: (x[0] == a.unknown, -x[1])) if n >= a.row_min]
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
    summary = {"people": len(persons), "people_outside_base": len(outside), "people_weighted": round(sum(pweight.values()), 1),
               "weighted": weighted, "result_rows": n_rows, "segments": dict(base), "rows_shown": rows,
               "thresholds": {"row_min": a.row_min, "cell_min": a.cell_min, "fill_min": a.fill_min, "top": a.top},
               "groups": {}}
    all_groups = status_groups + [g[0] for g in extras]
    matrices: dict[tuple[str, str], dict] = {}

    def share(wsum, s, e):
        return wsum[s][e] / base_w[s] if base_w[s] else 0.0

    for g in all_groups:
        summary["groups"][g] = {"people": len(names[g])}
        for level, store in (("entity", names[g]), ("sub", subs[g])):
            counts: dict[str, Counter] = {s: Counter() for s in base}      # segment -> entity -> people
            wsum: dict[str, Counter] = {s: Counter() for s in base}        # weighted people
            frac: dict[str, Counter] = {s: Counter() for s in base}
            for person, ents in store.items():
                s, k, w = segment_of[person], len(ents), pweight[person]
                for e in ents:
                    key = e if level == "entity" else f"{e[0]} · {e[1]}"
                    counts[s][key] += 1
                    wsum[s][key] += w
                    frac[s][key] += w / k
            if not rows or not any(counts[s] for s in rows):
                continue
            entities = set().union(*(counts[s].keys() for s in rows))
            mean_share = {e: sum(share(wsum, s, e) for s in rows) / len(rows) for e in entities}
            mean_frac = {e: sum(share(frac, s, e) for s in rows) / len(rows) for e in entities}
            cols = sorted(entities, key=lambda e: (-mean_share[e], -sum(counts[s][e] for s in rows), e))[:a.top]
            cols_frac = sorted(entities, key=lambda e: (-mean_frac[e], e))[:a.top]
            matrix = {"group": g, "level": level, "columns": cols, "weighted": weighted, "rows": []}
            for s in rows:
                cells = []
                for e in cols:
                    n = counts[s][e]
                    cells.append({"entity": e, "n": n, "share": round(100 * share(wsum, s, e), 1) if n >= a.cell_min else None,
                                  "fill": n >= a.fill_min})
                shares_all = sorted((100 * share(wsum, s, e) for e, n in counts[s].items() if n >= a.cell_min), reverse=True)
                matrix["rows"].append({"segment": s, "people": base[s], "people_weighted": round(base_w[s], 1),
                                       "named_any": sum(1 for p, e in store.items() if segment_of[p] == s and e),
                                       "top3": round(sum(shares_all[:3]), 1), "cells": cells})
            matrix["sensitivity"] = {"fractional_order": cols_frac, "order_changes": cols != cols_frac}
            matrices[(g, level)] = matrix
            md = [f"### {g} · {level}  (share of the segment's people naming the entity; empty < {a.cell_min} people, "
                  f"* = under {a.fill_min}" + ("; weighted by sampling weight" if weighted else "") + ")",
                  "| segment | people | top-3 | " + " | ".join(cols) + " |", "|---|---:|---:|" + "---:|" * len(cols)]
            for r in matrix["rows"]:
                md.append(f"| {r['segment']} | {r['people']} | {r['top3']}% | " + " | ".join(
                    "" if c["share"] is None else f"{c['share']}%{'' if c['fill'] else '*'}" for c in r["cells"]) + " |")
            if matrix["sensitivity"]["order_changes"]:
                md.append(f"\nFractional votes change the column order: {', '.join(cols_frac)}")
            if a.out:
                stem = a.out / f"matrix_{g}_{level}"
                stem.with_suffix(".json").write_text(json.dumps(matrix, ensure_ascii=False, indent=1))
                with stem.with_suffix(".csv").open("w", encoding="utf-8", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["segment", "people", "top3"] + cols)
                    for r in matrix["rows"]:
                        w.writerow([r["segment"], r["people"], r["top3"]] + [("" if c["share"] is None else c["share"]) for c in r["cells"]])
                stem.with_suffix(".md").write_text("\n".join(md) + "\n")
                print("\n".join(md) + "\n")
        totals = Counter()
        for person, ents in names[g].items():
            for e in ents:
                totals[e] += 1
        cards = []
        for e, n in totals.most_common(a.cards):
            seg_n = Counter(segment_of[p] for p, ents in names[g].items() if e in ents)
            seg_w: dict[str, float] = defaultdict(float)
            for p, ents in names[g].items():
                if e in ents:
                    seg_w[segment_of[p]] += pweight[p]
            sub_counts = Counter(sb for p, ents in subs[g].items() for (nm, sb) in ents if nm == e)
            cards.append({"entity": e, "people": n,
                          "segments": [{"segment": s, "n": seg_n[s], "share": round(100 * seg_w[s] / base_w[s], 1)}
                                       for s in rows if seg_n[s] >= a.cell_min],
                          "subs": [{"sub": sb, "n": k} for sb, k in sub_counts.most_common(15) if k >= a.cell_min]})
        if a.out:
            (a.out / f"cards_{g}.json").write_text(json.dumps(cards, ensure_ascii=False, indent=1))
    if a.out:
        (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"people in base={len(persons):,d} (kept by the prefilter but without an own-case post: {len(outside):,d}) "
          f"rows={n_rows:,d} segments={dict(base)} shown={rows}"
          + (f" weighted (sum of weights {summary['people_weighted']:,})" if weighted else ""), file=sys.stderr)
    for g in all_groups:
        print(f"  group {g}: {summary['groups'][g]['people']:,d} people", file=sys.stderr)
    return {"summary": summary, "matrices": matrices, "segment_of": segment_of}


def selftest() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aggregate_selftest_"))
    items = [{"id": f"i{k}", "author_hash": f"p{k}", "weight": (3.0 if k == 3 else 1.0)} for k in range(1, 8)]
    results = [
        {"id": "i1", "result": {"kind": "own", "segment": "A", "entities": [{"name": "X", "status": "using"}], "pains": [{"pain": "slow"}]}},
        {"id": "i2", "result": {"kind": "own", "segment": "A", "entities": [{"name": "Y", "status": "using"}], "pains": [{"pain": "slow"}]}},
        {"id": "i3", "result": {"kind": "own", "segment": "A", "entities": [{"name": "X", "status": "using"}, {"name": "Y", "status": "using"},
                                                                          {"name": "Z", "status": "considering"}], "pains": []}},
        {"id": "i4", "result": {"kind": "own", "segment": "B", "entities": [{"name": "X", "status": "using"}], "pains": [{"pain": "slow"}]}},
        {"id": "i5", "result": {"kind": "own", "segment": "B", "entities": [{"name": "Z", "status": "considering"}], "pains": []}},
        {"id": "i6", "result": {"kind": "own", "segment": "unknown", "entities": [], "pains": []}},
        {"id": "i7", "result": {"kind": "general", "segment": "B", "entities": [{"name": "X", "status": "using"}], "pains": [{"pain": "noise"}]}},
    ]
    (tmp / "items.ndjson").write_text("".join(json.dumps(x) + "\n" for x in items))
    (tmp / "results.ndjson").write_text("".join(json.dumps(x) + "\n" for x in results))
    base = dict(results=[tmp / "results.ndjson"], items=[tmp / "items.ndjson"], out=None, entity_key="entities", name_key="name",
                sub_key="sub", status_key="status", segment_key="segment", kind_key="kind", own_values="own", unknown="unknown",
                groups="using", extra_group=["pain=pains@pain"], extra_own_only=True, exclude_flag=[], row_min=2, cell_min=1,
                fill_min=2, top=10, cards=30)
    r = aggregate(argparse.Namespace(**base))
    m = r["matrices"][("using", "entity")]
    rowA = next(x for x in m["rows"] if x["segment"] == "A")
    rowB = next(x for x in m["rows"] if x["segment"] == "B")
    cell = lambda row, e: next(c for c in row["cells"] if c["entity"] == e)
    assert r["summary"]["weighted"] and r["summary"]["segments"] == {"A": 3, "B": 2, "unknown": 1}, r["summary"]  # i7 is not own: outside the base
    assert r["summary"]["people_outside_base"] == 1, r["summary"]
    assert cell(rowA, "X")["share"] == 80.0 and cell(rowA, "X")["n"] == 2, rowA          # (1 + 3) / (1 + 1 + 3)
    assert cell(rowB, "X")["share"] == 50.0 and cell(rowB, "X")["n"] == 1, rowB          # i7 is not own: ignored
    assert "Z" not in m["columns"], m["columns"]                                        # considering is not a group here
    p = r["matrices"][("pain", "entity")]
    assert cell(next(x for x in p["rows"] if x["segment"] == "A"), "slow")["share"] == 40.0   # 2 / 5
    assert "noise" not in p["columns"]                                                  # extra group, own only
    base["extra_own_only"] = False
    r2 = aggregate(argparse.Namespace(**base))
    assert "noise" in r2["matrices"][("pain", "entity")]["columns"]                     # every row counts (i7, segment unknown)
    assert r2["summary"]["segments"]["unknown"] == 2 and r2["summary"]["people_outside_base"] == 0, r2["summary"]
    assert counted_names(results[2]["result"], argparse.Namespace(**base)) == [("using", "X", None), ("using", "Y", None)]
    assert counted_names(results[6]["result"], argparse.Namespace(**{**base, "extra_own_only": True})) == []
    for it in items:
        it["weight"] = 1.0
    (tmp / "items.ndjson").write_text("".join(json.dumps(x) + "\n" for x in items))
    r3 = aggregate(argparse.Namespace(**base))
    assert not r3["summary"]["weighted"]
    assert cell(next(x for x in r3["matrices"][("using", "entity")]["rows"] if x["segment"] == "A"), "X")["share"] == 66.7
    try:
        aggregate(argparse.Namespace(**{**base, "groups": None, "extra_group": []}))
        raise AssertionError("no groups must refuse")
    except SystemExit as e:
        assert "nothing to count" in str(e)
    print("selftest ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, nargs="+")
    ap.add_argument("--items", type=Path, nargs="+")
    ap.add_argument("--out", type=Path, default=Path("report_data"))
    ap.add_argument("--entity-key", default="entities")
    ap.add_argument("--name-key", default="name")
    ap.add_argument("--sub-key", default="sub")
    ap.add_argument("--status-key", default="status")
    ap.add_argument("--segment-key", default="segment")
    ap.add_argument("--kind-key", default="kind")
    ap.add_argument("--own-values", default="own")
    ap.add_argument("--unknown", default="unknown")
    ap.add_argument("--groups", default=None, help="status values to count, comma-separated (one matrix each)")
    ap.add_argument("--extra-group", action="append", default=[], help="name=list_key[@name_key][:filter_key=value]")
    ap.add_argument("--extra-own-only", action="store_true", help="extra groups from own-case rows only")
    ap.add_argument("--exclude-flag", action="append", default=[])
    ap.add_argument("--row-min", type=int, default=30)
    ap.add_argument("--cell-min", type=int, default=10)
    ap.add_argument("--fill-min", type=int, default=30)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--cards", type=int, default=30, help="entities with cards per group")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.results or not a.items:
        ap.error("--results and --items are required")
    aggregate(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
