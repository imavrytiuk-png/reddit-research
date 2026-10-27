#!/bin/bash
# First-run check: one line per script. Run from anywhere: bash <skill>/scripts/selftest.sh
cd "$(dirname "$0")" || exit 1
fail=0
run() { local name=$1; shift; if out=$("$@" 2>&1); then echo "ok    $name"; else echo "FAIL  $name"; echo "$out" | tail -5 | sed 's/^/      /'; fail=1; fi; }
run "python3 >= 3.9 (_common.py)"      python3 _common.py
run "evidence_check.py --selftest"      python3 evidence_check.py --selftest
run "aggregate_matrix.py --selftest"    python3 aggregate_matrix.py --selftest
run "gold_score.py --selftest"          python3 gold_score.py --selftest
if command -v uv >/dev/null; then
  run "load_raw.py --selftest (uv)"     uv run load_raw.py --selftest
  run "export_items.py --selftest (uv)" uv run export_items.py --selftest
else
  echo "FAIL  uv not on PATH (needed for load_raw.py and export_items.py): https://docs.astral.sh/uv/"; fail=1
fi
if command -v claude >/dev/null; then echo "ok    claude CLI on PATH"; else echo "FAIL  claude CLI not on PATH (needed for claude_batch.py)"; fail=1; fi
command -v treg >/dev/null && echo "ok    treg CLI on PATH (optional)" || echo "info  treg CLI not on PATH: optional, the paid search fallback is skipped"
exit $fail
