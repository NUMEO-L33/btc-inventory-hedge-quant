#!/usr/bin/env bash
# Wait for the next funding settlement, capture the rate just before it, verify just after.
#
# The manual procedure needs a human awake at 00:00 / 08:00 / 16:00 UTC, and the first attempt was
# missed for exactly that reason. Start this once and walk away:
#
#   bash scripts/funding_verify_watch.sh
#
# It sleeps until a few minutes before the next settlement, captures the rate THIS environment
# will apply (the demo host publishes no funding history, so the rate has to be taken
# prospectively — see docs/exchange_api_verification.md §16), sleeps past the boundary, then runs
# the verification and leaves the JSON in reports/.
#
# Requires an open position at the settlement instant, or there is no FUNDING_FEE to check.

set -euo pipefail

CONFIG="${CONFIG:-configs/testnet.yaml}"
CAPTURE_LEAD_MIN="${CAPTURE_LEAD_MIN:-4}"   # capture this many minutes before settlement
VERIFY_DELAY_MIN="${VERIFY_DELAY_MIN:-5}"   # verify this many minutes after it
OUT_DIR="${OUT_DIR:-reports/funding_verification}"

cd "$(dirname "$0")/.."
mkdir -p "$OUT_DIR"

if [[ -z "${VIRTUAL_ENV:-}" && -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

seconds_until_next_settlement() {
  python3 - <<'PY'
import datetime as dt
now = dt.datetime.now(dt.UTC)
base = now.replace(minute=0, second=0, microsecond=0)
cands = [base.replace(hour=h) for h in (0, 8, 16)] + [base.replace(hour=0) + dt.timedelta(days=1)]
nxt = next(c for c in cands if c > now)
print(int((nxt - now).total_seconds()), nxt.isoformat())
PY
}

read -r SECS SETTLEMENT < <(seconds_until_next_settlement)
LEAD=$((CAPTURE_LEAD_MIN * 60))

echo "next settlement : $SETTLEMENT"
echo "capture at      : ${CAPTURE_LEAD_MIN} min before  (in $(( (SECS - LEAD) / 60 )) min)"
echo "verify at       : ${VERIFY_DELAY_MIN} min after"
echo

if (( SECS > LEAD )); then
  echo "sleeping $(( (SECS - LEAD) / 60 )) min until the capture window..."
  sleep $(( SECS - LEAD ))
else
  echo "already inside the capture window — capturing now"
fi

echo "=== capturing the upcoming rate ==="
python -m btc_inventory_quant verify-funding --config "$CONFIG" --snapshot-rate \
  | tee "$OUT_DIR/snapshot_$(date -u +%Y%m%dT%H%M%SZ).json"

read -r SECS2 _ < <(seconds_until_next_settlement)
WAIT=$(( SECS2 + VERIFY_DELAY_MIN * 60 ))
echo
echo "sleeping $(( WAIT / 60 )) min until after the settlement..."
sleep "$WAIT"

echo "=== verifying against the real settlement ==="
REPORT="$OUT_DIR/verification_$(date -u +%Y%m%dT%H%M%SZ).json"
set +e
python -m btc_inventory_quant verify-funding --config "$CONFIG" --window-hours 12 | tee "$REPORT"
STATUS=$?
set -e

echo
echo "report: $REPORT"
case "$STATUS" in
  0) echo "VERIFIED — the funding formula matches a real settlement." ;;
  1) echo "MISMATCH or MIXED — read rate_source before concluding the formula is wrong." ;;
  *) echo "NO_DATA — check that a position was open at the settlement instant." ;;
esac
exit "$STATUS"
