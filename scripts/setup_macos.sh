#!/usr/bin/env bash
# macOS bootstrap for BTC Inventory Hedge Quant (Apple Silicon / Intel).
# Usage: bash scripts/setup_macos.sh          (run from the repository root)
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Checking Python 3.12"
if ! command -v python3.12 >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "    python3.12 not found — installing with Homebrew"
    brew install python@3.12
  else
    echo "    Homebrew not found. Install Python 3.12 from https://www.python.org/downloads/macos/ or"
    echo "    install Homebrew first: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    exit 1
  fi
fi
python3.12 --version

echo "==> Creating virtual environment (.venv)"
python3.12 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null

echo "==> Installing project + dev tools"
pip install -e ".[dev]"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> .env created from .env.example (fill in API keys later; never commit it)"
fi
mkdir -p state reports

echo "==> Verifying the committed data snapshot"
python -m btc_inventory_quant data --raw-dir data/raw verify

echo "==> Running the test suite"
python -m pytest -q

cat <<'MSG'

Setup complete. Next:
  source .venv/bin/activate
  python -m btc_inventory_quant backtest --config configs/backtest.yaml      # reports/<run_id>/backtest_report.md
  python -m btc_inventory_quant data --raw-dir data/raw download             # refresh data (needs Binance access)
  python -m btc_inventory_quant testnet --config configs/testnet.yaml --once # after demo API keys are in .env
  python -m btc_inventory_quant shadow --config configs/shadow.yaml          # paper account, real data, no real orders
MSG
