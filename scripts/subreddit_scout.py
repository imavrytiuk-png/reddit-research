#!/usr/bin/env python3
"""Scout candidate subreddits from the Arctic Shift index (free, no auth): size, exact
monthly activity, language, description, and a probe of how many people in the sub
actually do the behaviour.

Usage:
  python3 subreddit_scout.py --names espresso,Coffee
  python3 subreddit_scout.py --prefix espresso --prefix coffee --require "espresso|coffee|barista"
  python3 subreddit_scout.py --names espresso,Coffee,buildapc \
      --probe "my first machine|which grinder|dial(l)?ing in" --probe-days 14 --probe-names espresso,Coffee --out out/scout.json

What is measured
- Monthly counts come from the archive's time_series endpoint: one request per sub, exact,
  no paging cap. The table shows the last full month, the 12-month total, and the peak and
  low months (seasonality).
- --probe REGEX pulls the last --probe-days days of posts for the subs in --probe-names (or,
  without it, the --probe-top most active measured candidates, default 15; in a prefix
  search those are the noisiest giants) and matches the regex locally on title + body, excluding deleted
  authors and bots. Per sub: posts in the window, on-topic posts, distinct on-topic authors,
  authors projected per year (authors × 365 / days), 5 on-topic titles and 10 random titles.
  Projected on-topic authors per year is the selection metric; the share alone is not.
- The archive meters requests in fixed 60 s windows (422 "slow down" / 429 with an
  X-RateLimit-Reset header); the script waits out the window instead of retrying blindly.

- --probe-save PATH writes every probed post (deleted authors and bots removed) as NDJSON:
  {"id","subreddit","created_utc","title","text" (first 1,500 chars of the body),"on_topic"}.
  Read on-topic posts from EVERY probed sub (5-8 each), not the first 50: app or product
  subs produce most hits and would dominate the reading.
- Each --probe argument is counted separately ("phrase hits"), and a phrase that is also a
  probed sub's name (excel, mint, copilot) is flagged: its share there measures the name,
  not the behaviour.
- The table's "days" column is the window the probe actually covered: a giant sub hits
  --probe-pages in a day or two, and its authors/year is marked ~ (rough).

Candidates under --min-subscribers are listed but not measured. Not-found names are
listed at the end: the sub may be private, banned, renamed, or spelled differently. A
central sub missing from the index is a gap to record in the study's claims note.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from _common import DELETED, is_bot

BASE = "https://arctic-shift.photon-reddit.com/api"
UA = "reddit-research-skill/1.0 (+claude code)"
PACE = 0.3
REQUESTS = {"n": 0, "rate_limited": 0}


def get_json(path: str, params: dict) -> dict | None:
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(6):
        REQUESTS["n"] += 1
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                time.sleep(PACE)
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:200]
            if e.code == 429 or (e.code == 422 and "slow down" in text.lower()):
                try:
                    pause = min(int(float(e.headers.get("X-RateLimit-Reset", ""))) + 1, 90)
                except ValueError:
                    pause = 30
                REQUESTS["rate_limited"] += 1
                print(f"  rate limited, waiting {pause}s", file=sys.stderr)
                time.sleep(pause)
                continue
            if e.code in (502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            print(f"  http {e.code} for {path} {params}: {text[:120]}", file=sys.stderr)
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            print(f"  error for {path} {params}: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None


def meta_rows(name: str | None = None, prefix: str | None = None, limit: int = 100) -> list[dict]:
    params = {"limit": limit}
    if name:
        params["subreddit"] = name
    else:
        params["subreddit_prefix"] = prefix
    data = get_json("subreddits/search", params)
    rows = (data or {}).get("data") or []
    if name:
        rows = [r for r in rows if (r.get("display_name") or "").lower() == name.lower()] or rows[:1]
    return rows


def monthly(name: str, what: str) -> list[tuple[dt.date, int]]:
    data = get_json("time_series", {"key": f"r/{name}/{what}/count", "precision": "month"})
    rows = (data or {}).get("data") or []
    return [(dt.datetime.fromtimestamp(r["date"], dt.timezone.utc).date(), int(r.get("value") or 0)) for r in rows]


def activity(name: str) -> dict:
    """Exact monthly counts: the last full month, the 12 full months before now, peak and low."""
    today = dt.datetime.now(dt.timezone.utc).date()
    cur = today.replace(day=1)
    out = {"posts_month": None, "posts_12m": None, "comments_12m": None, "peak": None, "low": None, "months": []}
    posts = [(d, v) for d, v in monthly(name, "posts") if d < cur][-12:]
    comments = [(d, v) for d, v in monthly(name, "comments") if d < cur][-12:]
    if posts:
        out["posts_month"] = posts[-1][1]
        out["posts_12m"] = sum(v for _, v in posts)
        out["months"] = [{"month": d.strftime("%Y-%m"), "posts": v} for d, v in posts]
        if len(posts) >= 6:
            hi, lo = max(posts, key=lambda x: x[1]), min(posts, key=lambda x: x[1])
            out["peak"], out["low"] = f"{hi[0]:%b} {hi[1]:,d}", f"{lo[0]:%b} {lo[1]:,d}"
            med = sorted(v for _, v in posts)[len(posts) // 2]
            for (d0, v0), (d1, v1) in zip(posts, posts[1:]):  # a 5x jump with the low side under 30% of the median
                if max(v0, v1) >= 5 * max(min(v0, v1), 1) and min(v0, v1) < 0.3 * med:
                    out["note"] = (f"coverage step {d0:%Y-%m} ({v0:,d}) -> {d1:%Y-%m} ({v1:,d}): the archive's coverage or "
                                   f"the sub's life changes here; a 12-month count overstates its steady level")
    if comments:
        out["comments_12m"] = sum(v for _, v in comments)
    if not out["posts_12m"]:  # tiny subs can be missing from time_series: one listing page of the last 30 days
        after = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).timestamp())
        data = get_json("posts/search", {"subreddit": name, "after": after, "limit": 100, "fields": "id"})
        n = len((data or {}).get("data") or [])
        out["posts_month"] = n
        out["posts_12m"] = None
        out["note"] = "no time_series; last 30 days by listing" + (" (>=100)" if n == 100 else "")
    return out


PHRASE_HITS: dict[str, int] = {}


def probe(name: str, days: int, rx: re.Pattern, max_pages: int, rng: random.Random, save=None, phrases=None) -> dict:
    """Pull the last N days of posts newest-first and count the behaviour locally."""
    now = dt.datetime.now(dt.timezone.utc)
    after = int((now - dt.timedelta(days=days)).timestamp())
    before = int(now.timestamp())
    posts: list[dict] = []
    capped = False
    for page in range(max_pages):
        data = get_json("posts/search", {"subreddit": name, "after": after, "before": before, "limit": 100,
                                         "sort": "desc", "fields": "id,author,title,selftext,created_utc"})
        rows = (data or {}).get("data") or []
        posts += rows
        if len(rows) < 100:
            break
        before = min(r.get("created_utc", before) for r in rows) - 1
        capped = page == max_pages - 1
    people = [p for p in posts if (p.get("author") or "") not in DELETED and not is_bot(p.get("author") or "")]
    texts = [((p.get("title") or "") + " " + (p.get("selftext") or "")).lower() for p in people]
    hits = [p for p, t in zip(people, texts) if rx.search(t)]
    for ph, prx in (phrases or []):
        PHRASE_HITS[ph] = PHRASE_HITS.get(ph, 0) + sum(1 for t in texts if prx.search(t))
    authors = {p["author"] for p in hits}
    if save is not None:  # the probe's posts, for the first-hour reading sample (no pull needed)
        hit_ids = {p.get("id") for p in hits}
        for p in people:
            save.write(json.dumps({"id": p.get("id"), "subreddit": name, "created_utc": p.get("created_utc"),
                                   "title": p.get("title") or "", "text": (p.get("selftext") or "")[:1500],
                                   "on_topic": p.get("id") in hit_ids}, ensure_ascii=False) + "\n")
    n_days = days
    if capped and posts:
        first = min(p.get("created_utc", before) for p in posts)
        n_days = max(1.0, (now.timestamp() - first) / 86400)
    titles = [p.get("title") or "" for p in people]
    return {
        "probe_days": round(n_days, 1), "probe_capped": capped,
        "probe_posts": len(people), "on_topic_posts": len(hits), "on_topic_authors": len(authors),
        "on_topic_share": round(100 * len(hits) / len(people), 1) if people else None,
        "authors_per_year": round(len(authors) * 365 / n_days) if people else None,
        "on_topic_titles": [p.get("title") or "" for p in rng.sample(hits, min(5, len(hits)))],
        "random_titles": rng.sample(titles, min(10, len(titles))),
    }


def summarize(r: dict) -> dict:
    meta = r.get("_meta") or {}
    return {
        "subreddit": r.get("display_name") or "",
        "subscribers": r.get("subscribers"),
        "posts_total": meta.get("num_posts"),
        "comments_total": meta.get("num_comments"),
        "lang": r.get("lang"),
        "nsfw": bool(r.get("over18")),
        "type": r.get("subreddit_type"),
        "since": dt.datetime.fromtimestamp(r["created_utc"], dt.timezone.utc).year if r.get("created_utc") else None,
        "description": " ".join((r.get("public_description") or r.get("title") or "").split())[:160],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--names", default="", help="comma-separated subreddit names")
    ap.add_argument("--prefix", action="append", default=[], help="name prefix to search (repeatable)")
    ap.add_argument("--limit", type=int, default=50, help="max prefix matches per prefix")
    ap.add_argument("--require", default=None, help="regex a candidate's name or description must match (cuts prefix noise)")
    ap.add_argument("--min-subscribers", type=int, default=1000, help="measure activity only above this")
    ap.add_argument("--probe", action="append", default=[], help="regex of seed phrases (repeatable; joined with |)")
    ap.add_argument("--probe-days", type=int, default=14)
    ap.add_argument("--probe-pages", type=int, default=20, help="100 posts per page; cap per sub")
    ap.add_argument("--probe-top", type=int, default=15, help="probe only the N most active measured candidates; 0 = all")
    ap.add_argument("--probe-names", default="", help="comma-separated subs to probe (the shortlist); overrides --probe-top")
    ap.add_argument("--probe-save", default=None, help="write every probed post (title, text, on_topic) to this NDJSON for reading")
    ap.add_argument("--seed", type=int, default=1, help="seed for the title samples")
    ap.add_argument("--out", default=None, help="write JSON here")
    a = ap.parse_args()

    rows: dict[str, dict] = {}
    not_found: list[str] = []
    named: set[str] = set()
    for name in [s.strip() for s in a.names.split(",") if s.strip()]:
        for r in meta_rows(name=name):
            key = (r.get("display_name") or name).lower()
            rows[key] = r
            named.add(key)
        if name.lower() not in rows:
            not_found.append(name)
    for prefix in a.prefix:
        for r in meta_rows(prefix=prefix, limit=a.limit):
            rows.setdefault((r.get("display_name") or "").lower(), r)
    if a.require:  # cuts prefix noise only: a name the caller typed is always measured
        req = re.compile(a.require, re.I)
        rows = {k: r for k, r in rows.items()
                if k in named or req.search((r.get("display_name") or "") + " " + (r.get("public_description") or "") + " " + (r.get("title") or ""))}
    if not rows:
        print("no candidates", file=sys.stderr)
        return 1

    measured = [r for r in rows.values() if (r.get("subscribers") or 0) >= a.min_subscribers]
    print(f"{len(rows)} candidates, {len(measured)} with >= {a.min_subscribers:,d} subscribers to measure "
          f"(2 requests each)", file=sys.stderr)
    results = []
    for i, r in enumerate(rows.values(), 1):
        x = summarize(r)
        if (r.get("subscribers") or 0) >= a.min_subscribers and x["subreddit"]:
            x.update(activity(x["subreddit"]))
            print(f"  [{i}/{len(rows)}] r/{x['subreddit']}: {x['posts_month'] or 0:,d} posts last month, "
                  f"{x['posts_12m'] or 0:,d} in 12 months", file=sys.stderr)
        else:
            x.update({"posts_month": None, "posts_12m": None, "comments_12m": None, "peak": None, "low": None, "months": []})
        results.append(x)

    probed = False
    if a.probe:
        rx = re.compile("|".join(f"(?:{p})" for p in a.probe), re.I)
        phrases = [(p, re.compile(p, re.I)) for p in a.probe]
        rng = random.Random(a.seed)
        measured_rows = [x for x in results if x["posts_month"] is not None]
        want = {s.strip().lower() for s in a.probe_names.split(",") if s.strip()}
        if want:  # the shortlist, as named; --probe-top is ignored
            cands = [x for x in measured_rows if x["subreddit"].lower() in want]
            missing = want - {x["subreddit"].lower() for x in cands}
            if missing:
                print(f"--probe-names not measured (not found, or under --min-subscribers): "
                      f"{', '.join(sorted(missing))}", file=sys.stderr)
        else:
            cands = sorted(measured_rows, key=lambda x: -(x["posts_month"] or 0))
            if a.probe_top:
                cands = cands[: a.probe_top]
        print(f"probing the last {a.probe_days} days of {len(cands)} subs for /{rx.pattern}/", file=sys.stderr)
        save = None
        if a.probe_save:
            Path(a.probe_save).parent.mkdir(parents=True, exist_ok=True)
            save = open(a.probe_save, "w", encoding="utf-8")
        for i, x in enumerate(cands, 1):
            x.update(probe(x["subreddit"], a.probe_days, rx, a.probe_pages, rng, save, phrases))
            for ph, prx in phrases:  # a phrase that is the sub's name measures the name, not the behaviour
                if prx.search(x["subreddit"].lower()) and len(ph) > 2:
                    x["note"] = (x.get("note") + "; " if x.get("note") else "") + f"seed phrase /{ph}/ matches the sub's own name"
            print(f"  [{i}/{len(cands)}] r/{x['subreddit']}: {x['probe_posts']:,d} posts in {x['probe_days']:g} days, "
                  f"{x['on_topic_posts']} on-topic by {x['on_topic_authors']} authors → ~{x['authors_per_year'] or 0:,d}/year",
                  file=sys.stderr)
            probed = True
        if save:
            save.close()
            print(f"probed posts written to {a.probe_save} (on_topic flag per post)", file=sys.stderr)
        if len(phrases) > 1:
            print("phrase hits across the probed subs: " + "; ".join(f"/{ph}/ {n}" for ph, n in sorted(PHRASE_HITS.items(), key=lambda x: -x[1])),
                  file=sys.stderr)

    results.sort(key=lambda x: (x.get("authors_per_year") is None, -(x.get("authors_per_year") or 0),
                                x["posts_month"] is None, -(x["posts_month"] or 0), -(x["subscribers"] or 0)))
    hdr = (f"{'subreddit':<26} {'subs':>9} {'posts/mo':>8} {'posts/12m':>9} {'comm/12m':>9} {'peak':>10} {'low':>10} "
           f"{'lang':>4} {'nsfw':>4}")
    if probed:
        hdr += f" {'probe':>7} {'days':>5} {'on-top':>6} {'authors':>7} {'/year':>6} {'share':>6}"
    hdr += "  description"
    print(hdr)
    print("-" * len(hdr))
    for x in results:
        pm = "-" if x["posts_month"] is None else f"{x['posts_month']:,d}"
        p12 = "-" if x["posts_12m"] is None else f"{x['posts_12m']:,d}"
        c12 = "-" if x["comments_12m"] is None else f"{x['comments_12m']:,d}"
        line = (f"{'r/' + x['subreddit']:<26} {x['subscribers'] or 0:>9,d} {pm:>8} {p12:>9} {c12:>9} "
                f"{x['peak'] or '':>10} {x['low'] or '':>10} {x['lang'] or '':>4} {'yes' if x['nsfw'] else '':>4}")
        if probed:
            if x.get("probe_posts") is not None:
                rough = "~" if (x.get("probe_days") or 0) < 3 else ""
                line += (f" {x['probe_posts']:>7,d} {x['probe_days']:>5g} {x['on_topic_posts']:>6} {x['on_topic_authors']:>7} "
                         f"{rough + format(x['authors_per_year'] or 0, ','):>6} {x['on_topic_share'] or 0:>5.1f}%")
            else:
                line += f" {'-':>7} {'-':>5} {'-':>6} {'-':>7} {'-':>6} {'-':>6}"
        print(line + "  " + x["description"][:60])
    notes = [(x["subreddit"], x["note"]) for x in results if x.get("note")]
    if notes:
        print("\nNotes:")
        for sub, note in notes:
            print(f"  r/{sub}: {note}")
    if probed:
        print("\nTitles (on-topic first, then random) for the probed subs:")
        for x in results:
            if x.get("probe_posts") is None:
                continue
            print(f"\nr/{x['subreddit']}: {x['on_topic_posts']} on-topic of {x['probe_posts']} posts"
                  + (" (window cut by --probe-pages)" if x.get("probe_capped") else ""))
            for t in x["on_topic_titles"]:
                print(f"  + {t[:110]}")
            for t in x["random_titles"]:
                print(f"  · {t[:110]}")
    if not_found:
        print(f"\nnot in the index: {', '.join('r/' + n for n in not_found)}. "
              "Check the spelling; the sub may be private, banned or renamed; confirm with a web search.", file=sys.stderr)
    print(f"requests: {REQUESTS['n']} (rate-limited pauses: {REQUESTS['rate_limited']})", file=sys.stderr)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"scouted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       "probe": "|".join(a.probe) or None, "probe_days": a.probe_days if a.probe else None,
                       "not_found": not_found, "subreddits": results}, f, ensure_ascii=False, indent=2)
        print(f"written {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
