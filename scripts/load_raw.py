#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["duckdb>=1.0"]
# ///
"""Rebuild <root>/reddit.duckdb from raw/: Arctic Shift pages (arctic_shift_archive.py)
and treg search pages (treg_reddit_search.py). Raw is the truth; both tables are
dropped and rebuilt on every run.

posts(id, subreddit, author, title, selftext, created_utc, score, num_comments, permalink,
      url, flair, removed_by_category, text_removed, source, fetched_at)
      source = archive | search | both; for a post seen twice the row with a non-removed
      text wins, then the archive row, then the latest fetch.
comments(id, post_id, parent_id, is_top_level, author, body, body_removed, created_utc,
      score, subreddit, fetched_at)   -- archive only; earliest fetch wins.

Prints per-subreddit counts and date spans; the sub x UTC-day gap check, one line per
sub (days at 0 or under 20% of the sub's median; the current day and the windows a pull
has not finished are excluded); coverage steps (a month-to-month jump of 5x with the low
side under 30% of the sub's monthly median: the archive's coverage or the sub's own life
changes there, so keep the sub for counts and drop it from any period comparison); posts
found only by search (archive gaps); and orphan comments whose post is outside the loaded
posts. Run with `uv run load_raw.py --root <root>`; `--selftest` checks the merge rules.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import duckdb

REMOVED = {"[removed]", "[deleted]", ""}


def iter_envelopes(raw_dir: Path, pattern: str):
    for path in sorted(raw_dir.glob(pattern)):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue


def strip_prefix(v, prefix):
    return v[len(prefix):] if isinstance(v, str) and v.startswith(prefix) else v


def post_row(p: dict, source: str, fetched_at: str) -> tuple | None:
    """Normalise an archive / tikhub / scrapecreators post into the posts tuple."""
    if source == "archive" or "selftext" in p:
        pid, title, text = p.get("id"), p.get("title") or "", p.get("selftext") or ""
        author, created = p.get("author"), p.get("created_utc")
        score, ncom = p.get("score"), p.get("num_comments")
        permalink, url, flair = p.get("permalink"), p.get("url"), p.get("link_flair_text")
        sub, rbc = p.get("subreddit"), p.get("removed_by_category")
    else:  # tikhub
        pid, title, text = p.get("id"), p.get("postTitle") or "", ((p.get("content") or {}).get("markdown") or "")
        author = (p.get("authorInfo") or {}).get("name")
        created = None
        if p.get("createdAt"):
            try:
                created = int(dt.datetime.fromisoformat(p["createdAt"].replace("+0000", "+00:00")).timestamp())
            except ValueError:
                created = None
        score, ncom = p.get("score"), p.get("commentCount")
        permalink, url, flair = p.get("permalink"), p.get("url"), (p.get("flair") or {}).get("text")
        sub, rbc = (p.get("subreddit") or {}).get("name"), None
    pid = strip_prefix(pid, "t3_")
    if not pid:
        return None
    removed = (text.strip() in REMOVED) or bool(rbc)
    return (pid, sub, author, title, text, created, score, ncom, permalink, url, flair, rbc, removed, source, fetched_at)


def better(a: tuple | None, b: tuple) -> tuple:
    """Pick the row to keep: non-removed text > archive > latest fetch."""
    if a is None:
        return b
    if a[12] != b[12]:
        return b if a[12] else a
    if a[13] != b[13]:
        return a if a[13] == "archive" else b
    return b if b[14] > a[14] else a


def load_posts(raw_dir: Path):
    best: dict[str, tuple] = {}
    sources: dict[str, set] = {}
    for env in iter_envelopes(raw_dir, "*/archive_posts_*.ndjson"):
        for p in (env.get("response") or {}).get("data") or []:
            row = post_row(p, "archive", env.get("fetched_at", ""))
            if row:
                best[row[0]] = better(best.get(row[0]), row)
                sources.setdefault(row[0], set()).add("archive")
    for env in iter_envelopes(raw_dir, "*/search_*.ndjson"):
        kind, resp = env.get("endpoint_kind"), env.get("response") or {}
        if kind == "tikhub":
            try:
                main = resp["data"]["search"]["dynamic"]["components"]["main"]
            except (KeyError, TypeError):
                continue
            posts = [c["post"] for edge in main.get("edges") or [] for c in (edge.get("node") or {}).get("children") or [] if c.get("post")]
        else:
            posts = resp.get("posts") or []
        for p in posts:
            row = post_row(p, "search", env.get("fetched_at", ""))
            if row:
                best[row[0]] = better(best.get(row[0]), row)
                sources.setdefault(row[0], set()).add("search")
    out = {}
    for pid, row in best.items():
        src = "both" if len(sources[pid]) == 2 else next(iter(sources[pid]))
        out[pid] = row[:13] + (src, row[14])
    return out


def load_comments(raw_dir: Path):
    best: dict[str, tuple] = {}
    for env in iter_envelopes(raw_dir, "*/archive_comments_*.ndjson"):
        for c in (env.get("response") or {}).get("data") or []:
            cid = strip_prefix(c.get("id"), "t1_")
            if not cid or cid in best:
                continue
            body, parent = c.get("body") or "", c.get("parent_id") or ""
            best[cid] = (cid, strip_prefix(c.get("link_id"), "t3_"), parent, parent.startswith("t3_"), c.get("author"),
                         body, body.strip() in REMOVED, c.get("created_utc"), c.get("score"), c.get("subreddit"),
                         env.get("fetched_at", ""))
    return best


def gap_check(con, table: str):
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    return con.execute(f"""
        WITH d AS (SELECT subreddit AS sub, strftime(to_timestamp(created_utc), '%Y-%m-%d') AS day, count(*) AS n
                   FROM {table} WHERE created_utc IS NOT NULL GROUP BY 1, 2),
        span AS (SELECT sub, min(day) AS d0, max(day) AS d1 FROM d GROUP BY 1),
        days AS (SELECT s.sub, strftime(gs.generate_series, '%Y-%m-%d') AS day
                 FROM span s, generate_series(CAST(s.d0 AS DATE), CAST(s.d1 AS DATE), INTERVAL 1 DAY) gs),
        filled AS (SELECT days.sub, days.day, coalesce(d.n, 0) AS n FROM days LEFT JOIN d USING (sub, day)),
        med AS (SELECT sub, median(n) AS med FROM filled GROUP BY 1)
        SELECT f.sub, f.day, f.n, m.med FROM filled f JOIN med m USING (sub)
        WHERE f.day < '{today}' AND (f.n = 0 OR f.n < 0.2 * m.med) ORDER BY 1, 2""").fetchall()


def coverage_steps(con, table: str) -> list[tuple[str, str, int, str, int]]:
    """(sub, month0, n0, month1, n1) where the count jumps 5x between adjacent months and the
    low side is under 30% of the sub's monthly median."""
    rows = con.execute(f"""SELECT subreddit, strftime(to_timestamp(created_utc), '%Y-%m') AS m, count(*)
                           FROM {table} WHERE created_utc IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    by: dict[str, list] = defaultdict(list)
    for sub, m, n in rows:
        by[sub].append((m, n))
    out = []
    for sub, ms in by.items():
        if len(ms) < 4:
            continue
        med = sorted(n for _, n in ms)[len(ms) // 2]
        for (m0, n0), (m1, n1) in zip(ms, ms[1:]):
            lo, hi = min(n0, n1), max(n0, n1)
            if hi >= 5 * max(lo, 1) and lo < 0.3 * med:
                out.append((sub, m0, n0, m1, n1))
    return out


def report_gaps(table: str, gaps: list, steps: list) -> None:
    per_sub: dict[str, list] = defaultdict(list)
    for g in gaps:
        per_sub[g[0]].append(g)
    step_of: dict[str, list] = defaultdict(list)
    for st in steps:
        step_of[st[0]].append(st)
    if gaps:
        print(f"  {table}: {len(gaps)} flagged sub-days (0 or <20% of the sub's median daily count):")
        for sub, gs in sorted(per_sub.items(), key=lambda x: -len(x[1])):
            ex = "; ".join(f"{d}: {n}" for _, d, n, _ in gs[:3])
            print(f"    r/{sub}: {len(gs)} day(s), {gs[0][1]} .. {gs[-1][1]} (median {gs[0][3]:.0f}/day), e.g. {ex}")
    for sub, sts in sorted(step_of.items()):
        for _, m0, n0, m1, n1 in sts:
            print(f"    r/{sub}: coverage step {m0} ({n0:,d}) -> {m1} ({n1:,d}): the archive's coverage or the sub's life "
                  f"changes here; keep it for counts, drop it from period comparisons")


def unfinished_windows(root: Path, kind: str) -> list[tuple[str, str, str, str, str]]:
    """Archive windows whose last ledger status is not done: (sub, start, end, status, covered_to).
    The pull pages oldest to newest, so an unfinished window holds its oldest days up to
    covered_to and lacks its newest ones. A flagged day inside one is the pull, not a gap."""
    path = root / "state" / f"archive_{kind}_tasks.jsonl"
    if not path.exists():
        return []
    last: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ev = json.loads(line)
            last[ev["task_key"]] = ev
    out = []
    for key, ev in last.items():
        parts = key.split("|")
        if len(parts) == 4 and ev.get("status", "") != "done":
            cur = ev.get("last_created_utc")
            covered = dt.datetime.fromtimestamp(cur, dt.timezone.utc).strftime("%Y-%m-%d") if cur else "(nothing yet)"
            out.append((parts[1], parts[2], parts[3], ev.get("status", ""), covered))
    return sorted(out)


def build(root: Path, db: str) -> None:
    posts, comments = load_posts(root / "raw"), load_comments(root / "raw")
    con = duckdb.connect(str(root / db))
    con.execute("SET TimeZone='UTC'")  # day bins and spans in UTC; the Mac's zone shifts 23:59 UTC into the next day
    con.execute("DROP TABLE IF EXISTS posts; DROP TABLE IF EXISTS comments")
    con.execute("""CREATE TABLE posts (id VARCHAR PRIMARY KEY, subreddit VARCHAR, author VARCHAR, title VARCHAR,
        selftext VARCHAR, created_utc BIGINT, score INTEGER, num_comments INTEGER, permalink VARCHAR, url VARCHAR,
        flair VARCHAR, removed_by_category VARCHAR, text_removed BOOLEAN, source VARCHAR, fetched_at VARCHAR)""")
    con.execute("""CREATE TABLE comments (id VARCHAR PRIMARY KEY, post_id VARCHAR, parent_id VARCHAR, is_top_level BOOLEAN,
        author VARCHAR, body VARCHAR, body_removed BOOLEAN, created_utc BIGINT, score INTEGER, subreddit VARCHAR,
        fetched_at VARCHAR)""")
    if posts:
        con.executemany("INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", list(posts.values()))
    if comments:
        con.executemany("INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?,?)", list(comments.values()))
    print(f"posts={len(posts):,d} comments={len(comments):,d} -> {root / db}")
    for table in ("posts", "comments"):
        for sub, n, d0, d1 in con.execute(f"""SELECT subreddit, count(*), min(strftime(to_timestamp(created_utc),'%Y-%m-%d')),
                max(strftime(to_timestamp(created_utc),'%Y-%m-%d')) FROM {table} GROUP BY 1 ORDER BY 1""").fetchall():
            print(f"  {table:8s} r/{sub or '?':<22s} {n:>9,d}  {d0} .. {d1}")
        gaps = gap_check(con, table)
        open_windows = unfinished_windows(root, table)
        if open_windows:
            print(f"  {table}: pull unfinished, {len(open_windows)} window(s) not done (re-run the archive command to resume); "
                  f"an unfinished window holds its oldest days and lacks its newest:")
            for sub, s, e, status, covered in open_windows[:20]:
                print(f"    r/{sub} {s}..{e}: {status}, covered {s}..{covered}, missing {covered}..{e}")
            if len(open_windows) > 20:
                print(f"    ... {len(open_windows) - 20} more")
        inside = [g for g in gaps if any(g[0] == sub and s <= g[1] < e for sub, s, e, _, _ in open_windows)]
        gaps = [g for g in gaps if g not in inside]
        if inside:
            print(f"  {table}: {len(inside)} flagged sub-days lie inside unfinished windows (the pull, not gaps)")
        report_gaps(table, gaps, coverage_steps(con, table))
    only_search = con.execute("SELECT subreddit, count(*) FROM posts WHERE source='search' GROUP BY 1 ORDER BY 1").fetchall()
    if only_search:
        print("  posts found only by search (archive gaps or not yet indexed):")
        for sub, n in only_search:
            print(f"    r/{sub}: {n:,d}")
    removed = con.execute("SELECT count(*) FILTER (WHERE text_removed), count(*) FROM posts").fetchone()
    if removed and removed[1]:
        print(f"  title-only posts (text removed/empty): {removed[0]:,d} of {removed[1]:,d} ({100 * removed[0] / removed[1]:.0f}%)")
    if comments:
        orphans = con.execute("SELECT count(*) FROM comments c LEFT JOIN posts p ON p.id=c.post_id WHERE p.id IS NULL").fetchone()[0]
        print(f"  orphan comments (post outside the loaded posts; expected at window edges): {orphans:,d}")
    con.close()


def selftest() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="load_selftest_"))
    day = (tmp / "raw" / "2026-01-01")
    day.mkdir(parents=True)
    env = lambda key, data, fetched, **kw: json.dumps({"task_key": key, "fetched_at": fetched, "response": {"data": data}, **kw}) + "\n"
    (day / "archive_posts_s1.ndjson").write_text(
        env("posts|s1|a|b", [{"id": "a", "subreddit": "s1", "author": "ann", "title": "T", "selftext": "full text", "created_utc": 1700000000},
                             {"id": "t3_b", "subreddit": "s1", "author": "bob", "title": "U", "selftext": "[removed]", "created_utc": 1700003600}],
            "2026-01-01T00:00:00") +
        env("posts|s1|a|b", [{"id": "a", "subreddit": "s1", "author": "ann", "title": "T", "selftext": "[removed]", "created_utc": 1700000000}],
            "2026-01-01T01:00:00"))
    (day / "search_s1.ndjson").write_text(
        env("q", [{"id": "c", "subreddit": "s1", "author": "cy", "title": "V", "selftext": "only by search", "created_utc": 1700007200}],
            "2026-01-01T02:00:00", endpoint_kind="scrapecreators").replace('{"data": [', '{"posts": [').replace(']}}', ']}}'))
    (day / "archive_comments_s1.ndjson").write_text(
        env("comments|s1|a|b", [{"id": "t1_k", "link_id": "t3_a", "parent_id": "t3_a", "author": "dee", "body": "hi", "created_utc": 1700000100, "subreddit": "s1"},
                                {"id": "t1_m", "link_id": "t3_zz", "parent_id": "t1_k", "author": "eve", "body": "[deleted]", "created_utc": 1700000200, "subreddit": "s1"}],
            "2026-01-01T03:00:00"))
    build(tmp, "t.duckdb")
    con = duckdb.connect(str(tmp / "t.duckdb"), read_only=True)
    rows = {r[0]: r for r in con.execute("SELECT id, selftext, text_removed, source FROM posts").fetchall()}
    assert set(rows) == {"a", "b", "c"}, set(rows)
    assert rows["a"][1] == "full text" and rows["a"][2] is False and rows["a"][3] == "archive", rows["a"]   # non-removed text wins; t3_ stripped
    assert rows["b"][2] is True and rows["c"][3] == "search", (rows["b"], rows["c"])
    com = {r[0]: r for r in con.execute("SELECT id, post_id, is_top_level, body_removed FROM comments").fetchall()}
    assert set(com) == {"k", "m"} and com["k"][1] == "a" and com["k"][2] is True and com["m"][3] is True, com
    orphans = con.execute("SELECT count(*) FROM comments c LEFT JOIN posts p ON p.id=c.post_id WHERE p.id IS NULL").fetchone()[0]
    assert orphans == 1, orphans
    con.close()
    print("selftest ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--db", default="reddit.duckdb")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    build(a.root.resolve(), a.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
