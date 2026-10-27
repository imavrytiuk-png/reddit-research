#!/usr/bin/env python3
"""Resilient downloader of a subreddit's posts or comments for a date range from
the Arctic Shift Reddit archive (https://arctic-shift.photon-reddit.com).

Why the archive: Reddit's own search caps every query at 252 results, so a year
of a busy subreddit is only reachable here. It is a free community service: at
most 4 requests in flight across everything, 0.3 s pace per worker, and a
User-Agent header (urllib's default gets 403).

Tasks = subreddits x monthly windows. Windows step one calendar month from --start
(10-18 -> 11-18 -> 12-18 ...), not calendar months. Each task pages oldest-to-newest
with after=<max created_utc of the previous page> until a short page or a page that
adds no new id, so a window stopped half-way holds its OLDEST days and lacks its NEWEST
ones (the loader prints the covered range of every unfinished window).
To stop a run: --max-minutes, or `kill -INT $(cat <root>/state/archive_<kind>.pid)`
(the pid file names the python process, not its shell wrapper). Every page is appended (flush+fsync) to
<root>/raw/<date>/archive_<kind>_<sub>.ndjson BEFORE a ledger line is appended to
<root>/state/archive_<kind>_tasks.jsonl (append-only, last line per task wins).
On start the ledger is reconciled from raw/, so a crash between the two never
loses or repeats a page. SIGINT/SIGTERM finish the in-flight request, persist it
and exit 3; re-run the same command to resume. Done tasks are never re-fetched.

Usage:
  python3 arctic_shift_archive.py --kind posts --subs subA,subB \
      --start 2025-09-25 --end 2026-09-26 --root ./research/reddit
  python3 arctic_shift_archive.py --kind comments --subs subA --start ... --end ...
  ... --dry-run            print the plan, no requests
  ... --retry-failed       also resume tasks marked failed
  ... --workers 4          TOTAL concurrent requests (keep <= 4)
  ... --order sub          one sub at a time; the default roundrobin takes the newest window
                           of every sub first, so a run stopped early leaves each sub its
                           latest months instead of one sub complete and the rest empty
  ... --fields id,author   restrict returned fields (comments default to a short list)
  ... --max-minutes 110    stop cleanly after N minutes (in-flight page persists, exit 3);
                           with the round-robin order every sub keeps its newest months
  ... --scout out/scout.json  read the scout's 12-month counts: --dry-run then prints the
                           request estimate and hours, and the progress line's time left is
                           computed from pages instead of windows

Load the result with load_raw.py (same --root). While running, a progress line every five
minutes gives windows done, requests per minute and the time left at that pace.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = "https://arctic-shift.photon-reddit.com/api"
USER_AGENT = "reddit-research-skill/1.0 (+claude code)"
PAGE_LIMIT = 100
BACKOFF = [2, 4, 8, 16, 32]
CALL_TIMEOUT = 60
PACE_SECONDS = 0.3
MAX_PAGES_PER_WINDOW = 2000  # runaway guard: 200k items per sub-month
COMMENT_FIELDS = "id,link_id,parent_id,author,body,created_utc,score,subreddit"

_write_lock = threading.Lock()


# ---------------------------------------------------------------- plan
def add_months(d: dt.date, months: int) -> dt.date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def build_windows(start: dt.datetime, end: dt.datetime):
    out, cur = [], start
    while cur < end:
        nd = add_months(cur.date(), 1)
        nxt = min(dt.datetime(nd.year, nd.month, nd.day, tzinfo=dt.timezone.utc), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def build_tasks(kind: str, subs: list[str], start: dt.datetime, end: dt.datetime, order: str = "roundrobin") -> list[dict]:
    """Task order matters under a time cap: the pool takes tasks in list order. roundrobin =
    newest window first, every sub, then the next window; sub = one sub at a time."""
    windows = build_windows(start, end)
    pairs = ([(sub, w) for w in reversed(windows) for sub in subs] if order == "roundrobin"
             else [(sub, w) for sub in subs for w in windows])
    return [{"task_key": f"{kind}|{sub}|{s:%Y-%m-%d}|{e:%Y-%m-%d}", "sub": sub, "start": s, "end": e}
            for sub, (s, e) in pairs]


# ---------------------------------------------------------------- ledger + raw
def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _write_lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def read_ledger(ledger: Path) -> dict[str, dict]:
    state: dict[str, dict] = {}
    if not ledger.exists():
        return state
    for line in ledger.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            logging.warning("ledger: corrupt line skipped: %r", line[:100])
            continue
        state[ev["task_key"]] = ev
    return state


def scan_raw(raw_dir: Path, kind: str) -> dict[str, dict]:
    """Rebuild per-task seen ids, page count, cursor from raw files (ground truth)."""
    by_key: dict[str, list[dict]] = {}
    for path in sorted(raw_dir.glob(f"*/archive_{kind}_*.ndjson")):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    env = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if env.get("task_key"):
                    by_key.setdefault(env["task_key"], []).append(env)
    out = {}
    for key, envs in by_key.items():
        envs.sort(key=lambda e: e["fetched_at"])
        seen, last_created, n = set(), None, 0
        for env in envs:
            rows = (env.get("response") or {}).get("data") or []
            n += len(rows)
            for r in rows:
                if r.get("id"):
                    seen.add(r["id"])
                c = r.get("created_utc")
                if c is not None:
                    last_created = c if last_created is None else max(last_created, c)
        out[key] = {"seen_ids": seen, "pages": len(envs), "items": n, "last_created_utc": last_created,
                    "last_fetched_at": envs[-1]["fetched_at"]}
    return out


def reconcile(ledger: Path, ledger_state: dict, raw_state: dict) -> int:
    added = 0
    for key, r in raw_state.items():
        st = ledger_state.get(key)
        known = st.get("pages", 0) if st else 0
        if st and st.get("status") in ("done", "failed") and known >= r["pages"]:
            continue
        if known >= r["pages"]:
            continue
        ev = {"task_key": key, "status": "in_progress", "last_created_utc": r["last_created_utc"], "pages": r["pages"],
              "items": r["items"], "fetched_at": r["last_fetched_at"], "error": None, "note": "reconciled from raw"}
        append_jsonl(ledger, ev)
        ledger_state[key] = ev
        added += 1
    return added


# ---------------------------------------------------------------- http
def build_url(kind: str, sub: str, after: int, before: int, fields: str | None) -> str:
    params = {"subreddit": sub, "after": after, "before": before, "limit": PAGE_LIMIT, "sort": "asc"}
    if fields:
        params["fields"] = fields
    return f"{BASE}/{kind}/search?{urllib.parse.urlencode(params)}"


def fetch(url: str) -> tuple[dict | None, str, int | None]:
    """(data, error, rate_limit_pause). The archive meters requests in fixed 60 s windows and
    answers 422 "Timeout. Maybe slow down a bit" (sometimes 429) when the window is spent;
    X-RateLimit-Reset says how many seconds remain. Retrying inside the window only wastes
    requests, so the pause is returned for the caller to honour across all workers."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        text = e.read().decode('utf-8', 'replace')[:300]
        pause = None
        if e.code == 429 or (e.code == 422 and "slow down" in text.lower()):
            try:
                pause = int(float(e.headers.get("X-RateLimit-Reset", ""))) + 1
            except ValueError:
                pause = 30
        return None, f"http {e.code}: {text}", pause
    except urllib.error.URLError as e:
        return None, f"urlerror: {e.reason}", None
    except (TimeoutError, OSError) as e:
        return None, f"oserror: {e}", None
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return None, f"non-json: {e}", None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None, f"unexpected shape: {str(data)[:200]}", None
    return data, "", None


# ---------------------------------------------------------------- downloader
class Stop(Exception):
    pass


class Downloader:
    def __init__(self, kind: str, root: Path, fields: str | None, dry_run: bool, raw_state: dict):
        self.kind, self.root, self.fields, self.dry_run, self.raw_state = kind, root, fields, dry_run, raw_state
        self.ledger = root / "state" / f"archive_{kind}_tasks.jsonl"
        self.raw_dir = root / "raw"
        self.requests = 0
        self.stop_requested = False
        self.stop_reason: str | None = None
        self.lock = threading.Lock()
        self.date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        self.stats = {"done_before": 0, "done": 0, "failed": 0, "blocked": 0, "pages": 0, "items": 0, "rate_limited": 0}
        self.pause_until = 0.0  # monotonic time until which every worker waits (rate limiter)
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, signum, frame):  # noqa: ANN001
        logging.warning("signal %s: finishing in-flight request, then stopping", signum)
        with self.lock:
            self.stop_requested, self.stop_reason = True, f"signal {signum}"

    def bump(self, k: str, n: int = 1):
        with self.lock:
            self.stats[k] += n

    def wait_if_paused(self):
        while True:
            if self.stop_requested:
                raise Stop(self.stop_reason or "stop")
            with self.lock:
                remaining = self.pause_until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def fetch_retry(self, url: str):
        last = ""
        for attempt, wait in enumerate(BACKOFF, 1):
            self.wait_if_paused()
            with self.lock:
                self.requests += 1
            resp, err, pause = fetch(url)
            if resp is not None:
                return resp, ""
            last = err
            if pause is not None:  # rate limiter: one pause for all workers, no backoff ladder
                pause = min(pause, 90)
                with self.lock:
                    self.stats["rate_limited"] += 1
                    self.pause_until = max(self.pause_until, time.monotonic() + pause)
                logging.warning("rate limited (%s): all workers pause %ds", err[:40], pause)
                continue  # the ladder is for real failures; a limited attempt does not count down
            logging.warning("attempt %d/%d failed: %s", attempt, len(BACKOFF), err[:200])
            if attempt == len(BACKOFF):
                break
            for _ in range(wait):
                if self.stop_requested:
                    raise Stop(self.stop_reason or "stop during backoff")
                time.sleep(1)
        return None, last

    def run_task(self, t: dict, ledger_state: dict, retry_failed: bool):
        key, sub = t["task_key"], t["sub"]
        st = ledger_state.get(key)
        if st and st.get("status") == "done":
            self.bump("done_before")
            return
        if st and st.get("status") == "failed" and not retry_failed:
            self.bump("blocked")
            return
        r = self.raw_state.get(key, {})
        seen: set[str] = set(r.get("seen_ids") or set())
        pages = int(st.get("pages", 0)) if st else 0
        total = int(st.get("items", 0)) if st else 0
        cursor = st.get("last_created_utc") if st else r.get("last_created_utc")
        after = cursor if cursor is not None else int(t["start"].timestamp())
        before = int(t["end"].timestamp())
        if self.dry_run:
            print(f"WOULD FETCH {key} after={after} (resume at page {pages})")
            return
        while True:
            if self.stop_requested:
                raise Stop(self.stop_reason or "stop")
            if pages >= MAX_PAGES_PER_WINDOW:
                logging.warning("%s hit the runaway guard (%d pages)", key, pages)
                break
            time.sleep(PACE_SECONDS)
            url = build_url(self.kind, sub, after, before, self.fields)
            resp, err = self.fetch_retry(url)
            fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            if resp is None:
                ev = {"task_key": key, "status": "failed", "last_created_utc": cursor, "pages": pages, "items": total,
                      "fetched_at": fetched_at, "error": err[:500]}
                append_jsonl(self.ledger, ev)
                ledger_state[key] = ev
                self.bump("failed")
                logging.error("FAILED %s after %d page(s): %s", key, pages, err[:200])
                if "403" in err:
                    logging.error("HINT: pitfall (Collection): 403 = User-Agent missing or blocked; fix it, then --retry-failed")
                elif "429" in err or "slow down" in err.lower():
                    logging.error("HINT: pitfall (Collection): the rate window never reopened after the pauses; "
                                  "lower --workers, wait a minute, then --retry-failed")
                return
            append_jsonl(self.raw_dir / self.date / f"archive_{self.kind}_{sub}.ndjson",
                         {"task_key": key, "source": "arctic_shift", "url": url, "fetched_at": fetched_at, "response": resp})
            rows = resp.get("data") or []
            ids = [x.get("id") for x in rows if x.get("id")]
            new = [i for i in ids if i not in seen]
            seen.update(ids)
            pages += 1
            total += len(rows)
            self.bump("pages")
            self.bump("items", len(rows))
            created = [x.get("created_utc") for x in rows if x.get("created_utc") is not None]
            if created:
                cursor = max(created) if cursor is None else max(cursor, max(created))
            logging.info("ok %s page=%d items=%d new=%d cursor=%s req=%d", key, pages, len(rows), len(new), cursor, self.requests)
            short, stuck = len(rows) < PAGE_LIMIT, (len(rows) > 0 and not new)
            if not (short or stuck):
                ev = {"task_key": key, "status": "in_progress", "last_created_utc": cursor, "pages": pages,
                      "items": total, "fetched_at": fetched_at, "error": None}
                append_jsonl(self.ledger, ev)
                ledger_state[key] = ev
                after = cursor
                continue
            ev = {"task_key": key, "status": "done", "last_created_utc": cursor, "pages": pages, "items": total,
                  "fetched_at": fetched_at, "error": None, "stop_reason": "page_stuck_no_new_ids" if stuck else "short_page"}
            append_jsonl(self.ledger, ev)
            ledger_state[key] = ev
            self.bump("done")
            logging.info("done %s pages=%d items=%d", key, pages, total)
            return

    def run(self, tasks: list[dict], ledger_state: dict, retry_failed: bool, workers: int, max_minutes: float | None = None,
            expected_pages: int = 0, pages_before: int = 0):
        self.expected_pages, self.pages_before = expected_pages, pages_before
        cap_timer = None
        if max_minutes:
            def _cap():
                with self.lock:
                    if not self.stop_requested:
                        self.stop_requested, self.stop_reason = True, f"time cap {max_minutes:g} min"
                logging.warning("time cap reached (%g min): finishing in-flight pages, then stopping", max_minutes)
            cap_timer = threading.Timer(max_minutes * 60, _cap)
            cap_timer.daemon = True
            cap_timer.start()
        try:
            return self._run(tasks, ledger_state, retry_failed, workers)
        finally:
            if cap_timer:
                cap_timer.cancel()

    def _run(self, tasks: list[dict], ledger_state: dict, retry_failed: bool, workers: int):
        if self.dry_run or workers <= 1:
            for t in tasks:
                self.run_task(t, ledger_state, retry_failed)
            return self.stats
        first_stop = None
        t0 = last_log = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:  # ONE pool = total in-flight cap
            futs = {pool.submit(self.run_task, t, ledger_state, retry_failed): t for t in tasks}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Stop as e:
                    first_stop = first_stop or e
                except Exception:  # noqa: BLE001
                    logging.exception("task crashed: %s", futs[fut]["task_key"])
                now = time.monotonic()
                if now - last_log >= 300:
                    last_log = now
                    worked = self.stats["done"] + self.stats["failed"]
                    left = len(tasks) - self.stats["done_before"] - self.stats["blocked"] - worked
                    elapsed = now - t0
                    if worked and elapsed > 0:
                        exp = getattr(self, "expected_pages", 0)
                        if exp and self.stats["pages"]:
                            left_pages = max(exp - getattr(self, "pages_before", 0) - self.stats["pages"], 0)
                            eta_h = left_pages / (self.stats["pages"] / elapsed) / 3600
                            logging.info("progress: %d window(s) done, %d pages in %.0f min, about %d pages left of ~%d expected, "
                                         "%.0f requests/min, about %.1f h left at this pace",
                                         worked, self.stats["pages"], elapsed / 60, left_pages, exp, self.requests / (elapsed / 60), eta_h)
                        else:
                            eta_h = left * (elapsed / worked) / 3600
                            logging.info("progress: %d window(s) done in %.0f min, %d left, %.0f requests/min, about %.1f h left at this "
                                         "pace (windows differ in size: pass --scout for a page-based estimate)",
                                         worked, elapsed / 60, left, self.requests / (elapsed / 60), eta_h)
        if first_stop:
            raise first_stop
        return self.stats


def per_sub_table(ledger_state: dict, tasks: list[dict]) -> str:
    rows: dict[str, dict] = {}
    for t in tasks:
        r = rows.setdefault(t["sub"], {"tasks": 0, "done": 0, "failed": 0, "items": 0, "pages": 0})
        st = ledger_state.get(t["task_key"]) or {}
        r["tasks"] += 1
        r["done"] += st.get("status") == "done"
        r["failed"] += st.get("status") == "failed"
        r["items"] += st.get("items", 0)
        r["pages"] += st.get("pages", 0)
    lines = [f"{'subreddit':<26} {'windows':>7} {'done':>5} {'failed':>6} {'pages':>7} {'items':>9}"]
    for sub, r in sorted(rows.items()):
        lines.append(f"{'r/' + sub:<26} {r['tasks']:>7} {r['done']:>5} {r['failed']:>6} {r['pages']:>7,d} {r['items']:>9,d}")
    return "\n".join(lines)


def estimate_pages(scout_path: Path | None, subs: list[str], kind: str, n_windows: int) -> dict[str, int]:
    """Expected pages per sub from the scout JSON: 12-month count / 100 per page, scaled to the
    number of windows. Missing subs get 0 (unknown)."""
    if not scout_path:
        return {}
    data = json.loads(Path(scout_path).read_text(encoding="utf-8"))
    key = "comments_12m" if kind == "comments" else "posts_12m"
    by = {(x.get("subreddit") or "").lower(): x for x in data.get("subreddits", [])}
    out = {}
    for sub in subs:
        x = by.get(sub.lower()) or {}
        n = x.get(key) or 0
        out[sub] = int(-(-n * n_windows / 12 // 100)) if n else 0  # ceil
    return out


def summarize(ledger_state: dict, tasks: list[dict]) -> dict:
    c = {"total": len(tasks), "done": 0, "failed": 0, "in_progress": 0, "pending": 0, "items": 0, "pages": 0}
    for t in tasks:
        st = ledger_state.get(t["task_key"])
        c[st["status"] if st else "pending"] += 1
        if st:
            c["items"] += st.get("items", 0)
            c["pages"] += st.get("pages", 0)
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=["posts", "comments"], required=True)
    ap.add_argument("--subs", required=True, help="comma-separated subreddit names (case-sensitive)")
    ap.add_argument("--start", required=True, help="UTC date YYYY-MM-DD, inclusive")
    ap.add_argument("--end", required=True, help="UTC date YYYY-MM-DD, exclusive")
    ap.add_argument("--root", type=Path, default=Path("."), help="folder holding raw/ state/ logs/ out/")
    ap.add_argument("--workers", type=int, default=4, help="TOTAL concurrent requests (keep <= 4)")
    ap.add_argument("--fields", default=None, help="comma-separated fields; comments default to a short list")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--order", choices=["roundrobin", "sub"], default="roundrobin",
                    help="roundrobin: newest window first across all subs (default); sub: one sub at a time")
    ap.add_argument("--max-minutes", type=float, default=None, help="stop cleanly after N minutes; re-run to resume")
    ap.add_argument("--scout", type=Path, default=None, help="scout JSON (subreddit_scout.py --out) for the request estimate and ETA")
    a = ap.parse_args()

    root = a.root.resolve()
    for d in ("state", "raw", "logs", "out"):
        (root / d).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(root / "logs" / f"archive_{a.kind}.log"), logging.StreamHandler(sys.stderr)])
    subs = [s.strip() for s in a.subs.split(",") if s.strip()]
    start = dt.datetime.fromisoformat(a.start).replace(tzinfo=dt.timezone.utc)
    end = dt.datetime.fromisoformat(a.end).replace(tzinfo=dt.timezone.utc)
    fields = a.fields if a.fields is not None else (COMMENT_FIELDS if a.kind == "comments" else None)
    workers = min(a.workers, 4)

    tasks = build_tasks(a.kind, subs, start, end, a.order)
    dl = Downloader(a.kind, root, fields, a.dry_run, scan_raw(root / "raw", a.kind))
    ledger_state = read_ledger(dl.ledger)
    n_rec = reconcile(dl.ledger, ledger_state, dl.raw_state)
    if n_rec:
        logging.info("reconciled %d task(s) from raw", n_rec)
    before = summarize(ledger_state, tasks)
    logging.info("plan: kind=%s subs=%s tasks=%d state=%s", a.kind, subs, len(tasks), before)
    n_windows = len(build_windows(start, end))
    est = estimate_pages(a.scout, subs, a.kind, n_windows)
    total_pages = sum(est.values())
    if est:
        unknown = [s_ for s_, n in est.items() if n == 0]
        lines = [f"estimate from {a.scout}: {n_windows} window(s) per sub, a page = {PAGE_LIMIT} items"]
        for s_, n in sorted(est.items(), key=lambda x: -x[1]):
            lines.append(f"  r/{s_:<24} ~{n:>6,d} requests")
        lines.append(f"  total ~{total_pages:,d} requests = about {total_pages / 40 / 60:.1f}–{total_pages / 10 / 60:.1f} h "
                     f"at 10–40 requests/min on one IP" + (f"; no scout count for: {', '.join(unknown)}" if unknown else ""))
        print("\n".join(lines))
        logging.info("estimate: ~%d requests, %.1f–%.1f h", total_pages, total_pages / 40 / 60, total_pages / 10 / 60)
    elif a.dry_run:
        print(f"{len(tasks)} windows ({n_windows} per sub). Pass --scout out/scout.json for the request and time estimate "
              f"(a page = {PAGE_LIMIT} items, 10–40 requests/min per IP).")
    if a.dry_run:
        dl.run(tasks, ledger_state, a.retry_failed, 1)
        print("dry-run:", before)
        return 0
    pid_file = root / "state" / f"archive_{a.kind}.pid"
    pid_file.write_text(str(os.getpid()))
    started, stop_reason = dt.datetime.now(dt.timezone.utc), "completed"
    try:
        dl.run(tasks, ledger_state, a.retry_failed, workers, a.max_minutes, total_pages, before["pages"])
    except Stop as e:
        stop_reason = str(e)
    finally:
        pid_file.unlink(missing_ok=True)
    after = summarize(ledger_state, tasks)
    ended = dt.datetime.now(dt.timezone.utc)
    table = per_sub_table(ledger_state, tasks)
    with (root / "out" / "archive-sessions.md").open("a", encoding="utf-8") as f:
        f.write(f"\n## {a.kind} session {started:%Y-%m-%dT%H:%M:%SZ} -> {ended:%Y-%m-%dT%H:%M:%SZ}\n\n"
                f"- subs: {subs}, tasks: {len(tasks)}, requests: {dl.requests} "
                f"(rate-limited: {dl.stats['rate_limited']}), stop: {stop_reason}\n"
                f"- before: {before}\n- after:  {after}\n\n```\n{table}\n```\n")
    print(table)
    print(f"requests={dl.requests} rate_limited={dl.stats['rate_limited']} stop={stop_reason} "
          f"done={after['done']}/{after['total']} failed={after['failed']} items={after['items']:,d}")
    return 0 if stop_reason == "completed" else 3


if __name__ == "__main__":
    sys.exit(main())
