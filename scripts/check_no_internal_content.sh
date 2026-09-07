#!/usr/bin/env bash
# Blocks accidental inclusion of Inferrail's private-strategy-repo content
# in this public repo. See CLAUDE.md's "Hard boundary with the private
# strategy repo" for what this repo must never contain.
#
# Scans all git-tracked files (not just a diff) so it catches anything
# already committed, not only new changes.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# This script and the workflow that runs it necessarily name the terms
# below, so both are excluded from the scan to avoid matching themselves.
SELF_PATHS=(
  "scripts/check_no_internal_content.sh"
  ".github/workflows/boundary-check.yml"
  ".githooks/pre-push"
)

DENYLIST_FILENAMES=(
  "BET_REGISTER.md"
  "WEDGE.md"
  "EVIDENCE_LOG.md"
  "DECISION_LOG.md"
  "KILL_CRITERIA.md"
  "ICP_AND_PAIN.md"
  "MISSION_AND_IDENTITY.md"
  "STRATEGIC_DOCTRINE.md"
  "PRODUCT_THESIS.md"
  "COMPETITIVE_MAP.md"
)

DENYLIST_STRINGS=(
  "inferrail-internal"
  "wedge hypothesis"
  "moat hypothesis"
  "bet register"
  "kill criteria"
  "icp and pain"
  "evidence log"
  "decision log"
)

fail=0

all_files="$(git ls-files)"

echo "Checking for denylisted internal filenames..."
for name in "${DENYLIST_FILENAMES[@]}"; do
  matches="$(echo "$all_files" | grep -F -- "$name" || true)"
  if [ -n "$matches" ]; then
    echo "BLOCKED: found internal-only filename pattern '$name':"
    echo "$matches"
    fail=1
  fi
done

echo "Checking tracked file contents for denylisted terms..."
scan_files="$all_files"
for skip in "${SELF_PATHS[@]}"; do
  scan_files="$(echo "$scan_files" | grep -F -v -- "$skip" || true)"
done

if [ -n "$scan_files" ]; then
  for term in "${DENYLIST_STRINGS[@]}"; do
    matches="$(echo "$scan_files" | xargs -d '\n' grep -ril -F -- "$term" 2>/dev/null || true)"
    if [ -n "$matches" ]; then
      echo "BLOCKED: found denylisted term '$term' in:"
      echo "$matches"
      fail=1
    fi
  done
fi

if [ "$fail" -ne 0 ]; then
  echo
  echo "One or more tracked files reference private-strategy-repo content."
  echo "If this is a false positive, adjust the denylist in this script."
  echo "Do not bypass this check to land internal content in this repo."
  exit 1
fi

echo "OK: no denylisted internal content found."
