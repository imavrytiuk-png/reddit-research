#!/usr/bin/env python3
"""Recall of the prefilter against the population the matrix describes, measured on a
labelled uniform control.

The prefilter (phrases, or whole subs) defines who is in the study; every share in the matrix
means "among people whose posts it kept". This check labels a uniform random sample of the
corpus (the control slice) with the same prompt and asks how much of the matrix's own
population the prefilter would have caught. Two figures, both derived from the aggregator's
counting rules (the same flags as aggregate_matrix.py), so the denominator is never chosen
by hand:

- base recall: T_base = own-case texts (kind in --own-values): the matrix's per-segment base;
- cell recall: T_cell = own-case texts with at least one counted item (an entity whose status
  is in --groups, or an entry of an --extra-group list): the texts that fill cells.

Both are "as labelled by model M": label the control with the model chosen for the full run
and the final prompt; a weak model inflates both denominators. The script lists the missed
T_cell texts: read them and sort them into "truly the behaviour" (widen the phrases, or take
that sub whole: posts inside a product's own sub rarely name the product) and "labeller
error" (an example for the next prompt version).

Frozen before the control is labelled, and printed with the result: the prompt version
(read from the results), the regex file(s), these flags. A change after the number is seen
is a new prompt version and a new control.

Sizing: about 30 caught T_cell texts are needed for a usable interval:
n ≈ 30 / (T_cell share × expected recall); the script says how many more it needs.

Usage:
  python3 recall_check.py --items items/control.ndjson --results out/S-ctl_grounded.ndjson \\
      --regex-file state/prefilter_app.txt state/prefilter_manual.txt --slice uniform \\
      --extra-group practice=practices --extra-group pain=pains@pain --extra-own-only \\
      --whole-subs ynab,MonarchMoney --population 115542 --prefilter-count 6403 --out out/recall.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

from _common import read_ndjson, read_regex_files
from aggregate_matrix import counted_names, is_own


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def ci(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{pct(k / n) if n else '-'} (95% {pct(lo)}–{pct(hi)})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--regex", default=None, help="the prefilter as a regex (case-insensitive)")
    ap.add_argument("--regex-file", type=Path, nargs="+", default=None, help="regex file(s); several = the union")
    ap.add_argument("--whole-subs", default="", help="comma-separated subs taken whole: every post there counts as caught")
    ap.add_argument("--slice", default=None, help="only items with this slice value (default: all items in the file)")
    # the aggregator's counting rules, same names and defaults
    ap.add_argument("--kind-key", default="kind")
    ap.add_argument("--own-values", default="own")
    ap.add_argument("--entity-key", default="entities")
    ap.add_argument("--name-key", default="name")
    ap.add_argument("--sub-key", default="sub")
    ap.add_argument("--status-key", default="status")
    ap.add_argument("--segment-key", default="segment")
    ap.add_argument("--unknown", default="unknown")
    ap.add_argument("--groups", default=None, help="status values that count (as in aggregate_matrix.py)")
    ap.add_argument("--extra-group", action="append", default=[], help="name=list_key[@name_key][:filter_key=value]")
    ap.add_argument("--extra-own-only", action="store_true")
    ap.add_argument("--exclude-flag", action="append", default=[])
    ap.add_argument("--population", type=int, default=None, help="corpus size after exclusions, for the projection")
    ap.add_argument("--prefilter-count", type=int, default=None, help="texts the prefilter keeps (export_items.py prints it)")
    ap.add_argument("--list-missed", type=int, default=30, help="print up to N missed T_cell texts for hand reading")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    if not a.regex and not a.regex_file and not a.whole_subs:
        ap.error("give the prefilter: --regex-file / --regex and/or --whole-subs")
    status_groups = [g for g in (a.groups or "").split(",") if g]
    if not status_groups and not a.extra_group:
        ap.error("name what the matrix counts, as for aggregate_matrix.py: --groups and/or --extra-group")
    rx = re.compile(read_regex_files(a.regex_file) if a.regex_file else (a.regex or r"(?!x)x"), re.I)
    whole = {s_.strip() for s_ in a.whole_subs.split(",") if s_.strip()}
    own_values = set(a.own_values.split(","))

    all_items = list(read_ndjson(a.items))
    items = {it["id"]: it for it in all_items if a.slice is None or it.get("slice") == a.slice}
    if not items:
        present = sorted({str(it.get("slice")) for it in all_items})
        sys.exit(f"no items with slice {a.slice!r}; slices in {a.items}: {present}")
    rows = [r for r in read_ndjson(a.results) if r["id"] in items]
    if not rows:
        sys.exit(f"no results match the control items (ids differ?): {a.results}")
    results = {r["id"]: (r.get("result") or {}) for r in rows}
    versions = sorted({str(r.get("prompt_version")) for r in rows if r.get("prompt_version")})
    n = len(results)

    def caught(i: str) -> bool:
        it = items[i]
        if it.get("subreddit") in whole:
            return True
        return bool(rx.search(((it.get("title") or "") + " " + (it.get("text") or "")).lower()))

    t_base = {i for i, r in results.items() if is_own(r, a.kind_key, own_values)}
    t_cell = {i for i in t_base if counted_names(results[i], a)}
    c_base = {i for i in t_base if caught(i)}
    c_cell = {i for i in t_cell if caught(i)}
    definition = (f"T_base = kind in {sorted(own_values)}; T_cell = T_base and >= 1 counted item "
                  f"(statuses {status_groups or '-'}; lists {[e.split('=')[0] for e in a.extra_group] or '-'}"
                  f"{', own rows only' if a.extra_own_only else ''})")
    prefilter = (f"regex {[str(f) for f in a.regex_file] if a.regex_file else a.regex}" if (a.regex_file or a.regex) else "no regex") \
        + (f" + whole subs {sorted(whole)}" if whole else "")
    print(f"prompt version(s) in the results: {', '.join(versions) or 'unknown'}")
    print(f"definition: {definition}")
    print(f"prefilter: {prefilter}")
    print(f"control: {len(items):,d} items, {n:,d} labelled" + (f", {len(items) - n} unlabelled" if len(items) > n else ""))
    print(f"T_base (own case):         {len(t_base)} of {n} = {ci(len(t_base), n)}; caught {len(c_base)} → base recall {ci(len(c_base), len(t_base)) if t_base else '-'}")
    print(f"T_cell (own + counted):    {len(t_cell)} of {n} = {ci(len(t_cell), n)}; caught {len(c_cell)} → cell recall {ci(len(c_cell), len(t_cell)) if t_cell else '-'}")
    out = {"prompt_versions": versions, "definition": definition, "prefilter": prefilter, "control_items": len(items), "labelled": n,
           "t_base": len(t_base), "caught_base": len(c_base), "base_recall_ci95": wilson(len(c_base), len(t_base)) if t_base else None,
           "t_cell": len(t_cell), "caught_cell": len(c_cell), "cell_recall_ci95": wilson(len(c_cell), len(t_cell)) if t_cell else None}
    missed_base = t_base - c_base
    seg_c = Counter(results[i].get(a.segment_key) or a.unknown for i in c_base)
    seg_m = Counter(results[i].get(a.segment_key) or a.unknown for i in missed_base)
    if len(c_base) >= 10 and len(missed_base) >= 10:
        print("segment mix of T_base, caught vs missed (a different mix means the prefilter biases the segments):")
        for s_ in sorted(set(seg_c) | set(seg_m), key=lambda x: -(seg_c[x] + seg_m[x])):
            print(f"  {s_:<20} caught {seg_c[s_]:>3} ({pct(seg_c[s_] / len(c_base))})   missed {seg_m[s_]:>3} ({pct(seg_m[s_] / len(missed_base))})")
        out["segments_caught"], out["segments_missed"] = dict(seg_c), dict(seg_m)
    if len(c_cell) < 30 and n:
        p = max(len(c_cell) / n, 1 / n)
        print(f"HINT: only {len(c_cell)} T_cell texts caught; about {max(math.ceil(30 / p) - n, 0):,d} more control items are needed "
              f"for a usable recall interval")
    if a.population and n:
        proj_base, proj_cell = a.population * len(t_base) / n, a.population * len(t_cell) / n
        line = f"projected in the corpus: ~{proj_base:,.0f} own-case texts, ~{proj_cell:,.0f} with a counted item (of {a.population:,d})"
        if a.prefilter_count is not None:
            line += f"; the prefilter keeps {a.prefilter_count:,d} texts"
        print(line)
        out["projected_t_base"], out["projected_t_cell"] = proj_base, proj_cell
    missed_cell = sorted(t_cell - c_cell)
    out["missed_cell_ids"] = missed_cell
    if missed_cell and a.list_missed:
        print(f"\nmissed T_cell texts, read these by hand ({min(len(missed_cell), a.list_missed)} of {len(missed_cell)}): "
              f"truly the behaviour → widen phrases / take the sub whole; not the behaviour → a prompt example")
        for i in missed_cell[: a.list_missed]:
            it = items[i]
            names = ", ".join(f"{g}:{nm}" for g, nm, _ in counted_names(results[i], a))
            print(f"  r/{it.get('subreddit', '?')} | {(it.get('title') or '')[:70]} | {names}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(out, ensure_ascii=False, indent=1))
        print(f"written {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
