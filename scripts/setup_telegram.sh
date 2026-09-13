#!/usr/bin/env bash
# Finish the Telegram alert channel: find the chat id, write it to .env, send a test alert.
#
#   bash scripts/setup_telegram.sh
#
# The bot token is read from .env and passed to curl in a variable. It is never printed and
# never typed on the command line, so it stays out of the terminal scrollback and out of the
# shell history file.

set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "FAILED: $*" >&2; exit 1; }

[[ -f .env ]] || fail ".env 가 없습니다"

TOKEN="$(sed -n 's/^BIQ_TELEGRAM_BOT_TOKEN=//p' .env | head -1 | tr -d " \t\r\n")"
[[ -n "$TOKEN" ]] || fail ".env 의 BIQ_TELEGRAM_BOT_TOKEN 이 비어 있습니다"

echo "1/3 봇 확인..."
curl -fsS "https://api.telegram.org/bot$TOKEN/getMe" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if not d.get("ok"):
    sys.exit("FAILED: 토큰이 거부됐습니다 — " + str(d.get("description")))
print("    봇: @" + d["result"]["username"])
' || fail "봇 확인 실패 (토큰이 틀렸거나 네트워크 문제)"

echo "2/3 chat_id 조회..."
CHAT="$(curl -fsS "https://api.telegram.org/bot$TOKEN/getUpdates" | python3 -c '
import json, sys
d = json.load(sys.stdin)
ids = []
for u in d.get("result", []):
    m = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
    c = m.get("chat", {}).get("id")
    if c is not None and c not in ids:
        ids.append(c)
print(ids[-1] if ids else "")
')"

if [[ -z "$CHAT" ]]; then
  echo
  echo "    아직 봇에게 온 메시지가 없습니다."
  echo "    텔레그램에서 위 봇을 찾아 대화를 열고 /start 를 보낸 뒤, 이 스크립트를 다시 실행하세요."
  echo "    (봇은 먼저 말을 걸 수 없습니다. 사람이 먼저 보내야 chat_id 가 생깁니다.)"
  exit 1
fi
echo "    chat_id: $CHAT"

echo "3/3 .env 기록 + 테스트 발송..."
CHAT="$CHAT" python3 - <<'PY'
import os
from pathlib import Path

chat = os.environ["CHAT"]
p = Path(".env")
lines = p.read_text(encoding="utf-8").splitlines()
for i, line in enumerate(lines):
    if line.startswith("BIQ_TELEGRAM_CHAT_ID="):
        lines[i] = f"BIQ_TELEGRAM_CHAT_ID={chat}"
        break
else:
    lines.append(f"BIQ_TELEGRAM_CHAT_ID={chat}")
p.write_text("\n".join(lines) + "\n", encoding="utf-8")  # 기존 권한(600) 유지
print("    .env 갱신 완료")
PY

curl -fsS -X POST "https://api.telegram.org/bot$TOKEN/sendMessage" \
  -d "chat_id=$CHAT" \
  -d "text=[TEST] btc-inventory-quant 알림 채널이 연결되었습니다. 앞으로 SAFE_HOLD·차단·스트림 재시작이 여기로 옵니다." \
  >/dev/null || fail "테스트 메시지 발송 실패"

echo
echo "완료. 텔레그램에 테스트 메시지가 도착했는지 확인하세요."
echo "도착했다면 에이전트를 재시작해야 새 설정이 적용됩니다:"
echo "  bash scripts/install_shadow_agent.sh --uninstall && bash scripts/install_shadow_agent.sh"
