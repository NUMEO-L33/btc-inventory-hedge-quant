#!/usr/bin/env bash
# Install (or reinstall) the launchd agent that keeps shadow mode running on macOS.
#
#   bash scripts/install_shadow_agent.sh            # install and start
#   bash scripts/install_shadow_agent.sh --uninstall
#
# Shadow mode needs NO API credentials: it reads real public market data and simulates the account,
# so real orders are impossible by construction. It is meant to run for weeks, which is what the
# agent is for — it starts at login and restarts the process if it dies.

set -euo pipefail

LABEL="com.numeo.btc-inventory-quant.shadow"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled $LABEL"
  exit 0
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "launchd is macOS-only. On Linux use 'docker compose up -d shadow' or a systemd unit." >&2
  exit 1
fi

# --- preflight: fail loudly now rather than silently in a log file for two weeks ---------------
fail() { echo "FAILED: $*" >&2; exit 1; }

[[ -x "$REPO/.venv/bin/python" ]] || fail "no venv at $REPO/.venv — run: python3.12 -m venv .venv && pip install -e '.[dev]'"
[[ -f "$REPO/configs/shadow.yaml" ]] || fail "configs/shadow.yaml is missing"
mkdir -p "$REPO/state" "$HOME/Library/LaunchAgents"

"$REPO/.venv/bin/python" - "$REPO" <<'PY' || fail "shadow config did not load — fix it before installing the agent"
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
from btc_inventory_quant.config.loader import load_config

cfg = load_config(Path(sys.argv[1]) / "configs" / "shadow.yaml")
ratio = getattr(cfg.strategy.fixed_hedge, "ratio", None)
print(f"  mode      : {cfg.mode.value}")
print(f"  strategy  : {cfg.strategy.name}" + (f" @ {ratio}" if ratio is not None else ""))
print(f"  exchange  : {cfg.exchange.name.value} ({cfg.exchange.environment}) — paper account")
print(f"  database  : {cfg.storage.database_url}")
PY

# --- install ------------------------------------------------------------------------------------
launchctl unload "$PLIST" 2>/dev/null || true
sed "s#/PATH/TO/REPO#$REPO#g" "$REPO/scripts/launchd/$LABEL.plist" > "$PLIST"
launchctl load "$PLIST"

sleep 3
if launchctl list | grep -q "$LABEL"; then
  echo
  echo "installed and started: $LABEL"
  echo "  logs   : tail -f $REPO/state/shadow.log"
  echo "  status : cd $REPO && .venv/bin/python -m btc_inventory_quant status --config configs/shadow.yaml"
  echo "  stop   : bash scripts/install_shadow_agent.sh --uninstall"
else
  fail "the agent did not appear in 'launchctl list' — check $REPO/state/shadow.log"
fi
