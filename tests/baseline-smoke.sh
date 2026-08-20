#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

make -C baseline --no-print-directory
actual=$(mktemp)
invalid=$(mktemp)
trap 'rm -f "$actual" "$invalid"' EXIT

baseline/baseline tests/custom/baseline-smoke.txt > "$actual"
cmp -s "$actual" tests/custom/baseline-smoke.expected

printf 'Broken;1.00\n' > "$invalid"
if baseline/baseline "$invalid" >/dev/null 2>&1; then
    echo "baseline accepted an invalid temperature" >&2
    exit 1
fi

echo "baseline smoke: PASS"
