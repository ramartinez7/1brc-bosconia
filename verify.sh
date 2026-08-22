#!/usr/bin/env bash
# Dataset-independent verification gate.
#
# Usage: ./verify.sh
#
# Runs every small correctness surface of this repository from a clean clone.
# It needs no root, no network, and no one-billion-row dataset; it writes only
# inside .test-work/, removes that directory before exiting, and fails if the
# working tree changed, so a passing run on a clean clone leaves it clean.
#
# scripts/validate.sh runs this gate and then adds the full-dataset comparison.
set -euo pipefail
cd "$(dirname "$0")"

unset ONEBRC_STRICT ONEBRC_GENERAL NTHREADS

WORK=".test-work"
started=$(date +%s)

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    IN_GIT=1
    STATUS_BEFORE=$(git status --porcelain)
else
    IN_GIT=0
    STATUS_BEFORE=""
fi

stage() {
    echo
    echo "=== $* ==="
}

require() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "verify: required tool missing: $1" >&2
        exit 1
    }
}

require cc
require make
require python3
require cmp
require diff

stage "build"
./scripts/build.sh

stage "oracle smoke"
bash tests/baseline-smoke.sh

stage "fixtures, dense sentinel/direct cases, and sanitizer regression"
bash tests/fixture-oracle-test.sh "$WORK/fixtures"

stage "parser, EOF guard, and sanitizer corpus"
bash tests/parser-fuzz.sh --work "$WORK/fuzz"

stage "general-input cardinality"
python3 tests/general-input-test.py c

stage "strict input and runtime envelope"
python3 tests/strict-input-test.py c

stage "repository contracts"
python3 tests/gate-contract-test.py
python3 tests/contract-mutation-test.py

stage "working tree cleanliness"
rm -rf -- "$WORK"
if ((IN_GIT)); then
    STATUS_AFTER=$(git status --porcelain)
    if [[ "$STATUS_AFTER" != "$STATUS_BEFORE" ]]; then
        echo "verify: the gate changed the working tree:" >&2
        diff <(printf '%s\n' "$STATUS_BEFORE") <(printf '%s\n' "$STATUS_AFTER") >&2 || true
        exit 1
    fi
    if [[ -n "$STATUS_AFTER" ]]; then
        echo "working tree: unchanged by the gate (pre-existing local changes)"
    else
        echo "working tree: clean"
    fi
else
    echo "working tree: not a Git checkout, cleanliness not checked"
fi

echo
echo "verify: PASS ($(( $(date +%s) - started ))s)"
