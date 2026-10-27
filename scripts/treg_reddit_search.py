#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Resilient Reddit search collector through the treg CLI (paid fallback to the archive).

Enumerates (subreddit, phrase, sort, timeframe, page) tasks from a YAML plan
(templates/subs.example.yaml), calls the search endpoint with `treg call`, appends
every successful response to <root>/raw/<date>/search_<sub>.ndjson (flush+fsync)
before recording the task in the append-only ledger <root>/state/search_tasks.jsonl.
Re-running resumes: done tasks are skipped and pagination continues from the stored
cursor, so nothing is fetched or paid twice.

Per (subreddit, phrase, sort, timeframe) group, pagination stops early when a page is
empty, has no next cursor, repeats the previous page, when N consecutive pages each add
under X% new ids (adaptive_stop), or when the whole page is older than max_age_days.
Remaining pages are marked "skipped" with the reason. Reddit caps a query at 252
results, so expect groups to end by themselves.

Budget guards: --max-calls per session and --balance-floor checked with `treg balance`
every 50 calls. SIGINT/SIGTERM finish the in-flight call and exit 3.

Usage:
  uv run treg_reddit_search.py --config subs.yaml --root research/<name>
  uv run treg_reddit_search.py --config subs.yaml --root research/<name> --dry-run
  uv run treg_reddit_search.py ... --retry-failed --workers 4 --max-calls 500
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

TREG = os.environ.get("TREG_BIN") or shutil.which("treg") or os.path.expanduser("~/.local/bin/treg")
BACKOFF = [2, 4, 8, 16, 32]
CALL_TIMEOUT = 90
PACE_SECONDS = 0.4
BALANCE_CHECK_EVERY = 50

ENDPOINTS = {
    "tikhub": {"id": "tikhub.x.reddit-app-fetch-dynamic-search"},
    "scrapecreators": {"id": "scrapecreators.x.v1-reddit-subreddit-search"},
}


# ---------------------------------------------------------------- endpoint adapters
def build_params(endpoint: str, sub: str, query: str, sort: str, timeframe: str, cursor: str | None) -> dict:
    if endpoint == "tikhub":
        p = {"query": f"subreddit:{sub} {query}", "search_type": "post", "sort": sort.upper(), "time_range": timeframe}
        if cursor:
            p["after"] = cursor
        return p
    p = {"subreddit": sub, "query": query, "sort": sort.lower(), "timeframe": timeframe}
    if cursor:
        p["cursor"] = cursor
    return p


class BadResponse(Exception):
    pass


def parse_response(endpoint: str, resp: dict) -> tuple[list[dict], str | None, bool]:
    if endpoint == "tikhub":
        if resp.get("code") != 200:
            raise BadResponse(f"code={resp.get('code')} message={resp.get('message')}")
        try:
            main = resp["data"]["search"]["dynamic"]["components"]["main"]
        except (KeyError, TypeError) as e:
            raise BadResponse(f"missing main: {e}")
        posts = [c["post"] for edge in main.get("edges") or [] for c in (edge.get("node") or {}).get("children") or []
                 if c.get("post") and c["post"].get("id")]
        info = main.get("pageInfo") or {}
        cursor = info.get("endCursor") or None
        return posts, cursor, bool(info.get("hasNextPage")) and cursor is not None
    if not resp.get("success"):
        raise BadResponse(f"success={resp.get('success')} {str(resp)[:200]}")
    posts = resp.get("posts") or []
    cursor = resp.get("cursor") or None
    return posts, cursor, bool(cursor) and len(posts) > 0


def post_ids(posts: list[dict]) -> list[str]:
    return [str(p.get("id") or "")[3:] if str(p.get("id") or "").startswith("t3_") else str(p.get("id") or "") for p in posts]


def post_subreddits(endpoint: str, posts: list[dict]) -> list[str]:
    if endpoint == "tikhub":
        return [((p.get("subreddit") or {}).get("name") or "") for p in posts]
    return [p.get("subreddit") or "" for p in posts]


def post_created(endpoint: str, posts: list[dict]) -> list[dt.datetime]:
    out = []
    for p in posts:
        try:
            if endpoint == "tikhub":
                s = p.get("createdAt")
                if s:
                    out.append(dt.datetime.fromisoformat(s.replace("+0000", "+00:00")))
            elif p.get("created_utc"):
                out.append(dt.datetime.fromtimestamp(float(p["created_utc"]), tz=dt.timezone.utc))
        except (ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------- plan + ledger
def task_key(endpoint, sub, query, sort, timeframe, page):
    return f"{endpoint}|{sub}|{query}|{sort.lower()}|{timeframe}|{page}"


def load_plan(cfg: dict) -> tuple[str, list[dict]]:
    endpoint = cfg.get("endpoint", "tikhub")
    if endpoint not in ENDPOINTS:
        raise SystemExit(f"unknown endpoint {endpoint!r}; one of {list(ENDPOINTS)}")
    d = cfg.get("defaults", {})

    def passes_of(o):
        if "passes" in o:
            return o["passes"]
        return d.get("passes") or [{"sort": d.get("sort", "relevance"), "timeframe": d.get("timeframe", "year"),
                                    "max_pages": d.get("max_pages", 36)}]

    groups = []
    for sub, over in (cfg.get("subreddits") or {}).items():
        over = over or {}
        phrases = list(over.get("phrases", d.get("phrases", []))) + list(over.get("extra_phrases", []))
        for ps in passes_of(over):
            sort, tf, mp = str(ps["sort"]), str(ps["timeframe"]), int(ps.get("max_pages", 36))
            for phrase in phrases:
                groups.append({"subreddit": sub, "query": str(phrase), "sort": sort, "timeframe": tf, "max_pages": mp,
                               "max_age_days": ps.get("max_age_days"),
                               "keys": [task_key(endpoint, sub, str(phrase), sort, tf, i) for i in range(mp)]})
    return endpoint, groups


_write_lock = threading.Lock()


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _write_lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def read_ledger(ledger: Path) -> dict[str, dict]:
    state = {}
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    ev = json.loads(line)
                    state[ev["task_key"]] = ev
                except (json.JSONDecodeError, KeyError):
                    continue
    return state


def scan_raw(raw_dir: Path, ledger: Path, state: dict) -> tuple[set[str], int]:
    seen, added = set(), 0
    for path in sorted(raw_dir.glob("*/search_*.ndjson")):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    env = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = env.get("task_key")
                if not k:
                    continue
                try:
                    posts, cursor, has_next = parse_response(env["endpoint_kind"], env["response"])
                except Exception:  # noqa: BLE001
                    continue
                ids = post_ids(posts)
                seen.update(ids)
                if state.get(k, {}).get("status") == "done":
                    continue
                ev = {"task_key": k, "status": "done", "cursor_used": (env.get("params") or {}).get("after") or (env.get("params") or {}).get("cursor"),
                      "cursor_returned": cursor, "has_next": has_next, "n_posts": len(posts), "post_ids": ids,
                      "fetched_at": env.get("fetched_at"), "error": None, "note": "reconciled from raw"}
                append_jsonl(ledger, ev)
                state[k] = ev
                added += 1
    return seen, added


def treg_balance() -> float | None:
    try:
        out = subprocess.run([TREG, "balance"], capture_output=True, text=True, timeout=60).stdout
    except Exception as e:  # noqa: BLE001
        logging.warning("treg balance failed: %s", e)
        return None
    m = re.search(r"Balance\s+\$([0-9.]+)", out)
    return float(m.group(1)) if m else None


class Stop(Exception):
    pass


class Collector:
    def __init__(self, endpoint, root: Path, max_calls, balance_floor, dry_run, adaptive, seen):
        self.endpoint, self.ep, self.root = endpoint, ENDPOINTS[endpoint], root
        self.ledger = root / "state" / "search_tasks.jsonl"
        self.raw_dir = root / "raw"
        self.max_calls, self.balance_floor, self.dry_run = max_calls, balance_floor, dry_run
        self.min_novelty = float(adaptive.get("min_novelty", 0.2))
        self.consecutive_pages = int(adaptive.get("consecutive_pages", 2))
        self.seen = seen
        self.calls, self.spent = 0, 0.0
        self.stop_requested, self.stop_reason = False, None
        self.lock = threading.Lock()
        self.date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        self.stats = {"done_before": 0, "fetched": 0, "failed": 0, "skipped": 0, "blocked": 0}
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, signum, frame):  # noqa: ANN001
        logging.warning("signal %s: finishing in-flight calls, then stopping", signum)
        self.request_stop(f"signal {signum}")

    def request_stop(self, reason):
        with self.lock:
            if not self.stop_requested:
                self.stop_requested, self.stop_reason = True, reason

    def bump(self, k, n=1):
        with self.lock:
            self.stats[k] += n

    def call(self, params: dict):
        cmd = [TREG, "call", self.ep["id"]]
        for k, v in params.items():
            cmd += ["-p", f"{k}={v}"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT, start_new_session=True)
        except subprocess.TimeoutExpired:
            return None, "timeout", 0.0
        except OSError as e:
            return None, f"oserror {e}", 0.0
        stderr = proc.stderr or ""
        m = re.search(r"charged \$([0-9.]+)", stderr)
        cost = float(m.group(1)) if m else 0.0
        with self.lock:
            self.calls += 1
            self.spent += cost
            calls = self.calls
        if calls % BALANCE_CHECK_EVERY == 0:
            bal = treg_balance()
            logging.info("balance after %d calls: $%s", calls, bal)
            if bal is not None and bal < self.balance_floor:
                self.request_stop(f"balance ${bal:.2f} below floor ${self.balance_floor:.2f}")
        if calls >= self.max_calls:
            self.request_stop(f"call cap reached ({calls})")
        if proc.returncode != 0:
            return None, f"exit={proc.returncode} {stderr.strip()[:300]} {proc.stdout.strip()[:300]}", cost
        try:
            return json.loads(proc.stdout), stderr, cost
        except json.JSONDecodeError:
            return None, f"non-json stdout: {proc.stdout[:300]!r}", cost

    def fetch_task(self, key, group, page, cursor):
        params = build_params(self.endpoint, group["subreddit"], group["query"], group["sort"], group["timeframe"], cursor)
        last_err = ""
        for attempt, wait in enumerate(BACKOFF, 1):
            if self.stop_requested:
                raise Stop(self.stop_reason or "stop")
            resp, info, cost = self.call(params)
            fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            if resp is not None:
                try:
                    posts, next_cursor, has_next = parse_response(self.endpoint, resp)
                except BadResponse as e:
                    last_err = f"bad response: {e}"
                else:
                    append_jsonl(self.raw_dir / self.date / f"search_{group['subreddit']}.ndjson",
                                 {"task_key": key, "endpoint": self.ep["id"], "endpoint_kind": self.endpoint, "params": params,
                                  "subreddit": group["subreddit"], "query": group["query"], "sort": group["sort"].lower(),
                                  "timeframe": group["timeframe"], "page": page, "fetched_at": fetched_at, "cost_usd": cost,
                                  "response": resp})
                    ids = post_ids(posts)
                    with self.lock:
                        new_ids = [i for i in ids if i not in self.seen]
                        self.seen.update(ids)
                    created = post_created(self.endpoint, posts)
                    ev = {"task_key": key, "status": "done", "cursor_used": cursor, "cursor_returned": next_cursor,
                          "has_next": has_next, "n_posts": len(posts), "n_new": len(new_ids), "post_ids": ids,
                          "off_sub": sum(1 for s in post_subreddits(self.endpoint, posts) if s.lower() != group["subreddit"].lower()),
                          "oldest_created": min(created).isoformat(timespec="seconds") if created else None,
                          "newest_created": max(created).isoformat(timespec="seconds") if created else None,
                          "fetched_at": fetched_at, "cost_usd": cost, "attempts": attempt, "error": None}
                    append_jsonl(self.ledger, ev)
                    return ev
            else:
                last_err = info
            logging.warning("attempt %d/%d failed for %s: %s", attempt, len(BACKOFF), key, last_err[:200])
            if attempt == len(BACKOFF):
                break
            for _ in range(wait + (30 if "429" in last_err else 0)):
                if self.stop_requested:
                    raise Stop(self.stop_reason or "stop during backoff")
                time.sleep(1)
        ev = {"task_key": key, "status": "failed", "cursor_used": cursor, "cursor_returned": None, "has_next": None,
              "n_posts": 0, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "attempts": len(BACKOFF), "error": last_err[:500]}
        append_jsonl(self.ledger, ev)
        return ev

    def run_group(self, g, state, retry_failed):
        prev_ids, cursor, low_streak = set(), None, 0
        age_limit = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=float(g["max_age_days"]))) if g.get("max_age_days") else None
        for page, key in enumerate(g["keys"]):
            st = state.get(key)
            if st and st["status"] == "done":
                self.bump("done_before")
                cursor, prev_ids = st.get("cursor_returned"), set(st.get("post_ids") or [])
                if not st.get("has_next"):
                    self._skip_rest(g, page + 1, state, "no_more_pages")
                    return
                if st.get("n_posts") and st.get("n_new") is not None:
                    low_streak = low_streak + 1 if (st["n_new"] / st["n_posts"]) < self.min_novelty else 0
                continue
            if st and st["status"] == "skipped":
                self.bump("skipped")
                self._skip_rest(g, page + 1, state, st.get("reason", "no_more_pages"))
                return
            if st and st["status"] == "failed" and not retry_failed:
                self.bump("failed")
                self.bump("blocked", len(g["keys"]) - page - 1)
                return
            if page > 0 and cursor is None:
                self.bump("blocked", len(g["keys"]) - page)
                return
            if self.dry_run:
                print(f"WOULD FETCH {key}")
                self.bump("fetched")
                continue
            if self.stop_requested:
                raise Stop(self.stop_reason or "stop")
            time.sleep(PACE_SECONDS)
            ev = self.fetch_task(key, g, page, cursor)
            state[key] = ev
            if ev["status"] != "done":
                self.bump("failed")
                self.bump("blocked", len(g["keys"]) - page - 1)
                logging.error("FAILED %s: %s", key, ev["error"])
                return
            self.bump("fetched")
            ids = set(ev.get("post_ids") or [])
            novelty = (ev["n_new"] / ev["n_posts"]) if ev["n_posts"] else 0.0
            logging.info("ok %s posts=%d new=%d has_next=%s calls=%d spent=$%.4f", key, ev["n_posts"], ev["n_new"],
                         ev["has_next"], self.calls, self.spent)
            cursor = ev.get("cursor_returned")
            if ev["n_posts"] == 0:
                self._skip_rest(g, page + 1, state, "empty_page")
                return
            if page > 0 and ids and ids == prev_ids:
                self._skip_rest(g, page + 1, state, "duplicate_page")
                return
            if not ev["has_next"]:
                self._skip_rest(g, page + 1, state, "no_more_pages")
                return
            low_streak = low_streak + 1 if novelty < self.min_novelty else 0
            if low_streak >= self.consecutive_pages:
                self._skip_rest(g, page + 1, state, "adaptive_low_novelty")
                return
            if age_limit and ev.get("newest_created") and dt.datetime.fromisoformat(ev["newest_created"]) < age_limit:
                self._skip_rest(g, page + 1, state, "older_than_max_age")
                return
            prev_ids = ids
        if not self.dry_run:
            logging.warning("page ceiling reached for %s; raise max_pages to continue", g["keys"][-1])

    def _skip_rest(self, g, start, state, reason):
        for key in g["keys"][start:]:
            if state.get(key, {}).get("status") in ("done", "skipped"):
                continue
            ev = {"task_key": key, "status": "skipped", "reason": reason, "n_posts": 0, "error": None,
                  "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
            if not self.dry_run:
                append_jsonl(self.ledger, ev)
            state[key] = ev
            self.bump("skipped")

    def run(self, groups, state, retry_failed, workers):
        if self.dry_run or workers <= 1:
            for g in groups:
                self.run_group(g, state, retry_failed)
            return self.stats
        first_stop = None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self.run_group, g, state, retry_failed): g for g in groups}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Stop as e:
                    first_stop = first_stop or e
                except Exception:  # noqa: BLE001
                    logging.exception("group crashed: %s", futs[fut]["keys"][0])
        if first_stop:
            raise first_stop
        return self.stats


def summarize(state, groups):
    keys = [k for g in groups for k in g["keys"]]
    c = {"total": len(keys), "done": 0, "failed": 0, "skipped": 0, "pending": 0, "posts_rows": 0}
    for k in keys:
        st = state.get(k)
        s = st["status"] if st else "pending"
        c[s] = c.get(s, 0) + 1
        if s == "done":
            c["posts_rows"] += st.get("n_posts", 0)
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--max-calls", type=int, default=5000)
    ap.add_argument("--balance-floor", type=float, default=0.70)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = a.root.resolve()
    for d in ("state", "raw", "logs", "out"):
        (root / d).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(root / "logs" / "search.log"), logging.StreamHandler(sys.stderr)])
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    endpoint, groups = load_plan(cfg)
    ledger = root / "state" / "search_tasks.jsonl"
    state = read_ledger(ledger)
    seen, n_rec = scan_raw(root / "raw", ledger, state)
    if n_rec:
        logging.info("reconciled %d task(s) from raw", n_rec)
    before = summarize(state, groups)
    logging.info("plan: endpoint=%s groups=%d tasks=%s seen_posts=%d", endpoint, len(groups), before, len(seen))
    col = Collector(endpoint, root, a.max_calls, a.balance_floor, a.dry_run, cfg.get("adaptive_stop") or {}, seen)
    if a.dry_run:
        print("dry-run stats:", col.run(groups, state, a.retry_failed, 1))
        return 0
    if not (os.path.exists(TREG) or shutil.which("treg")):
        logging.error("treg CLI not found (set TREG_BIN)")
        return 2
    bal_before = treg_balance()
    logging.info("balance before: $%s", bal_before)
    if bal_before is not None and bal_before < a.balance_floor:
        logging.error("balance below floor; not starting")
        return 2
    started, stop_reason, stats = dt.datetime.now(dt.timezone.utc), "completed", None
    try:
        stats = col.run(groups, state, a.retry_failed, a.workers)
    except Stop as e:
        stop_reason = str(e)
    bal_after, after, ended = treg_balance(), summarize(state, groups), dt.datetime.now(dt.timezone.utc)
    with (root / "out" / "search-sessions.md").open("a", encoding="utf-8") as f:
        f.write(f"\n## search session {started:%Y-%m-%dT%H:%M:%SZ} -> {ended:%Y-%m-%dT%H:%M:%SZ}\n\n"
                f"- endpoint: {ENDPOINTS[endpoint]['id']}\n- calls: {col.calls}, charged: ${col.spent:.4f}\n"
                f"- treg balance: ${bal_before} -> ${bal_after}\n- stop: {stop_reason}\n- before: {before}\n- after: {after}\n"
                f"- stats: {stats or col.stats}\n")
    print(f"calls={col.calls} spent=${col.spent:.4f} stop={stop_reason} after={after}")
    return 0 if stop_reason == "completed" else 3


if __name__ == "__main__":
    sys.exit(main())
