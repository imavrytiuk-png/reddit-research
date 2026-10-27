#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["duckdb>=1.0,<2"]
# ///
"""Export items for reading or labelling from <root>/reddit.duckdb to NDJSON.

Post item:    {"id","subreddit","created_utc","author_hash","title","text","title_only","weight"[,"slice","hints"]}
Comment item: {"id","subreddit","created_utc","author_hash","post_id","post_title","text","is_top_level","weight"[,"slice","hints"]}

Local preparation, no model, applied in this order and reported on stderr:
- deleted / empty authors dropped (they cannot be one person);
- bots dropped (_common.is_bot: AutoModerator, *modteam, a "bot" token; plus --bots-file,
  one name per line, for single-token bots like sneakpeekbot);
- removed comment bodies dropped; title-only posts stay, with the text
  "(title-only, body removed or empty)";
- duplicates dropped (default; --no-dedup keeps them): the same author with the same
  normalised title and opening of the body (comments: the same body) keeps the earliest
  copy. Cross-posts and reposts then count once. Marketing accounts are not detected
  here: that is the model's "promo" label.
- author_hash = sha256(salt + author)[:24]. The salt is <root>/state/salt.txt, created
  only with --new-salt (once per study) and never committed: a lost or changed salt
  splits every person in two across exports. The salt fingerprint is printed so two
  exports can be checked for the same salt.
- weight = population / items drawn, on every item: 1.0 for --all, above 1 for --sample
  or a --permutation prefix (1.0 with --ids-file, which is not a sample).
  aggregate_matrix.py uses it when slices were sampled at different rates.
- --slice NAME marks the items of one prefilter slice (for example "artefact", "uniform");
  recall_check.py and compare_runs.py report per slice.

Selection, one of: --all; --sample N --seed S (uniform random); --ids-file; or
--permutation PATH [--limit N --offset M] (a seeded shuffle of the whole population,
written once and reused, so any prefix is a uniform random sample).
Filters: --subs, --since, --until (UTC dates), --prefilter-file (one or more regex files, joined
with |, matched on lower(title + " " + body) through a bound parameter, so apostrophes need no
escaping; several files = the union of the families), --sql-where (table alias t, for anything
else), --exclude-ids-file. With --prefilter-file the population line is the "texts the
prefilter keeps" figure that recall_check.py --prefilter-count asks for.
--hints hints.ndjson (from entity_match.py) adds "hints": [canonical names].
Text is truncated to the first --head and last --tail characters.

Usage:
  uv run export_items.py --root r --kind posts --sample 200 --seed 1 --slice uniform \
      --out items/explore.ndjson --md items/explore.md --new-salt
  uv run export_items.py --root r --kind posts --all --slice app --prefilter-file state/prefilter_app.txt --out items/app.ndjson
  uv run export_items.py --root r --kind posts --all --prefilter-file state/prefilter_app.txt state/prefilter_manual.txt \
      --out /dev/null        # prints the union's population = the prefilter count
  uv run export_items.py --root r --kind comments --permutation state/comments_perm.json --limit 10000 --out items/c1.ndjson
  uv run export_items.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import secrets
import sys
import tempfile
from pathlib import Path

import duckdb

from _common import is_bot, is_deleted, norm_text, read_bots_file, read_id_list, read_ndjson, read_regex_files


def exclusion(author: str | None, kind: str, body_removed, bots_extra: set[str]) -> str | None:
    if is_deleted(author):
        return "deleted authors"
    if is_bot(author, bots_extra):
        return "bots"
    if kind == "comments" and body_removed:
        return "removed bodies"
    return None


def get_salt(root: Path, create: bool) -> str:
    p = root / "state" / "salt.txt"
    if not p.exists():
        if not create:
            sys.exit(f"no salt at {p}. First export of this study: add --new-salt once and keep state/salt.txt out "
                     "of git. If this study already has exported items, restore the original salt file instead: "
                     "a new salt gives every person a new hash and splits them in two.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(16))
        p.chmod(0o600)
        print(f"created {p} (keep it out of git)", file=sys.stderr)
    return p.read_text().strip()


def salt_fingerprint(salt: str) -> str:
    return hashlib.sha256(salt.encode("utf-8")).hexdigest()[:8]


def truncate(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail + 100:
        return text
    return text[:head] + " […] " + text[-tail:]


def epoch(date: str | None, end: bool = False) -> int | None:
    if not date:
        return None
    d = dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc)
    if end:
        d += dt.timedelta(days=1)
    return int(d.timestamp())


def dedup_key(kind: str, r: tuple) -> tuple:
    if kind == "posts":  # author + normalised title + opening of the body
        return (r[3], norm_text(r[4])[:200], norm_text(r[5])[:200])
    return (r[3], norm_text(r[4])[:300])  # comments: author + body


def run(a: argparse.Namespace) -> int:
    root = a.root.resolve()
    salt = get_salt(root, a.new_salt)
    bots_extra = read_bots_file(a.bots_file)
    con = duckdb.connect(str(root / a.db), read_only=True)
    conds, params = [], []
    if a.subs:
        subs = [s.strip() for s in a.subs.split(",") if s.strip()]
        conds.append(f"t.subreddit IN ({','.join('?' * len(subs))})")
        params += subs
    if a.since:
        conds.append("t.created_utc >= ?")
        params.append(epoch(a.since))
    if a.until:
        conds.append("t.created_utc < ?")
        params.append(epoch(a.until, end=True))
    if a.sql_where:
        conds.append(f"({a.sql_where})")
    if a.prefilter_file:
        col = "lower(t.title || ' ' || coalesce(t.selftext, ''))" if a.kind == "posts" else "lower(coalesce(t.body, ''))"
        conds.append(f"regexp_matches({col}, ?)")
        params.append(read_regex_files(a.prefilter_file))
    if a.kind == "posts":
        sql = f"""SELECT t.id, t.subreddit, t.created_utc, t.author, t.title, t.selftext, t.text_removed
                  FROM posts t WHERE {' AND '.join(conds) or 'TRUE'} ORDER BY t.id"""
    else:
        sql = f"""SELECT t.id, t.subreddit, t.created_utc, t.author, t.body, t.post_id, t.is_top_level, p.title, t.body_removed
                  FROM comments t LEFT JOIN posts p ON p.id = t.post_id WHERE {' AND '.join(conds) or 'TRUE'} ORDER BY t.id"""
    rows = con.execute(sql, params).fetchall()
    con.close()
    excluded: dict[str, int] = {"deleted authors": 0, "bots": 0, "removed bodies": 0, "duplicates (same author + text)": 0}
    kept = []
    for r in rows:
        why = exclusion(r[3], a.kind, r[8] if a.kind == "comments" else None, bots_extra)
        if why:
            excluded[why] += 1
        else:
            kept.append(r)
    if not a.no_dedup:
        first: dict[tuple, tuple] = {}
        for r in kept:
            k = dedup_key(a.kind, r)
            if k not in first or (r[2] or 0) < (first[k][2] or 0):
                first[k] = r
        excluded["duplicates (same author + text)"] = len(kept) - len(first)
        kept = list(first.values())
    by_id = {r[0]: r for r in kept}
    exclude = read_id_list(a.exclude_ids_file)
    population = sorted(i for i in by_id if i not in exclude)
    excluded["--exclude-ids-file"] = len(by_id) - len(population)
    shown = ", ".join(f"{v:,d} {k}" for k, v in excluded.items() if v) or "nothing"
    print(f"loaded {len(rows):,d} {a.kind} in scope; excluded {shown}; population {len(population):,d}; "
          f"salt {salt_fingerprint(salt)}", file=sys.stderr)
    if a.prefilter_file:
        print(f"  prefilter ({', '.join(str(f) for f in a.prefilter_file)}) keeps {len(population):,d} texts: "
              f"the --prefilter-count for recall_check.py", file=sys.stderr)

    weight = 1.0
    if a.all:
        chosen = population
    elif a.sample is not None:
        rng = random.Random(a.seed)
        chosen = rng.sample(population, min(a.sample, len(population)))
        weight = len(population) / len(chosen) if chosen else 1.0
    elif a.ids_file:
        wanted = read_id_list(a.ids_file)
        chosen = [i for i in population if i in wanted]
        missing = wanted - set(chosen)
        if missing:
            print(f"  {len(missing)} ids from --ids-file not in the population", file=sys.stderr)
        print("  --ids-file is not a sample: weight 1.0 on every item", file=sys.stderr)
    else:
        perm = json.loads(a.permutation.read_text()) if a.permutation.exists() else None
        if perm and not perm.get("order"):
            print(f"permutation {a.permutation} is empty (made before data was loaded): rebuilding", file=sys.stderr)
            perm = None
        if perm:
            order = [i for i in perm["order"] if i in by_id and i not in exclude]
            missing = len(set(population) - set(perm["order"]))
            print(f"permutation {a.permutation} (seed {perm.get('seed')}, {len(perm['order']):,d} ids, {len(order):,d} usable)", file=sys.stderr)
            if missing:
                print(f"  warning: {missing:,d} population ids are not in the permutation (new data since it was made); "
                      f"they are never drawn. Delete the file to rebuild only if nothing was labelled from it yet.", file=sys.stderr)
        else:
            order = list(population)
            random.Random(a.seed).shuffle(order)
            a.permutation.parent.mkdir(parents=True, exist_ok=True)
            a.permutation.write_text(json.dumps({"kind": a.kind, "seed": a.seed, "n": len(order),
                                                 "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                                 "order": order}))
            print(f"wrote permutation {a.permutation} ({len(order):,d} ids, seed {a.seed})", file=sys.stderr)
        chosen = order[a.offset: a.offset + a.limit] if a.limit is not None else order[a.offset:]
        weight = len(order) / len(chosen) if chosen else 1.0
    if weight != 1.0:
        print(f"  sample weight {weight:.3f} (population {len(population):,d} / {len(chosen):,d} drawn)", file=sys.stderr)

    hints: dict[str, list[str]] = {}
    if a.hints:
        for h in read_ndjson(a.hints):
            hints[h["id"]] = [x["canonical"] if isinstance(x, dict) else str(x) for x in h.get("hints", [])]

    a.out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    md = None
    if a.md:
        a.md.parent.mkdir(parents=True, exist_ok=True)
        md = a.md.open("w", encoding="utf-8")
        md.write(f"# {a.kind} sample: {len(chosen):,d} items ({a.out.name})\n\n")
    with a.out.open("w", encoding="utf-8") as f:
        for i in chosen:
            r = by_id[i]
            author_hash = hashlib.sha256((salt + (r[3] or "")).encode("utf-8")).hexdigest()[:24]
            if a.kind == "posts":
                title_only = bool(r[6]) or not (r[5] or "").strip()
                item = {"id": r[0], "subreddit": r[1], "created_utc": r[2], "author_hash": author_hash, "title": r[4] or "",
                        "text": "(title-only, body removed or empty)" if title_only else truncate(r[5], a.head, a.tail),
                        "title_only": title_only}
            else:
                item = {"id": r[0], "subreddit": r[1], "created_utc": r[2], "author_hash": author_hash, "post_id": r[5],
                        "post_title": r[7] or "", "text": truncate(r[4] or "", a.head, a.tail), "is_top_level": bool(r[6])}
            item["weight"] = round(weight, 6)
            if a.slice:
                item["slice"] = a.slice
            if i in hints:
                item["hints"] = hints[i]
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            n += 1
            if md:
                day = dt.datetime.fromtimestamp(item["created_utc"], dt.timezone.utc).strftime("%Y-%m-%d")
                head = f"### {n}. r/{item['subreddit']} · {item['id']} · {day}"
                if a.kind == "posts":
                    md.write(f"{head}\n\n**{item['title']}**\n\n{item['text']}\n\n")
                else:
                    where = "top-level" if item["is_top_level"] else "reply"
                    md.write(f"{head} · {where} in “{item['post_title']}”\n\n{item['text']}\n\n")
    if md:
        md.close()
        print(f"readable copy -> {a.md}", file=sys.stderr)
    print(f"wrote {n:,d} items -> {a.out}", file=sys.stderr)
    return 0


def selftest() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="export_selftest_"))
    con = duckdb.connect(str(tmp / "reddit.duckdb"))
    con.execute("CREATE TABLE posts (id VARCHAR, subreddit VARCHAR, author VARCHAR, title VARCHAR, selftext VARCHAR, "
                "created_utc BIGINT, text_removed BOOLEAN)")
    con.executemany("INSERT INTO posts VALUES (?,?,?,?,?,?,?)", [
        ("a1", "s1", "alice", "Which machine should I buy?", "body one", 1000, False),
        ("a2", "s2", "alice", "Which machine should I buy?", "body one", 1100, False),      # cross-post: duplicate
        ("a3", "s1", "alice", "Which machine should I buy?", "a different body", 1200, False),  # same title, new body: kept
        ("b1", "s1", "[deleted]", "gone", "x", 1300, False),
        ("c1", "s1", "RemindMeBot", "I will remind you", "x", 1400, False),
        ("d1", "s1", "dave", "Title only", "", 1500, True),
        ("e1", "s1", "Bottle", "A human called Bottle", "text", 1600, False),
    ])
    con.close()
    base = dict(root=tmp, db="reddit.duckdb", kind="posts", sample=None, ids_file=None, permutation=None, seed=1, limit=None,
                offset=0, subs="", since=None, until=None, sql_where=None, exclude_ids_file=None, hints=None, md=None,
                head=2400, tail=600, slice="test", no_dedup=False, bots_file=None, prefilter_file=[])
    a = argparse.Namespace(**base, all=True, out=tmp / "all.ndjson", new_salt=True)
    run(a)
    items = {i["id"]: i for i in read_ndjson(a.out)}
    assert set(items) == {"a1", "a3", "d1", "e1"}, set(items)
    assert items["d1"]["title_only"] and items["d1"]["text"].startswith("(title-only")
    assert all(i["weight"] == 1.0 and i["slice"] == "test" for i in items.values())
    assert items["a1"]["author_hash"] == items["a3"]["author_hash"] != items["e1"]["author_hash"]
    b = argparse.Namespace(**{**base, "sample": 2}, all=False, out=tmp / "sample.ndjson", new_salt=False)
    run(b)
    s = list(read_ndjson(b.out))
    assert len(s) == 2 and abs(s[0]["weight"] - 2.0) < 1e-9, s
    assert s[0]["author_hash"] == items[s[0]["id"]]["author_hash"], "same salt must give the same hash"
    (tmp / "pf.txt").write_text("machine|human's\n")
    c = argparse.Namespace(**{**base, "prefilter_file": [tmp / "pf.txt"]}, all=True, out=tmp / "pf.ndjson", new_salt=False)
    run(c)
    assert {i["id"] for i in read_ndjson(c.out)} == {"a1", "a3"}, "prefilter-file must bind the regex (apostrophe safe)"
    (tmp / "state" / "salt.txt").unlink()
    try:
        run(b)
        raise AssertionError("an export without a salt must refuse")
    except SystemExit as e:
        assert "no salt" in str(e)
    print("selftest ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--db", default="reddit.duckdb")
    ap.add_argument("--kind", choices=["posts", "comments"])
    ap.add_argument("--out", type=Path)
    sel = ap.add_mutually_exclusive_group()
    sel.add_argument("--all", action="store_true")
    sel.add_argument("--sample", type=int)
    sel.add_argument("--ids-file", type=Path)
    sel.add_argument("--permutation", type=Path)
    ap.add_argument("--seed", type=int, default=20260101)
    ap.add_argument("--limit", type=int, default=None, help="with --permutation: take N after --offset")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--subs", default="", help="comma-separated subreddit filter")
    ap.add_argument("--since", default=None, help="UTC date YYYY-MM-DD inclusive")
    ap.add_argument("--until", default=None, help="UTC date YYYY-MM-DD inclusive")
    ap.add_argument("--sql-where", default=None, help="extra SQL condition on the base table (alias t)")
    ap.add_argument("--prefilter-file", type=Path, action="append", default=[],
                    help="regex file matched on lower(title + ' ' + body); repeat for the union of families")
    ap.add_argument("--exclude-ids-file", type=Path, default=None)
    ap.add_argument("--hints", type=Path, default=None, help="hints NDJSON from entity_match.py")
    ap.add_argument("--md", type=Path, default=None, help="also write a readable Markdown copy of the items here")
    ap.add_argument("--slice", default=None, help="name of this prefilter slice, written on every item")
    ap.add_argument("--no-dedup", action="store_true", help="keep an author's repeated posts (default drops them)")
    ap.add_argument("--bots-file", type=Path, default=None, help="extra bot usernames, one per line")
    ap.add_argument("--new-salt", action="store_true", help="create state/salt.txt when missing (first export of a study)")
    ap.add_argument("--head", type=int, default=2400)
    ap.add_argument("--tail", type=int, default=600)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.kind or not a.out or not (a.all or a.sample is not None or a.ids_file or a.permutation):
        ap.error("--kind, --out and one of --all / --sample / --ids-file / --permutation are required")
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
