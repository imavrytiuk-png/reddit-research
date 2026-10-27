#!/usr/bin/env python3
"""Compare two labelling runs on the same items: two models, or two prompt versions.

Inputs: two grounded result files (evidence_check.py output, {"id","result":{...}}) and
the items file (for "slice" and the title). The fields are read from the results, so the
script works for any schema.

Prints, for every field except id and *_evidence:
- scalar fields: agreement share over items both runs labelled, and how often one run
  has a value where the other has null / "unknown";
- list fields (entities, pains, tools…): exact-set agreement after lowercasing the names
  (--name-keys says which key holds the name, first match wins), mean Jaccard, and the
  number of names only A or only B produced;
- per slice: the kind distribution of A and B side by side.
Writes a Markdown disagreement list (id, field, A, B, title) with --out.

Differences of a few items out of 50 are inside the noise of one run against itself;
pick a model on the error types (fabricated names, own vs not-own) and on cost, not on a
two-point difference in agreement.

Usage:
  python3 compare_runs.py --a out/S-v1_grounded.ndjson --b out/H-v1_grounded.ndjson \
      --items items/pilot.ndjson --out out/compare_S_H.md [--name-keys name,pain] [--max-list 80]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from _common import read_ndjson

NULLS = {None, "", "unknown"}


def names_of(lst, name_keys: list[str]) -> set[str]:
    out = set()
    for e in lst or []:
        if isinstance(e, dict):
            for k in name_keys:
                if e.get(k):
                    out.add(str(e[k]).strip().lower())
                    break
        elif e is not None:
            out.add(str(e).strip().lower())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", type=Path, required=True)
    ap.add_argument("--b", type=Path, required=True)
    ap.add_argument("--items", type=Path, required=True)
    ap.add_argument("--name-keys", default="name,pain,canonical", help="keys that hold a list entry's name, in order")
    ap.add_argument("--kind-key", default="kind")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--max-list", type=int, default=80, help="disagreements listed per field in --out")
    a = ap.parse_args()
    name_keys = [k for k in a.name_keys.split(",") if k]
    items = {it["id"]: it for it in read_ndjson(a.items)}
    A = {r["id"]: (r.get("result") or {}) for r in read_ndjson(a.a)}
    B = {r["id"]: (r.get("result") or {}) for r in read_ndjson(a.b)}
    common = sorted(set(A) & set(B) & set(items))
    print(f"A={a.a.name}: {len(A)} results; B={a.b.name}: {len(B)}; both labelled {len(common)} of {len(items)} items")
    if not common:
        sys.exit("no common items")
    fields = sorted({k for i in common for k in list(A[i]) + list(B[i]) if k != "id" and not k.endswith("_evidence")})
    disagreements: dict[str, list[tuple]] = defaultdict(list)
    print(f"\n{'field':<22} {'type':<6} {'agree':>6} {'n':>4}  detail")
    for f in fields:
        va = [A[i].get(f) for i in common]
        vb = [B[i].get(f) for i in common]
        if any(isinstance(v, list) for v in va + vb):
            same = jac = 0.0
            only_a = only_b = 0
            for i, x, y in zip(common, va, vb):
                sx, sy = names_of(x, name_keys), names_of(y, name_keys)
                if sx == sy:
                    same += 1
                else:
                    disagreements[f].append((i, sorted(sx), sorted(sy)))
                u = sx | sy
                jac += (len(sx & sy) / len(u)) if u else 1.0
                only_a += len(sx - sy)
                only_b += len(sy - sx)
            print(f"{f:<22} {'list':<6} {100 * same / len(common):>5.0f}% {len(common):>4}  mean Jaccard {jac / len(common):.2f}; "
                  f"names only in A {only_a}, only in B {only_b}")
        else:
            same = sum(1 for x, y in zip(va, vb) if x == y)
            a_only = sum(1 for x, y in zip(va, vb) if x not in NULLS and y in NULLS)
            b_only = sum(1 for x, y in zip(va, vb) if y not in NULLS and x in NULLS)
            for i, x, y in zip(common, va, vb):
                if x != y:
                    disagreements[f].append((i, x, y))
            print(f"{f:<22} {'scalar':<6} {100 * same / len(common):>5.0f}% {len(common):>4}  "
                  f"value in A only {a_only}, in B only {b_only}")
    slices = sorted({items[i].get("slice") or "(all)" for i in common})
    print(f"\n{a.kind_key} by slice (A / B):")
    for s in slices:
        ids = [i for i in common if (items[i].get("slice") or "(all)") == s]
        ca = Counter(A[i].get(a.kind_key) for i in ids)
        cb = Counter(B[i].get(a.kind_key) for i in ids)
        keys = sorted(set(ca) | set(cb), key=lambda k: -(ca[k] + cb[k]))
        print(f"  {s:<14} n={len(ids):<4} " + "  ".join(f"{k}: {ca[k]}/{cb[k]}" for k in keys))
    if a.out:
        lines = [f"# Disagreements: A = {a.a.name}, B = {a.b.name}", "", f"{len(common)} items labelled by both.", ""]
        for f in fields:
            d = disagreements.get(f) or []
            if not d:
                continue
            lines += [f"## {f} ({len(d)} items)", "", "| id | A | B | title |", "|---|---|---|---|"]
            for i, x, y in d[: a.max_list]:
                t = (items[i].get("title") or items[i].get("post_title") or "")[:80].replace("|", "/")
                lines.append(f"| {i} | {x} | {y} | {t} |")
            if len(d) > a.max_list:
                lines.append(f"| … | {len(d) - a.max_list} more | | |")
            lines.append("")
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text("\n".join(lines))
        print(f"\nwritten {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
