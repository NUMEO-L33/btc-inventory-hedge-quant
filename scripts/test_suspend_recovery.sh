#!/usr/bin/env bash
# Test the sleep-recovery path deterministically — without actually putting the MacBook to sleep.
#
#   bash scripts/test_suspend_recovery.sh
#
# Why this is needed: caffeinate is preventing sleep, so the fix for the 2026-09-05 incident has
# never once triggered in the field. If caffeinate holds out even with the lid closed, nothing
# gets tested.
#
# How it works: what sleep does to a process is 'freeze' it, and SIGSTOP does exactly the same.
# Freezing it for 90 s and then waking it must trigger the stall detection in _reconnect_reason
# (threshold 60 s) just as sleep would.
# Shadow is a paper account, so real orders are structurally impossible and there is no risk.

set -uo pipefail
cd "$(dirname "$0")/.."

LOG=state/shadow.log
[[ -f "$LOG" ]] || { echo "FAILED: $LOG does not exist. Is the agent installed?" >&2; exit 1; }

# caffeinate wraps python, so pgrep catches both. Pick the right one by its arguments —
# on macOS `ps -o comm=` gives the full path and is sometimes truncated, so the name can't be trusted.
PID=""
for p in $(pgrep -f "btc_inventory_quant" 2>/dev/null); do
  args=$(ps -o args= -p "$p" 2>/dev/null)
  case "$args" in
    *caffeinate*) : ;;                       # skip the wrapper
    *btc_inventory_quant*) PID="$p"; break ;;
  esac
done
[[ -n "$PID" ]] || {
  echo "FAILED: could not find the shadow python process." >&2
  echo >&2
  echo "--- related processes right now (send this back exactly as shown) ---" >&2
  ps -eo pid,ppid,args | grep "[b]tc_inventory_quant" >&2 || echo "  (none)" >&2
  echo "--- launchd agents ---" >&2
  launchctl list | grep numeo >&2 || echo "  (not in the list — the agent is dead)" >&2
  exit 1
}

before=$(grep -c "forcing stream reconnect" "$LOG" 2>/dev/null || true)
before=${before:-0}
echo "shadow PID : $PID"
echo "log so far : ${before} forced reconnects"
echo

echo "Stopping the process for 90 s (the same state as sleep)..."
kill -STOP "$PID" || { echo "FAILED: SIGSTOP failed" >&2; exit 1; }
# Always wake it, even if this script dies for whatever reason
trap 'kill -CONT "$PID" 2>/dev/null || true' EXIT INT TERM
sleep 90
kill -CONT "$PID"
trap - EXIT INT TERM
echo "Woken. Waiting 20 s for detection and reconnect..."
sleep 20

after=$(grep -c "forcing stream reconnect" "$LOG" 2>/dev/null || true)
after=${after:-0}
echo
if (( after > before )); then
  echo "PASS — detected the stall and forced a reconnect (${before} -> ${after} reconnects)"
  grep "forcing stream reconnect" "$LOG" | tail -2
else
  echo "FAIL — did not detect the 90 s stall (${before} -> ${after} reconnects)"
  echo "  This means the fix does not work. Send this output back exactly as shown."
fi

echo
echo "State after the stall:"
.venv/bin/python -m btc_inventory_quant status --config configs/shadow.yaml 2>/dev/null |
  python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('  could not read status'); raise SystemExit(0)
m = d.get('metrics', {})
print('  system_state :', d.get('system_state'), '| reason:', d.get('safe_hold_reason') or '-')
print('  ws_connected :', m.get('websocket_connected'), '| contracts:', m.get('short_contracts'))
"
echo
echo "If SAFE_HOLD engaged, that is a result too — it was fixed not to latch, so it should not engage."
