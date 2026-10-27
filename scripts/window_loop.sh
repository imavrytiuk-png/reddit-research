#!/bin/bash
# macOS only (caffeinate, BSD date). Started by the OWNER in their own terminal with the
# command the agent prepared; the agent reads logs/window_loop.log afterwards.
# Re-run a labelling command after each 5-hour subscription window reset until it
# exits 0 (nothing pending). The command must exit 3 when it stops on a limit
# (claude_batch.py does). Attempts at +2, +7, +17 minutes after each reset; an
# attempt counts when the command exits 0 or made progress (exit 3 after some
# batches). Stop any time: `touch STOP` in the current folder, or kill the pid in
# state/window_loop.pid. `caffeinate` keeps the Mac awake while this runs.
#
# Usage: ./window_loop.sh HH:MM [--window-hours 5] -- <command...>
#   HH:MM  local time of the NEXT reset (compute it; do not poll the limit)
# Example:
#   ./window_loop.sh 00:52 -- python3 claude_batch.py --items items.ndjson --prompt p.md \
#       --schema s.json --model claude-sonnet-5-5 --effort low --model-tag S-v1 --max-minutes 290
set -u
RESET=${1:?next reset HH:MM}; shift
WINDOW_H=5
if [ "${1:-}" = "--window-hours" ]; then WINDOW_H=$2; shift 2; fi
[ "${1:-}" = "--" ] && shift
[ $# -gt 0 ] || { echo "no command given after --" >&2; exit 1; }
CMD=("$@")
RETRY_MIN=(2 7 17)
mkdir -p logs state
echo $$ > state/window_loop.pid
caffeinate -i -s -w $$ &
log() { echo "$(date '+%F %T') $*" | tee -a logs/window_loop.log; }
next_reset=$(date -j -f "%Y-%m-%d %H:%M" "$(date +%Y-%m-%d) $RESET" +%s)
[ "$next_reset" -le "$(date +%s)" ] && next_reset=$((next_reset + 86400))
log "loop started; next reset $(date -r "$next_reset" '+%F %H:%M'); command: ${CMD[*]}"
while true; do
  [ -f STOP ] && { log "STOP file found, exiting"; rm -f state/window_loop.pid; exit 0; }
  progressed=0
  for m in "${RETRY_MIN[@]}"; do
    at=$((next_reset + m * 60)); s=$((at - $(date +%s)))
    [ "$s" -gt 0 ] && { log "sleeping ${s}s until $(date -r "$at" '+%H:%M')"; sleep "$s"; }
    [ -f STOP ] && { log "STOP file found, exiting"; rm -f state/window_loop.pid; exit 0; }
    alog="logs/window_$(date +%m%d_%H%M).log"
    log "attempt +${m}m, log $alog"
    "${CMD[@]}" > "$alog" 2>&1; rc=$?
    n=$(grep -c '^batch .* done' "$alog")
    log "attempt finished rc=$rc batches_done=$n"
    if [ "$rc" -eq 0 ]; then log "nothing pending: all done"; rm -f state/window_loop.pid; exit 0; fi
    if grep -qi "weekly" "$alog"; then log "WEEKLY limit: stopping"; touch STOP; rm -f state/window_loop.pid; exit 0; fi
    if [ "$n" -gt 0 ]; then progressed=1; break; fi
  done
  [ "$progressed" -eq 0 ] && log "no batch completed this window; trying at the next reset"
  next_reset=$((next_reset + WINDOW_H * 3600))
done
