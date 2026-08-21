#!/usr/bin/env bash
# Validates and benchmarks the optimized C implementation.
# Usage: ./bench.sh path/to/measurements_1B.txt
# Recommended: quiet machine, performance governor, page cache warm.
set -euo pipefail
cd "$(dirname "$0")/.."

DATA="${1:-measurements_1B.txt}"
RUNS="${RUNS:-20}"
WARMUP="${WARMUP:-5}"

command -v hyperfine >/dev/null || {
    echo "FAIL: hyperfine is not installed." >&2
    exit 1
}
command -v fincore >/dev/null || {
    echo "FAIL: fincore is not installed." >&2
    exit 1
}

./scripts/validate.sh "$DATA"
dd if="$DATA" of=/dev/null bs=8M status=progress
fincore -o RES,PAGES,SIZE "$DATA"
grep '^FileHugePages:' /proc/meminfo

hyperfine \
    --style basic \
    --warmup "$WARMUP" \
    --runs "$RUNS" \
    --shell=none \
    "c/c-linux $DATA"
