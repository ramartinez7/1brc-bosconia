#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

make -C baseline --no-print-directory
WORK=".test-work/baseline-smoke.$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT
actual="$WORK/actual.out"
invalid="$WORK/invalid.txt"

baseline/baseline tests/custom/baseline-smoke.txt > "$actual"
cmp -s "$actual" tests/custom/baseline-smoke.expected

printf 'Broken;1.00\n' > "$invalid"
if baseline/baseline "$invalid" >/dev/null 2>&1; then
    echo "baseline accepted an invalid temperature" >&2
    exit 1
fi

echo "baseline smoke: PASS"
