#!/usr/bin/env bash
# Runs the hosted Work Economics paid-path suite (plus the x402 SDK
# contract tests it depends on) and fails the job if pytest fails, if
# anything reports SKIPPED, or if any default target file contributed no
# passing test. A skipped or silently-deselected paid-path suite must
# never be mistaken for a passing one. Same exit-code discipline as
# scripts/run_hosted_a2a_tests.sh; see
# tests/unit/hosted/test_run_hosted_work_economics_tests_script.py.
#
# Usage: run_hosted_work_economics_tests.sh [pytest target ...]
# With no arguments, runs the real suite. Tests pass their own synthetic
# targets to exercise this script's behavior in isolation.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

DEFAULT_TARGETS=(
    tests/unit/hosted/test_x402_sdk_contract.py
    tests/unit/hosted/test_work_economics_paid_path.py
    tests/unit/hosted/test_work_economics_capability.py
    tests/unit/hosted/test_work_economics_store.py
    tests/unit/hosted/test_work_economics_production_db_guard.py
)
# test_work_economics_service.py is deliberately NOT listed: it is
# skip-gated on real CDP credentials (it talks to the real CDP facilitator
# for the unpaid 402 path) and still runs, allowed to skip, in the main
# `test` CI job.

REQUIRE_EVERY_TARGET=0
if [ "$#" -eq 0 ]; then
    set -- "${DEFAULT_TARGETS[@]}"
    REQUIRE_EVERY_TARGET=1
fi

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

pytest "$@" -v -rs -p no:cacheprovider | tee "$LOG"

if grep -q "^SKIPPED" "$LOG"; then
    echo "::error::a skipped test was detected -- a skipped Work Economics paid-path test must fail this job." >&2
    exit 1
fi

if [ "$REQUIRE_EVERY_TARGET" -eq 1 ]; then
    for target in "${DEFAULT_TARGETS[@]}"; do
        if ! grep -q "^${target}::.* PASSED" "$LOG"; then
            echo "::error::${target} contributed no passing test -- it may have been deselected or failed to collect." >&2
            exit 1
        fi
    done
fi
