#!/usr/bin/env bash
# Full validation: the dataset-independent gate plus the byte-exact comparison
# of the optimized program against the independent C oracle on a complete
# dataset.
#
# Usage: ./scripts/validate.sh [path/to/measurements_1B.txt]
#
# Only the final comparison needs the dataset. Run ./verify.sh directly for
# every correctness surface that does not.
set -euo pipefail
cd "$(dirname "$0")/.."

DATA="${1:-measurements_1B.txt}"

./verify.sh

echo
echo "=== full-dataset comparison ==="
if [ ! -s "$DATA" ]; then
    echo "FAIL: $DATA not found. Run scripts/gen-data.sh first." >&2
    exit 1
fi

CACHE_ROOT=".oracle-cache"
mkdir -p "$CACHE_ROOT"
DHASH=$(md5sum "$DATA" | cut -d' ' -f1)
BHASH=$(sha256sum baseline/main.c | cut -c1-16)
CACHE="$CACHE_ROOT/oracle-${DHASH}-${BHASH}.txt"

if [ ! -s "$CACHE" ]; then
    echo "Generating reference output via C oracle ($(du -h "$DATA" | cut -f1))..."
    baseline/baseline "$DATA" > "$CACHE"
fi

printf '  %-38s ... ' "optimized output"
if cmp -s <(c/c-linux "$DATA") "$CACHE"; then
    echo "OK"
else
    echo "FAIL"
    exit 1
fi

echo
echo "validation: PASS"
