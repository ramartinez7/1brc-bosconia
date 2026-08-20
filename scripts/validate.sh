#!/usr/bin/env bash
# Validates optimized C against the independent C oracle and regressions.
# Usage: ./validate.sh path/to/measurements_1B.txt
set -euo pipefail
cd "$(dirname "$0")/.."

DATA="${1:-measurements_1B.txt}"
if [ ! -s "$DATA" ]; then
    echo "FAIL: $DATA not found. Run scripts/gen-data.sh first." >&2
    exit 1
fi

./scripts/build.sh
bash tests/baseline-smoke.sh

DHASH=$(md5sum "$DATA" | cut -d' ' -f1)
BHASH=$(sha256sum baseline/main.c | cut -c1-16)
CACHE="/tmp/bosconia-baseline-${DHASH}-${BHASH}.txt"

if [ ! -s "$CACHE" ]; then
    echo "Generating reference output via C oracle ($(du -h "$DATA" | cut -f1))..."
    baseline/baseline "$DATA" > "$CACHE"
fi

echo -n "optimized output ... "
if cmp -s <(c/c-linux "$DATA") "$CACHE"; then
    echo "OK"
else
    echo "FAIL"
    exit 1
fi

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
python3 tests/generate-dense-fixture.py "$TEST_DIR"

check_output() {
    local label=$1
    local input=$2
    local expected=$3
    local actual="$TEST_DIR/actual.out"
    printf '%-24s ... ' "$label"
    if c/c-linux "$input" > "$actual" &&
       cmp -s "$actual" "$expected"; then
        echo "OK"
    else
        echo "FAIL"
        diff -u "$expected" "$actual" | head -80 || true
        exit 1
    fi
}

check_output \
    "dense execution" \
    "$TEST_DIR/dense-413.txt" \
    "$TEST_DIR/dense-413.expected"
check_output \
    "collision fallback" \
    tests/custom/all-413-stations-stress.txt \
    tests/custom/all-413-stations-stress.expected
check_output \
    "long-key fallback" \
    "$TEST_DIR/dense-long-key-fallback.txt" \
    "$TEST_DIR/dense-long-key-fallback.expected"

for page_case in \
    dense-page-eof \
    generic-page-eof \
    generic-page-eof-long-existing \
    generic-page-eof-long-new \
    generic-page-eof-long-65-existing \
    generic-page-eof-long-65-new; do
    check_output \
        "$page_case" \
        "$TEST_DIR/$page_case.txt" \
        "$TEST_DIR/$page_case.expected"
done

TEMP_DIR="$TEST_DIR/temperatures"
python3 tests/generate-temperature-fixture.py "$TEMP_DIR"
for input in "$TEMP_DIR"/temperature-exhaustive-*.txt; do
    stem=$(basename "${input%.txt}")
    check_output \
        "$stem" \
        "$input" \
        "${input%.txt}.expected"
done

echo -n "dense sanitizer regression ... "
C_REGRESSION="$TEST_DIR/c-dense-regression"
SANITIZER_FLAGS=(
    -O1 -g -fno-omit-frame-pointer
    -march=native -mavx2 -mbmi2 -std=c11
    -Wall -Wextra -Wpedantic -Werror
    -pthread
    -fsanitize=address,undefined
)
if "${CC:-cc}" "${SANITIZER_FLAGS[@]}" \
        tests/c-dense-regression.c \
        -o "$C_REGRESSION" \
        -lm \
   && ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
      UBSAN_OPTIONS=halt_on_error=1 \
      "$C_REGRESSION" \
        tests/custom/all-413-stations-stress.txt \
        "$TEST_DIR/dense-page-eof.txt" \
        "$TEST_DIR/generic-page-eof.txt" \
        "$TEST_DIR/generic-page-eof-long-existing.txt" \
        "$TEST_DIR/generic-page-eof-long-new.txt" \
        "$TEST_DIR/generic-page-eof-long-65-existing.txt" \
        "$TEST_DIR/generic-page-eof-long-65-new.txt" >/dev/null; then
    echo "OK"
else
    echo "FAIL"
    exit 1
fi

echo "validation: PASS"
