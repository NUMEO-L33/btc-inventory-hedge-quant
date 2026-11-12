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

[[ -f .env ]] || fail ".env does not exist"

TOKEN="$(sed -n 's/^BIQ_TELEGRAM_BOT_TOKEN=//p' .env | head -1 | tr -d " \t\r\n")"
[[ -n "$TOKEN" ]] || fail "BIQ_TELEGRAM_BOT_TOKEN in .env is empty"

echo "1/3 checking the bot..."
curl -fsS "https://api.telegram.org/bot$TOKEN/getMe" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if not d.get("ok"):
    sys.exit("FAILED: the token was rejected — " + str(d.get("description")))
print("    bot: @" + d["result"]["username"])
' || fail "bot check failed (wrong token or a network problem)"

echo "2/3 looking up chat_id..."
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
  echo "    The bot has not received any messages yet."
  echo "    In Telegram, find the bot above, open a chat with it and send /start, then run this script again."
  echo "    (A bot cannot start a conversation. A chat_id only exists once a person has sent it a message first.)"
  exit 1
fi
echo "    chat_id: $CHAT"

echo "3/3 writing .env + sending a test message..."
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
p.write_text("\n".join(lines) + "\n", encoding="utf-8")  # keeps the existing permissions (600)
print("    .env updated")
PY

curl -fsS -X POST "https://api.telegram.org/bot$TOKEN/sendMessage" \
  -d "chat_id=$CHAT" \
  -d "text=[TEST] btc-inventory-quant alert channel connected. From now on SAFE_HOLD, block and stream-restart alerts will arrive here." \
  >/dev/null || fail "sending the test message failed"

echo
echo "Done. Check that the test message arrived in Telegram."
echo "If it did, restart the agent so the new settings take effect:"
echo "  bash scripts/install_shadow_agent.sh --uninstall && bash scripts/install_shadow_agent.sh"
