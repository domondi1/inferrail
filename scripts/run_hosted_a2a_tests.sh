#!/usr/bin/env bash
# Runs the hosted Economic Authority test suite and fails the job if
# EITHER pytest itself fails OR anything reports SKIPPED -- a skipped
# transport suite must never be mistaken for a passing one (see repair
# item 7 in hosted/a2a_economic_authority/'s security-repair history).
#
# `set -euo pipefail` (not just `pipefail` on its own) is what makes this
# safe to run as a plain `run:` step in CI: without it, `pytest ... | tee`
# would let a failing pytest be masked by tee's own successful exit code.
# See tests/unit/hosted/test_run_hosted_a2a_tests_script.py for a
# deterministic proof of this script's exit-code behavior against
# synthetic passing/failing/skipped test files.
#
# Usage: run_hosted_a2a_tests.sh [pytest target ...]
# With no arguments, runs the real Economic Authority suite. Tests pass
# their own synthetic targets to exercise this script's behavior in
# isolation.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if [ "$#" -eq 0 ]; then
    set -- \
        tests/unit/hosted/test_a2a_economic_authority_core.py \
        tests/unit/hosted/test_a2a_economic_authority_capabilities.py \
        tests/unit/hosted/test_a2a_economic_authority_reservation_recovery.py \
        tests/unit/hosted/test_a2a_economic_authority_transport.py
fi

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

pytest "$@" -v -rs | tee "$LOG"

if grep -q "^SKIPPED" "$LOG"; then
    echo "::error::a skipped test was detected -- a skipped Economic Authority test must fail this job." >&2
    exit 1
fi
