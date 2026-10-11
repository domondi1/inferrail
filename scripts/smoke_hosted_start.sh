#!/usr/bin/env bash
# Start the production entry point (hosted/budget_gateway/cohost.py) the way the host runs it and
# check that it serves, with payments off and with the x402 credit rail on. Run it with the
# Python of an environment installed exactly like production:
#
#   python -m venv /tmp/prod && /tmp/prod/bin/pip install -e . \
#     -r hosted/cost_gateway/requirements.txt -r hosted/budget_gateway/requirements.txt
#   scripts/smoke_hosted_start.sh /tmp/prod/bin/python
#
# "Payments on" uses Base Sepolia and a burn address, so nothing can be paid to anyone. It catches
# imports that only run when the rail is configured (e.g. a missing optional dependency).
set -euo pipefail
PY="${1:?usage: smoke_hosted_start.sh <python of a production-install venv>}"
PORT="${PORT:-8481}"
BASE="http://127.0.0.1:${PORT}"
WORK="$(mktemp -d)"
PID=0
# Never `kill 0`: that would signal this script's whole process group.
trap '[ "$PID" -gt 0 ] && kill "$PID" 2>/dev/null; rm -rf "$WORK"' EXIT

check() {  # check <method> <path> <expected status>
  local got
  got="$(curl -s -o /dev/null -w '%{http_code}' -X "$1" "${BASE}$2")"
  if [ "$got" != "$3" ]; then
    echo "FAIL: $1 $2 -> $got (expected $3)"; echo "--- server log:"; cat "$WORK/log"; exit 1
  fi
  echo "ok: $1 $2 -> $got"
}

start() {  # start <label> [extra env...]
  local label="$1"; shift
  mkdir -p "$WORK/$label/hosted" "$WORK/$label/trial"
  env -i PATH="$PATH" HOME="$HOME" PORT="$PORT" \
    BG_DATA_DIR="$WORK/$label/hosted" COST_GATEWAY_DATA_DIR="$WORK/$label/trial" "$@" \
    "$PY" hosted/budget_gateway/cohost.py >"$WORK/log" 2>&1 &
  PID=$!
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null "${BASE}/health"; then return 0; fi
    if ! kill -0 "$PID" 2>/dev/null; then
      echo "FAIL: the service exited during startup ($label)"; cat "$WORK/log"; exit 1
    fi
    sleep 0.5
  done
  echo "FAIL: no response within 30s ($label)"; cat "$WORK/log"; exit 1
}

stop() { kill "$PID"; wait "$PID" 2>/dev/null || true; PID=0; }

echo "== payments off"
start off
check GET /health 200
check GET /hosted/health 200
check GET /hosted/pricing 200
check POST /hosted/v1/credits/x402 404
stop

echo "== payments on (Base Sepolia, burn address)"
start on BG_X402_PAY_TO=0x000000000000000000000000000000000000dEaD
check GET /health 200
check GET /hosted/health 200
check GET /hosted/pricing 200
check POST /hosted/v1/credits/x402 402
stop
echo "SMOKE OK"
