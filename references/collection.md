# Collection

## Window
12 months by default, ending a week before the pull; keep `created_utc` on every row so
recency can be judged. Twelve months is not a convenience: most behaviours have a season
(gifts in December, moves in summer), and a 60-day window lands in the low or the peak
without saying which. The scout's peak and low months say whether this matters. The
archive's last days can be partial, hence the week of margin. Every day boundary is UTC:
the loader sets its session to UTC so a post at 23:59 UTC is not binned into the next
local day.

## Rate limiter
The archive meters requests in fixed 60-second windows per client and answers
`422 {"error":"Timeout. Maybe slow down a bit"}` (sometimes 429) when the window is
spent, with `X-RateLimit-Reset` = seconds until it opens. Retrying inside the window
only burns requests. The scripts pause every worker until the reset and count the
pauses; a run that reports many pauses is simply fast, not broken. Keyword search
(`query=`) is metered far more heavily than listings; sitewide keyword search is refused
(HTTP 400) and `aggregate` with a keyword can answer 0 for a non-zero count.

The budget is per IP, and it shrinks when the archive is busy (evenings: pauses after a
handful of requests; mornings: a hundred a minute). Two studies on one machine share it:
two scouts plus two pulls at once crawl at a request a minute each. Run one archive
process at a time per machine, and kill only the PID you started (never `pkill -f` a
shared script name: that kills the other study's run).

## Source 1: Arctic Shift archive (free, complete)

`https://arctic-shift.photon-reddit.com/api` indexes Reddit continuously.

- Posts: `/posts/search?subreddit=<sub>&after=<epoch>&before=<epoch>&limit=100&sort=asc`
- Comments: `/comments/search?…&fields=id,link_id,parent_id,author,body,created_utc,score,subreddit`
- Subreddits: `/subreddits/search?subreddit=<name>` or `?subreddit_prefix=<word>`
- Page by `after = max(created_utc)` of the previous page; a task ends on a short page
  (<100) or a page that adds no new id.
- House rules: at most **4 requests in flight across everything**, 0.3 s pace per
  worker, a `User-Agent` header (the default urllib agent gets 403). Backoff 2/4/8/16/32 s,
  +30 s on 429, 5 attempts, then the task is `failed` and the run continues.
- Removed bodies arrive as `[removed]` / `[deleted]` with `removed_by_category`; a
  moderated sub can have a quarter of posts title-only. Keep them: titles often carry the
  whole fact.

```
python3 scripts/arctic_shift_archive.py --kind posts --subs a,b,c --start 2025-10-01 --end 2026-10-01 --root research/<name>
python3 scripts/arctic_shift_archive.py --kind comments --subs a,b,c --start ... --end ... --root research/<name>
```
`--dry-run` prints the plan; with `--scout out/scout.json` it prints the request estimate
(12-month posts / 100 per sub) and the hours at 10–40 requests a minute, and the running
pull's time left is then computed from pages instead of windows (a giant's window is 50
pages, a small sub's is 2). Re-running resumes; `--retry-failed` resumes failed tasks;
`--max-minutes N` stops cleanly at a time cap the owner set (exit 3, same command
resumes); `kill -INT $(cat state/archive_posts.pid)` does the same by hand.

Windows step one calendar month from `--start` (10-18 → 11-18 → 12-18), not calendar
months. Inside a window pages run oldest to newest, so a window stopped half-way holds
its oldest days and lacks its newest; the loader prints `covered … missing …` for every
unfinished window. With the round-robin order a capped run therefore leaves each sub its
newest complete months plus one partial older month.
Tasks run round-robin across subs, newest window first (`--order roundrobin`, the
default), so a run stopped at a time cap leaves every sub its latest months instead of
one sub complete and the rest empty; `--order sub` pulls one sub at a time.

## Source 2: Reddit search through treg (optional, paid, capped)

Needs the `treg` CLI logged in. Without it skip this source: the archive alone has been
complete enough for every study so far, and the only loss is the last few days.

- `tikhub.x.reddit-app-fetch-dynamic-search`: priced per page of 7 posts, full text and
  flair, sitewide search scoped with `subreddit:<name>` in the query.
  `scrapecreators.x.v1-reddit-subreddit-search` returns titles only.
- **Every query is capped at 252 results** (36 pages × 7). A year of a busy sub is not
  reachable by search; that is why the archive is primary.
- Use it for: the most recent days, a completeness cross-check (search found a post the
  archive did not → a real gap), a quick first look, or when the archive is down.
- `scripts/treg_reddit_search.py --config subs.yaml --root research/<name>`: plan =
  subs × intent phrases × (sort, timeframe) passes; adaptive stop after N pages in a row
  with under X% new ids; call cap and balance floor; ledger, raw-first, resume. Plan
  format: `templates/subs.example.yaml`. Needs the `treg` CLI logged in.

## Load

`uv run scripts/load_raw.py --root research/<name>` rebuilds `reddit.duckdb` from
`raw/` every time: `posts` (archive and search merged by id, `source` column, the
non-removed text wins) and `comments`. It prints per-sub counts and date spans, the sub ×
day gap check as one line per sub (days at 0 or under 20% of the sub's median; the
current day and the windows a pull has not finished are excluded), coverage steps (a
month-to-month jump of 5x: the archive's coverage or the sub's own life changes there;
keep the sub for counts, never compare its periods), posts found only by search
(archive gaps), and orphan comments whose post is outside the window (expected at the
edges). A re-run of the archive with a different `--end` extends the window silently:
keep one `--start`/`--end` per study in the runbook.

Show the owner the coverage table before anything else is built on it.

## Resilience rules (any collector)

1. Enumerate tasks up front; every state change appends one JSON line to a ledger;
   replay with "last line per key wins".
2. **Raw first.** Append the response to `raw/<date>/<name>.ndjson` with flush+fsync
   before the ledger line. On start, reconcile raw into the ledger, so a crash between
   the two never repeats a paid call.
3. Resume = skip done tasks, continue from the stored cursor.
4. SIGINT/SIGTERM set a flag; the in-flight request finishes and persists; exit code 3.
5. Paid sources get a per-session call cap and a balance floor.
6. A session log in `out/*-sessions.md`: tasks before/after, requests, cost.

## Storage and privacy

- DuckDB, one file per stage (`reddit.duckdb`, `labels.duckdb`…), one writer each.
  Downstream readers use a snapshot copy taken when no writer runs.
- Backup a database before a script writes into an existing one.
- gitignored: `*.duckdb`, `raw/`, `raw_llm/`, `logs/`, `state/salt.txt`, large CSVs.
  Committed: scripts, prompts, `report_data/` matrices and summaries. Items
  and review files hold text: not in a public repository.
- `author_hash = sha256(salt + author)`; the salt lives in `state/salt.txt`, created once
  per study with `export_items.py --new-salt` and never in git. A lost or changed salt
  splits every person in two across exports; the exporter prints the salt fingerprint so
  two exports can be checked. The hash is pseudonymous, not anonymous: anyone with the
  salt and a username list can reverse it, which is why the salt stays private.
- Pages show no usernames and no quotes. Items and review files hold text and quotes:
  they stay out of a public repository (`deliverables.md` §6).
