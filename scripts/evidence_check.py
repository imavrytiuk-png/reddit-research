#!/usr/bin/env python3
"""Zero-fabrication validator for model labels: every fact must carry a verbatim quote
of the item's own text, or it is dropped.

Rules (generic rules):
- A quote is grounded when, after normalisation on both sides (NFC/NFKC, nbsp, curly
  quotes, markdown characters * _ ` > #, case, whitespace, contractions "Im"/"I'm"/"I am"),
  it is a substring of the source; or it is 2..3 fragments joined by " ... " / " … ",
  each independently a substring, in any order. Max 200 characters. Look-alike words
  ("were"/"we're") are never merged.
- A list entry (a dict with an `evidence` key) whose quote fails is removed.
- A scalar field `X` with `X_evidence` whose quote fails is set to null together with its
  evidence (the VALUE is dropped, not only the quote); with --unknown-keys X it becomes
  "unknown" instead, so an enum field never carries a value outside its schema. Boolean
  false / null fields need no evidence.
- --name-keys applies to every list entry that has that key: give the entity list its
  own key name ("name") and the pain list another ("pain"), or pains are rejected because
  a label like no_results is never inside a quote.
- Optional name check: with --name-keys the entry's name (or an alias from --aliases, a
  JSON {canonical: [aliases]}) must appear, folded, inside the quote or in the title
  field. Without an alias table this rejects "UK" vs "United Kingdom": build aliases when
  entities are places.

Input: the results NDJSON written by claude_batch.py ({"id", "result": {...}}) and the
items NDJSON ({"id", "title"?, "text"}). Output: grounded results NDJSON, rejects NDJSON,
and a summary by field and reason on stderr.

Usage:
  python3 evidence_check.py --results out/S-v1_results.ndjson --items items.ndjson \
      --out out/S-v1_grounded.ndjson --rejects out/S-v1_rejects.ndjson \
      [--text-keys title,text] [--evidence-key evidence] [--suffix _evidence] \
      [--name-keys name,country,city] [--aliases aliases.json] [--title-key title]
  python3 evidence_check.py --selftest
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

MAX_LEN = 200
MAX_FRAGMENTS = 3
CURLY = {"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"',
         "–": "-", "—": "-", " ": " "}
MD_CHARS = set("*_`>#~")
WS_RE = re.compile(r"\s+")
FRAG_RE = re.compile(r"\s*(?:\.\.\.|…)\s*")
CONTRACTIONS = [
    (re.compile(r"\b(i|you|we|they)\s*'?\s*m\b"), r"\1 am"),
    (re.compile(r"\bi am\b"), "i am"),
    (re.compile(r"\b(i|you|we|they)'ve\b"), r"\1 have"),
    (re.compile(r"\b(i|you|we|they|he|she|it)'ll\b"), r"\1 will"),
    (re.compile(r"\b(i|you|we|they|he|she|it)'d\b"), r"\1 would"),
    (re.compile(r"\b(he|she|it|that|there|what|who)'s\b"), r"\1 is"),
    (re.compile(r"\b(you|we|they)'re\b"), r"\1 are"),
    (re.compile(r"\b(do|does|did|is|are|was|were|has|have|had|could|would|should|wo|ca|can)n't\b"), r"\1 not"),
    (re.compile(r"\bwon't\b"), "will not"), (re.compile(r"\bcan't\b"), "can not"),
    (re.compile(r"\bdont\b"), "do not"), (re.compile(r"\bdoesnt\b"), "does not"), (re.compile(r"\bdidnt\b"), "did not"),
    (re.compile(r"\bisnt\b"), "is not"), (re.compile(r"\bwasnt\b"), "was not"), (re.compile(r"\bcant\b"), "can not"),
    (re.compile(r"\bwont\b"), "will not"), (re.compile(r"\bive\b"), "i have"), (re.compile(r"\bim\b"), "i am"),
]


def normalize(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = "".join(CURLY.get(c, c) for c in t)
    t = "".join(c for c in t if c not in MD_CHARS)
    t = t.lower()
    for rx, rep in CONTRACTIONS:
        t = rx.sub(rep, t)
    return WS_RE.sub(" ", t).strip()


def fold(text: str) -> str:
    """Normalised + diacritics removed, for name matching."""
    t = unicodedata.normalize("NFKD", normalize(text))
    return "".join(c for c in t if not unicodedata.combining(c))


def evidence_grounded(evidence, source_text: str, max_len: int = MAX_LEN) -> tuple[bool, str]:
    if evidence is None:
        return False, "missing_evidence"
    if not isinstance(evidence, str) or not evidence.strip():
        return False, "empty_evidence"
    if len(evidence) > max_len:
        return False, "evidence_too_long"
    ne, ns = normalize(evidence), normalize(source_text)
    if not ne:
        return False, "empty_evidence"
    if ne in ns:
        return True, "ok"
    frags = [f for f in FRAG_RE.split(ne) if f]
    if len(frags) < 2:
        return False, "evidence_not_in_source"
    if len(frags) > MAX_FRAGMENTS:
        return False, "too_many_fragments"
    for f in frags:
        if f not in ns:
            return False, "fragment_not_in_source"
    return True, "ok"


def name_in(name: str | None, evidence: str, title: str, aliases: dict[str, list[str]]) -> bool:
    if not name:
        return True
    hay = fold(evidence) + " \n " + fold(title or "")
    forms = [name] + list(aliases.get(name, []))
    for f in forms:
        ff = fold(f)
        if ff and re.search(r"(?<!\w)" + re.escape(ff) + r"(?!\w)", hay):
            return True
    return False


def check_record(result: dict, source: str, *, evidence_key: str = "evidence", suffix: str = "_evidence",
                 name_keys: tuple[str, ...] = (), aliases: dict | None = None, title: str = "",
                 unknown_keys: tuple[str, ...] = ()) -> tuple[dict, list[dict]]:
    """Return (cleaned copy, rejects). Walks nested dicts/lists. A rejected scalar in
    unknown_keys becomes "unknown" (its enum's silent value) instead of null."""
    aliases = aliases or {}
    rejects: list[dict] = []

    def walk(node, path: str):
        if isinstance(node, list):
            kept = []
            for i, el in enumerate(node):
                if isinstance(el, dict) and evidence_key in el:
                    ok, why = evidence_grounded(el.get(evidence_key), source)
                    if ok and name_keys:
                        for nk in name_keys:
                            if nk in el and el[nk] and not name_in(el[nk], el[evidence_key], title, aliases):
                                ok, why = False, f"name_not_in_evidence:{nk}"
                                break
                    if not ok:
                        rejects.append({"field": f"{path}[{i}]", "value": {k: v for k, v in el.items() if k != evidence_key},
                                        "evidence": el.get(evidence_key), "reason": why})
                        continue
                kept.append(walk(el, f"{path}[{i}]"))
            return kept
        if isinstance(node, dict):
            out = dict(node)
            for k in list(out.keys()):
                if k.endswith(suffix) and k != evidence_key:
                    base = k[: -len(suffix)]
                    val = out.get(base)
                    if val in (None, False, "", [], "unknown"):
                        continue  # nothing claimed, no quote needed
                    ok, why = evidence_grounded(out.get(k), source)
                    if ok and base in name_keys and not name_in(val, out[k], title, aliases):
                        ok, why = False, f"name_not_in_evidence:{base}"
                    if not ok:
                        rejects.append({"field": f"{path}.{base}", "value": val, "evidence": out.get(k), "reason": why})
                        out[base] = False if isinstance(val, bool) else ("unknown" if base in unknown_keys else None)
                        out[k] = None
            for k, v in out.items():
                if isinstance(v, (list, dict)):
                    out[k] = walk(v, f"{path}.{k}")
            return out
        return node

    return walk(result, "$"), rejects


def selftest() -> int:
    src = "I'm currently on my **third** week with Notion, paying for the team plan. Sheets was “great”.\nSwitching to Obsidian next year."
    cases = [
        ("I'm currently on my third week with Notion", True),
        ("Im currently on my third week with notion", True),
        ("I am currently on my third week with Notion", True),
        ('Sheets was "great"', True),
        ("third week with Notion ... Switching to Obsidian", True),
        ("Switching to Obsidian ... third week with Notion", True),
        ("I switched to Notion last year", False),
        ("third week with Notion ... Switching to Trello", False),
        ("", False), (None, False), ("x" * 201, False),
    ]
    bad = 0
    for ev, want in cases:
        ok, why = evidence_grounded(ev, src)
        if ok != want:
            bad += 1
            print(f"FAIL {ev!r}: got {ok} ({why}), want {want}")
    rec = {"id": "1", "kind": "own", "segment": "freelancer", "segment_evidence": "freelance life",
           "origin": "agency", "origin_evidence": None, "flag": True, "flag_evidence": "paying for the team plan",
           "entities": [{"name": "Notion", "status": "using", "evidence": "third week with Notion"},
                        {"name": "Obsidian", "status": "considering", "evidence": "Switching to Obsidian next year"},
                        {"name": "Google Sheets", "status": "considering", "evidence": "Sheets was \"great\""},
                        {"name": "Trello", "status": "past", "evidence": "I used Trello before"}]}
    aliases = {"Google Sheets": ["Sheets", "GSheets"]}
    cleaned, rejects = check_record(rec, src, name_keys=("name", "origin"), aliases=aliases)
    checks = [
        (cleaned["segment"] is None and cleaned["segment_evidence"] is None, "ungrounded segment nulled"),
        (cleaned["origin"] is None, "origin without evidence nulled"),
        (cleaned["flag"] is True, "grounded flag kept"),
        ([e["name"] for e in cleaned["entities"]] == ["Notion", "Obsidian", "Google Sheets"], "entities: Google Sheets kept via alias, Trello dropped"),
        (len(rejects) == 3, f"3 rejects, got {len(rejects)}"),
    ]
    for ok, label in checks:
        if not ok:
            bad += 1
            print(f"FAIL {label}: {cleaned} {rejects}")
    print("selftest:", "OK" if bad == 0 else f"{bad} failures")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path)
    ap.add_argument("--items", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--rejects", type=Path)
    ap.add_argument("--text-keys", default="title,text", help="item fields concatenated as the source text")
    ap.add_argument("--title-key", default="title")
    ap.add_argument("--evidence-key", default="evidence")
    ap.add_argument("--suffix", default="_evidence")
    ap.add_argument("--name-keys", default="", help="comma-separated keys whose value must appear in the quote or title")
    ap.add_argument("--aliases", type=Path, default=None, help='JSON {"Canonical": ["alias", ...]}')
    ap.add_argument("--unknown-keys", default="", help="comma-separated scalar keys whose enum has 'unknown': a rejected value becomes 'unknown', not null")
    ap.add_argument("--id-key", default="id")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.results and a.items and a.out and a.rejects):
        ap.error("--results, --items, --out and --rejects are required (or --selftest)")
    text_keys = [k for k in a.text_keys.split(",") if k]
    name_keys = tuple(k for k in a.name_keys.split(",") if k)
    unknown_keys = tuple(k for k in a.unknown_keys.split(",") if k)
    aliases = json.loads(a.aliases.read_text(encoding="utf-8")) if a.aliases else {}
    items = {}
    with a.items.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                it = json.loads(line)
                items[it[a.id_key]] = it
    n_in = n_out = 0
    by_field, by_reason = Counter(), Counter()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.rejects.parent.mkdir(parents=True, exist_ok=True)
    with a.results.open("r", encoding="utf-8") as fin, a.out.open("w", encoding="utf-8") as fout, \
            a.rejects.open("w", encoding="utf-8") as frej:
        for line in fin:
            if not line.strip():
                continue
            row = json.loads(line)
            n_in += 1
            it = items.get(row["id"])
            if it is None:
                frej.write(json.dumps({"id": row["id"], "field": "$", "reason": "item_not_found"}) + "\n")
                by_reason["item_not_found"] += 1
                continue
            source = "\n".join(str(it.get(k) or "") for k in text_keys)
            cleaned, rejects = check_record(row["result"], source, evidence_key=a.evidence_key, suffix=a.suffix,
                                            name_keys=name_keys, aliases=aliases, title=str(it.get(a.title_key) or ""),
                                            unknown_keys=unknown_keys)
            for r in rejects:
                r["id"] = row["id"]
                r["model_tag"] = row.get("model_tag")
                frej.write(json.dumps(r, ensure_ascii=False) + "\n")
                by_field[re.sub(r"\[\d+\]", "[]", r["field"])] += 1
                by_reason[r["reason"]] += 1
            row["result"] = cleaned
            row["grounded"] = True
            fout.write(json.dumps(row, ensure_ascii=False) + "\n")
            n_out += 1
    print(f"results in={n_in} out={n_out} rejects={sum(by_reason.values())}", file=sys.stderr)
    for k, v in by_field.most_common():
        print(f"  field {k}: {v}", file=sys.stderr)
    for k, v in by_reason.most_common():
        print(f"  reason {k}: {v}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
