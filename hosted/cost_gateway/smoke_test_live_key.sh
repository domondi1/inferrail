#!/usr/bin/env bash
# Live-provider smoke test for the hosted Cost Gateway (Phase 1).
#
# Run this YOURSELF, locally, with your own OPENAI_API_KEY -- it is read
# from your own shell environment only. This script never prints, logs,
# or transmits it anywhere except this one request to the local service
# you are running, exactly the boundary the service itself enforces (see
# hosted/cost_gateway/README.md's threat-model section). No assistant
# session ever sees this key.
#
# Prerequisites:
#   1. Start the service in one terminal:
#        pip install -e ".[dev,mcp]"
#        pip install -r hosted/cost_gateway/requirements.txt
#        python3 hosted/cost_gateway/service.py /tmp/cost_gateway_smoke 8423
#   2. In a second terminal, run this script:
#        OPENAI_API_KEY=sk-... ./hosted/cost_gateway/smoke_test_live_key.sh
#
# This will make exactly one real, billed OpenAI call.

set -euo pipefail

HOST="${COST_GATEWAY_HOST:-http://127.0.0.1:8423}"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "Set OPENAI_API_KEY in your own shell first, e.g.:" >&2
  echo "  OPENAI_API_KEY=sk-... $0" >&2
  exit 1
fi

echo "== 1. Health check =="
curl -s "$HOST/health"
echo -e "\n"

echo "== 2. Issuing a trial tenant (no auth, no account) =="
TRIAL_JSON=$(curl -s -X POST "$HOST/v1/trial")
echo "$TRIAL_JSON" | python3 -m json.tool
API_KEY=$(echo "$TRIAL_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['api_key'])")
TENANT_ID=$(echo "$TRIAL_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['tenant_id'])")
echo

echo "== 3. Submitting your OpenAI key (value never echoed by the service or this script) =="
curl -s -X POST "$HOST/v1/trial/$TENANT_ID/keys" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d "{\"openai_key\": \"$OPENAI_API_KEY\"}" | python3 -m json.tool
echo

echo "== 4. Sending one real request through your key =="
curl -s -X POST "$HOST/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -H "X-Inferrail-Attribute-Purpose: cost-gateway-smoke-test" \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Say hello in exactly five words."}]}' \
  | python3 -m json.tool
echo

echo "== 5. Reading back the receipt -- confirm real cost, no prompt/response content =="
curl -s "$HOST/v1/receipts" -H "Authorization: Bearer $API_KEY" | python3 -m json.tool
echo

echo "== 6. Trial status -- key configured, expiry tightened to <= 4h =="
curl -s "$HOST/v1/trial/$TENANT_ID" -H "Authorization: Bearer $API_KEY" | python3 -m json.tool
echo

echo "== 7. Cleanup: forgetting the key, then ending the trial =="
curl -s -X DELETE "$HOST/v1/trial/$TENANT_ID/keys" -H "Authorization: Bearer $API_KEY" | python3 -m json.tool
curl -s -X DELETE "$HOST/v1/trial/$TENANT_ID" -H "Authorization: Bearer $API_KEY" | python3 -m json.tool
echo

cat <<'EOF'
Done. Check by eye, above:
  - Step 4: a real "content" reply and a non-null "estimated_cost_usd"
    on the response's "inferrail" block (or step 5's receipt).
  - Step 5: the receipt's "provider" is "openai" (not "demo"), and it
    contains no prompt/response text anywhere -- only counts and cost.
  - Step 6, taken BEFORE cleanup: "openai_configured": true and
    "mode": "real_key", with "seconds_remaining" <= 14400 (4h).
  - Step 7: both cleanup calls succeed; a repeat GET against the same
    tenant_id/api_key afterward should now return 401.
EOF
