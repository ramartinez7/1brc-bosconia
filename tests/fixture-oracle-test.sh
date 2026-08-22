#!/usr/bin/env bash
# Byte-exact fixture comparison and dense-path sanitizer regression.
#
# Usage: bash tests/fixture-oracle-test.sh [work-dir]
#
# Compares the optimized binary against checked-in and generated expectations
# for the dense sentinel/direct path, the collision fallback, the exhaustive
# temperature domain, and page-aligned end-of-file records, then rebuilds the
# implementation under AddressSanitizer and UndefinedBehaviorSanitizer and
# drives the same inputs through its worker loops.
set -euo pipefail
cd "$(dirname "$0")/.."

WORK=$(realpath -m -- "${1:-.test-work/fixtures}")
ROOT=$PWD
case "$WORK" in
    "$ROOT/.test-work"/*) ;;
    *)
        echo "fixtures: work directory must be under $ROOT/.test-work: $WORK" >&2
        exit 1
        ;;
esac
MARKER="$WORK/.fixture-oracle-owned"
if [[ -e "$WORK" && ! -f "$MARKER" ]]; then
    echo "fixtures: refusing unsafe work directory: $WORK" >&2
    exit 1
fi
rm -rf -- "$WORK"
mkdir -p "$WORK"
touch "$MARKER"

./scripts/build.sh >/dev/null
BIN="c/c-linux"
SOURCE=$(realpath c/main.c)

check_output() {
    local label=$1
    local input=$2
    local expected=$3
    local actual="$WORK/actual.out"
    printf '  %-38s ... ' "$label"
    if "$BIN" "$input" > "$actual" && cmp -s "$actual" "$expected"; then
        echo "OK"
    else
        echo "FAIL"
        diff -u "$expected" "$actual" | head -80 || true
        exit 1
    fi
}

for input in tests/custom/*.txt; do
    expected="${input%.txt}.expected"
    [[ -f "$expected" ]] || continue
    check_output "$(basename "${input%.txt}")" "$input" "$expected"
done

python3 tests/generate-dense-fixture.py "$WORK/dense"
for input in "$WORK"/dense/*.txt; do
    expected="${input%.txt}.expected"
    check_output "$(basename "${input%.txt}")" "$input" "$expected"
done

python3 tests/generate-temperature-fixture.py "$WORK/temperatures"
for input in "$WORK"/temperatures/*.txt; do
    expected="${input%.txt}.expected"
    check_output "$(basename "${input%.txt}")" "$input" "$expected"
done

printf '  %-38s ... ' "dense sanitizer regression"
REGRESSION="$WORK/c-dense-regression"
if "${CC:-cc}" \
       -O1 -g -fno-omit-frame-pointer \
       -march=native -mavx2 -mbmi2 -std=c11 \
       -Wall -Wextra -Wpedantic -Werror -pthread \
       -fsanitize=address,undefined \
       "-DONEBRC_SOURCE=\"$SOURCE\"" \
       tests/c-dense-regression.c -o "$REGRESSION" -lm \
   && ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
      UBSAN_OPTIONS=halt_on_error=1 \
      "$REGRESSION" \
        tests/custom/all-413-stations-stress.txt \
        "$WORK/dense/dense-page-eof.txt" \
        "$WORK/dense/generic-page-eof.txt" \
        "$WORK/dense/generic-page-eof-long-existing.txt" \
        "$WORK/dense/generic-page-eof-long-new.txt" \
        "$WORK/dense/generic-page-eof-long-65-existing.txt" \
        "$WORK/dense/generic-page-eof-long-65-new.txt" >/dev/null; then
    echo "OK"
else
    echo "FAIL"
    exit 1
fi

rm -rf -- "$WORK"
echo "fixtures: PASS"
