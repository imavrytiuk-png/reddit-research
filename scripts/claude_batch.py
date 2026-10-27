#!/usr/bin/env python3
"""Headless labelling runner: items NDJSON + prompt file + JSON schema -> results NDJSON,
through `claude -p` with the minimal invocation that costs ~350 tokens of overhead per
call instead of ~30k (no default system prompt, tools, MCP, skills, settings, sessions).

Input  <items>: one JSON object per line with an "id" (or --id-key) and whatever the
       prompt expects (title, text, hints...). The whole object is sent to the model.
Prompt <prompt.md>: the task instructions. A first line `prompt_version: xyz` is stripped
       and recorded. Sent as --system-prompt, byte-identical on every call, so it is cached;
       the user turn carries only the batch.
Items: only --send-keys (default id,title,text,hints,post_title) reach the model; subreddit,
author_hash, slice and weight stay local, so the prompt's "never infer from the subreddit"
holds by construction. --model must be a name the CLI accepts today (check `claude --help`
or a one-line call); the names in these examples are illustrative.
Schema <schema.json>: JSON schema of the whole response {"results": [...]}; each result must
       echo the item id under --id-key.

Discipline:
- batches by character budget (--char-budget), long items alone (--long-threshold),
  shuffled inside the batch;
- the raw CLI output is appended (flush+fsync) to <root>/raw_llm/<date>/<tag>_batch_<n>.ndjson
  BEFORE parsing, so results are rebuildable without new calls;
- returned id set must equal the batch id set, else one retry with a reminder, then split
  in halves recursively down to single items, which are marked failed with the reason;
- ledger <root>/state/<tag>_tasks.jsonl, append-only, last line per id wins; re-running the
  same command resumes (done ids skipped; --retry-failed includes failed ones);
- usage/rate-limit notices are detected ONLY on responses without model output (item text
  quotes things like "hit your 90-day limit"), then the run stops cleanly with exit 3 after
  in-flight batches persist; SIGINT/SIGTERM do the same; --max-minutes ends the run in time;
- MAX_THINKING_TOKENS=0 for Haiku (it ignores --effort and may burn 17k thinking tokens);
  --effort low for Sonnet/Opus; never both; --max-turns 4 because --json-schema takes 2-3
  internal turns.

Results: <root>/out/<tag>_results.ndjson, one line per item:
  {"id", "model_tag", "prompt_version", "model", "batch", "result": {...}, "labelled_at"}
Run evidence_check.py on it next. Exit codes: 0 nothing pending / all done, 3 stopped
(limit, signal, time, or the --max-calls cap: default 1000 model calls per invocation, the
same command resumes), 1 error.

Usage:
  python3 claude_batch.py --items items.ndjson --prompt prompt.md --schema schema.json \
      --model claude-sonnet-5-5 --effort low --model-tag S-v1 --workers 8 --limit 500 --root .
  python3 claude_batch.py ... --model claude-haiku-5-5 --max-thinking-tokens 0
  python3 claude_batch.py ... --dry-run      # print batches and the command, no calls
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

RATE_LIMIT_RE = re.compile(
    r"usage limit|rate limit|quota exceeded|too many requests|session limit|weekly limit|"
    r"\d+-hour limit|limit reached|hit your .{0,20}limit", re.IGNORECASE)
HINTS = [  # (pattern in the failure reason, the pitfalls.md entry it maps to)
    (re.compile(r"max_turns", re.I),
     "pitfall (Headless model calls): the --json-schema pass takes extra turns; keep --max-turns 4. "
     "This is not a thinking problem."),
    (re.compile(r"nested|CLAUDECODE|inside (a |another )?claude", re.I),
     "pitfall (Headless model calls): CLAUDECODE is set in the child environment; the nested CLI refuses to run."),
    (re.compile(r"bad outer JSON", re.I),
     "pitfall (Headless model calls): no JSON envelope came back; check the minimal flags and run one batch with --limit 1."),
    (re.compile(r"exit 1\b", re.I),
     "pitfall (Headless model calls): exit 1 on a small model usually means thinking is on; use --max-thinking-tokens 0, never --effort."),
]
REMINDER = ("\n\nReminder: output ONLY the JSON object, no prose, no markdown fences. "
            "Return exactly one result per id given, no more, no fewer.")

_stop = threading.Event()
_write_lock = threading.Lock()
_deadline: float | None = None


class RateLimitHit(Exception):
    pass


def _on_signal(signum, frame):  # noqa: ANN001
    print(f"[signal {signum}] stopping after in-flight batches finish...", file=sys.stderr)
    _stop.set()


def time_up() -> bool:
    return _deadline is not None and time.monotonic() >= _deadline


# ---------------------------------------------------------------- io
def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _write_lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def read_ndjson(path: Path) -> list[dict]:
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_prompt(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    version = "unversioned"
    first, _, rest = text.partition("\n")
    m = re.match(r"\s*prompt_version:\s*(\S+)\s*$", first)
    if m:
        version, text = m.group(1), rest.lstrip("\n")
    return text, version


def read_ledger(path: Path) -> dict[str, dict]:
    state: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    ev = json.loads(line)
                    state[ev["id"]] = ev
                except (json.JSONDecodeError, KeyError):
                    continue
    return state


# ---------------------------------------------------------------- batching
def build_batches(items: list[dict], char_budget: int, long_threshold: int, seed: int) -> list[list[dict]]:
    def plen(p):
        return len(json.dumps(p, ensure_ascii=False))
    batches, cur, cur_len = [], [], 0
    for p in items:
        n = plen(p)
        if n >= long_threshold:
            if cur:
                batches.append(cur)
                cur, cur_len = [], 0
            batches.append([p])
            continue
        if cur and cur_len + n > char_budget:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += n
    if cur:
        batches.append(cur)
    rng = random.Random(seed)
    for b in batches:
        rng.shuffle(b)
    return batches


# ---------------------------------------------------------------- claude call
def call_claude(prompt: str, system_prompt: str, schema: dict, model: str, effort: str | None,
                max_thinking_tokens: int | None, timeout: int, label: str) -> dict:
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # a nested CLI refuses to start inside a session otherwise
    if max_thinking_tokens is not None:
        env["MAX_THINKING_TOKENS"] = str(max_thinking_tokens)
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--system-prompt", system_prompt,
           "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--disable-slash-commands", "--no-session-persistence", "--max-turns", "4",
           "--json-schema", json.dumps(schema)]
    if effort:
        cmd += ["--effort", effort]
    cmd.append(prompt)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"[{label}] timeout after {timeout}s")
    wall = time.monotonic() - t0
    combined = (proc.stdout or "") + (proc.stderr or "")
    try:
        outer = json.loads(proc.stdout) if proc.stdout else None
    except json.JSONDecodeError:
        outer = None
    if isinstance(outer, dict):
        usage = outer.get("usage") or {}
        produced = (usage.get("output_tokens") or 0) > 0 or outer.get("structured_output") is not None
        if proc.returncode == 0 and not outer.get("is_error") and produced:
            outer["_wall_seconds"] = wall
            return outer  # a real answer: never scan it for limit phrases
        if RATE_LIMIT_RE.search(combined):
            raise RateLimitHit(combined[:500])
        if outer.get("is_error") or proc.returncode != 0:
            raise RuntimeError(f"[{label}] CLI error (exit {proc.returncode}): {combined[:300]}")
        outer["_wall_seconds"] = wall
        return outer
    if RATE_LIMIT_RE.search(combined):
        raise RateLimitHit(combined[:500])
    if proc.returncode != 0:
        raise RuntimeError(f"[{label}] exit {proc.returncode}: {combined[:800]}")
    raise RuntimeError(f"[{label}] bad outer JSON: {proc.stdout[:300]}")


def extract_results(outer: dict) -> list[dict]:
    so = outer.get("structured_output")
    if isinstance(so, dict) and isinstance(so.get("results"), list):
        return so["results"]
    text = (outer.get("result") or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    parsed = json.loads(m.group(1) if m else text)
    items = parsed.get("results")
    if not isinstance(items, list) or not items:
        raise ValueError("empty/invalid results array")
    return items


# ---------------------------------------------------------------- runner
class Runner:
    def __init__(self, a, system_prompt: str, prompt_version: str, schema: dict):
        self.a, self.system_prompt, self.prompt_version, self.schema = a, system_prompt, prompt_version, schema
        self.root = a.root.resolve()
        self.ledger = self.root / "state" / f"{a.model_tag}_tasks.jsonl"
        self.results_path = self.root / "out" / f"{a.model_tag}_results.ndjson"
        self.date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        self.lock = threading.Lock()
        self.hints_shown: set[str] = set()
        self.stats = {"calls": 0, "wall_seconds": 0.0, "input_tokens": 0, "output_tokens": 0, "cache_read": 0,
                      "cache_creation": 0, "cost_usd": 0.0, "items_done": 0, "items_failed": 0, "batches_done": 0}

    def write_raw(self, batch_n, obj: dict):
        append_jsonl(self.root / "raw_llm" / self.date / f"{self.a.model_tag}_batch_{batch_n}.ndjson", obj)

    def record(self, outer: dict):
        u = outer.get("usage") or {}
        with self.lock:
            self.stats["calls"] += 1
            self.stats["wall_seconds"] += outer.get("_wall_seconds", 0)
            self.stats["input_tokens"] += u.get("input_tokens", 0) or 0
            self.stats["output_tokens"] += u.get("output_tokens", 0) or 0
            self.stats["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            self.stats["cache_creation"] += u.get("cache_creation_input_tokens", 0) or 0
            self.stats["cost_usd"] += outer.get("total_cost_usd", 0) or 0

    def calls_capped(self) -> bool:
        return bool(self.a.max_calls) and self.stats["calls"] >= self.a.max_calls

    def persist(self, items: list[dict], batch_n):
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for it in items:
            iid = it[self.a.id_key]
            append_jsonl(self.results_path, {"id": iid, "model_tag": self.a.model_tag, "prompt_version": self.prompt_version,
                                             "model": self.a.model, "batch": str(batch_n), "result": it, "labelled_at": now})
            append_jsonl(self.ledger, {"id": iid, "status": "done", "batch": str(batch_n), "model_tag": self.a.model_tag,
                                       "prompt_version": self.prompt_version, "at": now})
        with self.lock:
            self.stats["items_done"] += len(items)
            self.stats["batches_done"] += 1

    def mark_failed(self, ids: list[str], reason: str):
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for iid in ids:
            append_jsonl(self.ledger, {"id": iid, "status": "failed", "model_tag": self.a.model_tag,
                                       "prompt_version": self.prompt_version, "error": reason[:300], "at": now})
        with self.lock:
            self.stats["items_failed"] += len(ids)
        hint = next((h for rx, h in HINTS if rx.search(reason)), None)
        if hint and hint not in self.hints_shown:
            self.hints_shown.add(hint)
            print(f"HINT: {hint}", file=sys.stderr)

    def process_batch(self, batch_n, items: list[dict]):
        ids = [it[self.a.id_key] for it in items]
        label = f"batch_{batch_n}"
        prompt = json.dumps(items, ensure_ascii=False)
        reason = None
        for attempt, extra in enumerate(["", REMINDER]):
            if _stop.is_set() or time_up() or self.calls_capped():
                _stop.set()
                return
            try:
                outer = call_claude(prompt + extra, self.system_prompt, self.schema, self.a.model, self.a.effort,
                                    self.a.max_thinking_tokens, self.a.timeout, label)
            except RateLimitHit:
                raise
            except RuntimeError as e:
                self.write_raw(batch_n, {"batch": str(batch_n), "ids": ids, "attempt": attempt, "error": str(e),
                                         "at": dt.datetime.now(dt.timezone.utc).isoformat()})
                reason = f"request error: {e}"
                continue
            self.write_raw(batch_n, {"batch": str(batch_n), "ids": ids, "attempt": attempt, "outer": outer,
                                     "at": dt.datetime.now(dt.timezone.utc).isoformat()})
            self.record(outer)
            try:
                results = extract_results(outer)
            except Exception as e:  # noqa: BLE001
                reason = f"parse failure: {e}"
                continue
            returned = {r.get(self.a.id_key) for r in results if isinstance(r, dict)}
            if returned != set(ids):
                reason = f"id-set mismatch: missing={set(ids) - returned} extra={returned - set(ids)}"
                continue
            self.persist([r for r in results if isinstance(r, dict)], batch_n)
            print(f"batch {batch_n} done: {len(items)} items, ${outer.get('total_cost_usd', 0) or 0:.4f}, "
                  f"{outer.get('_wall_seconds', 0):.0f}s", file=sys.stderr)
            return
        if len(items) > 1:
            mid = len(items) // 2
            self.process_batch(f"{batch_n}a", items[:mid])
            self.process_batch(f"{batch_n}b", items[mid:])
        else:
            self.mark_failed(ids, reason or "unknown failure after retry")
            print(f"batch {batch_n} FAILED: {reason}", file=sys.stderr)


def main() -> int:
    global _deadline
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items", type=Path, required=True)
    ap.add_argument("--prompt", type=Path, required=True)
    ap.add_argument("--schema", type=Path, required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--model-tag", required=True, help="run tag: ledger, raw and results are keyed by it")
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    ap.add_argument("--max-thinking-tokens", type=int, default=None, help="0 disables thinking (Haiku)")
    ap.add_argument("--id-key", default="id")
    ap.add_argument("--send-keys", default="id,title,text,hints,post_title",
                    help="item fields the model sees; everything else (subreddit, author_hash, slice, weight) stays local")
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--char-budget", type=int, default=12000)
    ap.add_argument("--long-threshold", type=int, default=6000)
    ap.add_argument("--limit", type=int, default=None, help="label at most N pending items (in file order)")
    ap.add_argument("--ids-file", type=Path, default=None, help="only these ids, one per line")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--max-calls", type=int, default=1000, help="stop (exit 3) after N model calls in this invocation; 0 = no cap")
    ap.add_argument("--timeout", type=int, default=240, help="seconds per CLI call")
    ap.add_argument("--seed", type=int, default=20260101)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.effort and a.max_thinking_tokens == 0:
        ap.error("--effort cannot be combined with --max-thinking-tokens 0 (the CLI rejects it)")

    system_prompt, version = load_prompt(a.prompt)
    schema = json.loads(a.schema.read_text(encoding="utf-8"))
    items = read_ndjson(a.items)
    runner = Runner(a, system_prompt, version, schema)
    for d in ("state", "raw_llm", "out", "logs"):
        (runner.root / d).mkdir(parents=True, exist_ok=True)
    ledger = read_ledger(runner.ledger)
    skip = {i for i, ev in ledger.items() if ev.get("status") == "done" or (ev.get("status") == "failed" and not a.retry_failed)}
    wanted = None
    if a.ids_file:
        wanted = {ln.strip().split("\t")[0] for ln in a.ids_file.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    pending = [it for it in items if it[a.id_key] not in skip and (wanted is None or it[a.id_key] in wanted)]
    send = {k for k in a.send_keys.split(",") if k} | {a.id_key}
    pending = [{k: v for k, v in it.items() if k in send} for it in pending]  # the prompt says "never infer from the subreddit"
    if a.limit is not None:
        pending = pending[:a.limit]
    print(f"items={len(items)} done_before={sum(1 for e in ledger.values() if e.get('status')=='done')} "
          f"pending_now={len(pending)} tag={a.model_tag} prompt={version} model={a.model}", file=sys.stderr)
    if not pending:
        print("pending=0", file=sys.stderr)
        return 0
    batches = build_batches(pending, a.char_budget, a.long_threshold, a.seed)
    if a.dry_run:
        print(f"{len(batches)} batches; sizes: {[len(b) for b in batches][:30]}{' ...' if len(batches) > 30 else ''}")
        print("command: claude -p --model", a.model, "--output-format json --system-prompt <prompt> --tools '' "
              "--setting-sources '' --strict-mcp-config --mcp-config '{\"mcpServers\":{}}' --disable-slash-commands "
              "--no-session-persistence --max-turns 4 --json-schema <schema>", f"--effort {a.effort}" if a.effort else "",
              "<batch JSON>")
        return 0

    if not shutil.which("claude"):
        sys.exit("the `claude` CLI is not on PATH: install Claude Code and log in (subscription or ANTHROPIC_API_KEY), then re-run")
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if a.max_minutes:
        _deadline = time.monotonic() + a.max_minutes * 60
    started = time.monotonic()
    stop_reason = "completed"
    limit_hit = None
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        futs = {pool.submit(runner.process_batch, n, b): n for n, b in enumerate(batches)}
        for fut in as_completed(futs):
            try:
                fut.result()
            except RateLimitHit as e:
                limit_hit = limit_hit or str(e)
                _stop.set()
            except Exception as e:  # noqa: BLE001
                print(f"batch {futs[fut]} crashed: {e}", file=sys.stderr)
    if limit_hit:
        stop_reason = f"usage limit: {limit_hit[:160]}"
    elif _stop.is_set():
        stop_reason = "time cap" if time_up() else ("calls cap" if runner.calls_capped() else "signal")
    s = runner.stats
    summary = (f"stats: stop={stop_reason} calls={s['calls']} items_done={s['items_done']} items_failed={s['items_failed']} "
               f"batches_done={s['batches_done']} wall_s={s['wall_seconds']:.0f} run_s={time.monotonic() - started:.0f} "
               f"cost_usd={s['cost_usd']:.4f} in_tok={s['input_tokens']} out_tok={s['output_tokens']} "
               f"cache_read={s['cache_read']} cache_create={s['cache_creation']}")
    print(summary, file=sys.stderr)
    if stop_reason == "calls cap":
        print(f"HINT: --max-calls {a.max_calls} reached; re-run the same command to continue (0 = no cap)", file=sys.stderr)
    with (runner.root / "logs" / f"{a.model_tag}_sessions.md").open("a", encoding="utf-8") as f:
        f.write(f"\n- {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {summary}\n")
    if "weekly" in (limit_hit or "").lower():
        print("WEEKLY limit reached: stop everything, do not retry at the next window", file=sys.stderr)
    elif limit_hit:
        print("HINT: pitfall (Headless model calls): a limit message on a response without output; "
              "subscription: resume at the next window (window_loop.sh); API: check the account.", file=sys.stderr)
    if s["items_failed"]:
        print(f"HINT: {s['items_failed']} item(s) failed; the reasons are in state/{a.model_tag}_tasks.jsonl. "
              "grep references/pitfalls.md for the error text before --retry-failed.", file=sys.stderr)
    return 0 if stop_reason == "completed" else 3


if __name__ == "__main__":
    sys.exit(main())
