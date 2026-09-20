#!/usr/bin/env bash
# Install (or reinstall) the launchd agent that keeps one runtime mode running on macOS.
#
#   bash scripts/install_agent.sh shadow             # install and start (paper account, no keys)
#   bash scripts/install_agent.sh testnet            # install and start (Binance DEMO account, demo keys)
#   bash scripts/install_agent.sh shadow --uninstall
#
# The two agents are independent: separate label, config, database, log. They can run side by
# side - shadow proves the decision/recovery layer on a simulated account, testnet proves the real
# adapter path (signed orders, exchange-assigned ids, user stream) on a demo account. Neither can
# reach a production account: shadow has no keys at all, testnet only demo keys.
#
# The agent starts at login and restarts the process if it dies. With `caffeinate -s` it also
# keeps the Mac awake while on mains power (see the plist comment).

set -euo pipefail

MODE="${1:?usage: install_agent.sh <shadow|testnet> [--uninstall]}"
case "$MODE" in
  shadow|testnet) ;;
  *) echo "unknown mode: $MODE (shadow|testnet)" >&2; exit 2 ;;
esac
LABEL="com.numeo.btc-inventory-quant.$MODE"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="configs/$MODE.yaml"

if [[ "${2:-}" == "--uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled $LABEL"
  exit 0
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "launchd is macOS-only. On Linux use 'docker compose up -d $MODE' or a systemd unit." >&2
  exit 1
fi

# --- preflight: fail loudly now rather than silently in a log file for two weeks ---------------
fail() { echo "FAILED: $*" >&2; exit 1; }

[[ -x "$REPO/.venv/bin/python" ]] || fail "no venv at $REPO/.venv — run: python3.12 -m venv .venv && pip install -e '.[dev]'"
[[ -f "$REPO/$CONFIG" ]] || fail "$CONFIG is missing"
[[ -f "$REPO/scripts/launchd/$LABEL.plist" ]] || fail "scripts/launchd/$LABEL.plist is missing"
mkdir -p "$REPO/state" "$HOME/Library/LaunchAgents"

"$REPO/.venv/bin/python" - "$REPO" "$MODE" <<'PY' || fail "$MODE config did not load — fix it before installing the agent"
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
from btc_inventory_quant.config.loader import load_config
from btc_inventory_quant.config.settings import Settings

mode = sys.argv[2]
cfg = load_config(Path(sys.argv[1]) / "configs" / f"{mode}.yaml")
if cfg.mode.value != mode:
    raise SystemExit(f"configs/{mode}.yaml declares mode {cfg.mode.value}, expected {mode}")
ratio = getattr(cfg.strategy.fixed_hedge, "ratio", None)
channels = [c.value for c in cfg.monitoring.alert_channels]
account = "paper account" if cfg.exchange.name.value == "paper" else f"{cfg.exchange.environment} account"
print(f"  mode      : {cfg.mode.value}")
print(f"  strategy  : {cfg.strategy.name}" + (f" @ {ratio}" if ratio is not None else ""))
print(f"  exchange  : {cfg.exchange.name.value} ({cfg.exchange.environment}) — {account}")
print(f"  database  : {cfg.storage.database_url}")
print(f"  alerts    : {', '.join(channels)}")

s = Settings()
if mode == "testnet":
    # testnet places real (demo) orders, so missing keys are fatal, not a warning
    if not (s.binance_demo_api_key and s.binance_demo_api_secret):
        raise SystemExit("BIQ_BINANCE_DEMO_API_KEY / BIQ_BINANCE_DEMO_API_SECRET are not set in .env")
    if cfg.exchange.environment != "demo":
        raise SystemExit(f"testnet config must use the demo environment, got {cfg.exchange.environment}")
    print("  demo keys : present (values not shown)")

# Not fatal: neither mode can touch real money. But an unattended run whose only alert channel is
# a log file is how week one spent six days in SAFE_HOLD unnoticed, so say it out loud.
if "telegram" in channels and not (s.telegram_bot_token and s.telegram_chat_id):
    print("  WARNING   : telegram is configured but BIQ_TELEGRAM_BOT_TOKEN/CHAT_ID are unset")
    print(f"              -> alerts will only reach state/{mode}.log; nothing will page you")
elif channels == ["log"]:
    print("  WARNING   : alerts only go to the log file; nobody is notified if this stops")
PY

# --- install ------------------------------------------------------------------------------------
launchctl unload "$PLIST" 2>/dev/null || true
sed "s#/PATH/TO/REPO#$REPO#g" "$REPO/scripts/launchd/$LABEL.plist" > "$PLIST"
launchctl load "$PLIST"

sleep 3
if launchctl list | grep -q "$LABEL"; then
  echo
  echo "installed and started: $LABEL"
  echo "  logs   : tail -f $REPO/state/$MODE.log"
  echo "  status : cd $REPO && .venv/bin/python -m btc_inventory_quant status --config $CONFIG"
  echo "  stop   : bash scripts/install_agent.sh $MODE --uninstall"
else
  fail "the agent did not appear in 'launchctl list' — check $REPO/state/$MODE.log"
fi
