#!/usr/bin/env bash
# 절전 복구 경로를 결정적으로 시험한다 — 맥북을 실제로 재우지 않고.
#
#   bash scripts/test_suspend_recovery.sh
#
# 왜 이게 필요한가: caffeinate 가 절전을 막고 있어서 2026-09-05 인시던트의 수정이
# 현장에서 한 번도 발동하지 않았다. 뚜껑을 닫아도 caffeinate 가 버티면 시험이 안 된다.
#
# 원리: 절전이 프로세스에 하는 일은 '얼리는 것'이고 SIGSTOP 이 똑같은 일을 한다.
# 90초 얼렸다 깨우면 _reconnect_reason 의 정지 감지(임계값 60초)가 그대로 발동해야 한다.
# Shadow 는 페이퍼 계좌라 실주문이 구조적으로 불가능하므로 위험이 없다.

set -uo pipefail
cd "$(dirname "$0")/.."

LOG=state/shadow.log
[[ -f "$LOG" ]] || { echo "FAILED: $LOG 가 없다. 에이전트가 설치돼 있는가?" >&2; exit 1; }

# caffeinate 가 파이썬을 감싸고 있으므로 pgrep 은 둘 다 잡는다. 인자로 골라낸다 —
# macOS 의 `ps -o comm=` 은 전체 경로를 주고 폭이 잘리기도 해서 이름 기준은 못 믿는다.
PID=""
for p in $(pgrep -f "btc_inventory_quant" 2>/dev/null); do
  args=$(ps -o args= -p "$p" 2>/dev/null)
  case "$args" in
    *caffeinate*) : ;;                       # 감싸는 쪽은 건너뛴다
    *btc_inventory_quant*) PID="$p"; break ;;
  esac
done
[[ -n "$PID" ]] || {
  echo "FAILED: shadow 파이썬 프로세스를 찾지 못했다." >&2
  echo >&2
  echo "--- 현재 관련 프로세스 (이 내용을 그대로 알려달라) ---" >&2
  ps -eo pid,ppid,args | grep "[b]tc_inventory_quant" >&2 || echo "  (없음)" >&2
  echo "--- launchd 에이전트 ---" >&2
  launchctl list | grep numeo >&2 || echo "  (목록에 없음 — 에이전트가 죽었다)" >&2
  exit 1
}

before=$(grep -c "forcing stream reconnect" "$LOG" 2>/dev/null || true)
before=${before:-0}
echo "shadow PID : $PID"
echo "현재 기록  : 강제 재연결 ${before}건"
echo

echo "프로세스를 90초 정지시킨다 (절전과 같은 상태)..."
kill -STOP "$PID" || { echo "FAILED: SIGSTOP 실패" >&2; exit 1; }
# 어떤 이유로 스크립트가 죽어도 반드시 깨운다
trap 'kill -CONT "$PID" 2>/dev/null || true' EXIT INT TERM
sleep 90
kill -CONT "$PID"
trap - EXIT INT TERM
echo "깨웠다. 감지·재연결까지 20초 대기..."
sleep 20

after=$(grep -c "forcing stream reconnect" "$LOG" 2>/dev/null || true)
after=${after:-0}
echo
if (( after > before )); then
  echo "PASS — 정지를 감지하고 강제 재연결했다 (${before} -> ${after}건)"
  grep "forcing stream reconnect" "$LOG" | tail -2
else
  echo "FAIL — 90초 정지를 감지하지 못했다 (${before} -> ${after}건)"
  echo "  수정이 작동하지 않는다는 뜻이다. 이 출력을 그대로 알려달라."
fi

echo
echo "정지 이후 상태:"
.venv/bin/python -m btc_inventory_quant status --config configs/shadow.yaml 2>/dev/null |
  python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('  status 를 읽지 못했다'); raise SystemExit(0)
m = d.get('metrics', {})
print('  system_state :', d.get('system_state'), '| reason:', d.get('safe_hold_reason') or '-')
print('  ws_connected :', m.get('websocket_connected'), '| 계약:', m.get('short_contracts'))
"
echo
echo "SAFE_HOLD 가 걸렸다면 그것도 결과다 — 래치하지 않도록 고쳤으므로 걸리면 안 된다."
