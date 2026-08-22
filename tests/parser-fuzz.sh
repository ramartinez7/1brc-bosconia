#!/usr/bin/env bash
# Deterministic parser, temperature-domain, and page-aligned EOF fuzz corpus.
#
# Usage: bash tests/parser-fuzz.sh [--work DIR] [--keep]
#
# Every case is generated from versioned named seeds in
# tests/generate-fuzz-corpus.py, so a failure reproduces byte-for-byte.
# See docs/parser-fuzz-corpus.md.
set -euo pipefail
cd "$(dirname "$0")/.."

IMPL="c"
WORK="${ONEBRC_FUZZ_WORK:-.test-work/fuzz}"
KEEP=0

usage() {
    sed -n '2,7p' "$0"
}

while (($#)); do
    case "$1" in
        --work) WORK="${2:?--work needs a directory}"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "parser-fuzz: unexpected argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

test -f "$IMPL/main.c" || {
    echo "parser-fuzz: implementation source missing: $IMPL/main.c" >&2
    exit 1
}

WORK=$(realpath -m -- "$WORK")
ROOT=$PWD
MARKER="$WORK/.onebrc-parser-fuzz-work"
MARKER_VALUE="onebrc-parser-fuzz-v1"
if [[ "$WORK" == "/" || "$WORK" == "$ROOT" ]]; then
    echo "parser-fuzz: refusing unsafe work directory: $WORK" >&2
    exit 1
fi
if [[ -e "$WORK" ]]; then
    if [[ ! -d "$WORK" ]] ||
       [[ ! -f "$MARKER" ]] ||
       ! grep -Fxq "$MARKER_VALUE" "$MARKER"; then
        echo "parser-fuzz: refusing to remove unowned work directory: $WORK" >&2
        exit 1
    fi
    rm -rf -- "$WORK"
fi
mkdir -p "$WORK"
printf '%s\n' "$MARKER_VALUE" > "$MARKER"

started=$(date +%s)
./scripts/build.sh >/dev/null

THREADS=$(nproc 2>/dev/null || echo 1)
if ((THREADS > 4)); then THREADS=4; fi
BIN="$IMPL/c-linux"
ORACLE="baseline/baseline"
SOURCE=$(realpath "$IMPL/main.c")

python3 tests/generate-fuzz-corpus.py "$WORK"

CASES="$WORK/cases.tsv"
GUARDED="$WORK/guarded.tsv"
test -s "$CASES" || {
    echo "parser-fuzz: corpus index missing: $CASES" >&2
    exit 1
}

report_case() {
    local name=$1
    local row
    row=$(awk -F'\t' -v name="$name" '$1 == name' "$CASES" || true)
    {
        echo "parser-fuzz: FAIL: $name"
        echo "  corpus:      version $(cat "$WORK/corpus-version")"
        if [[ -n "$row" ]]; then
            awk -F'\t' -v OFS='' '{
                print "  family:      ", $2
                print "  seed:        ", $3, " = ", $4
                print "  class:       ", $5
                print "  bytes:       ", $6, "  records: ", $7, "  names: ", $8
                print "  description: ", $9
            }' <<<"$row"
        fi
        echo "  reproduce:   python3 tests/generate-fuzz-corpus.py $WORK --only $name"
        echo "               $BIN $WORK/$name.txt"
    } >&2
}

valid_cases=0
unspecified_cases=0
mismatch=0

while IFS=$'\t' read -r name family seed_name seed_value class bytes records names description; do
    if [[ "$name" == "case" ]]; then
        continue
    fi
    input="$WORK/$name.txt"
    case "$class" in
    valid)
        expected="$WORK/$name.expected"
        oracle_out="$WORK/$name.oracle"
        if ! "$ORACLE" "$input" > "$oracle_out"; then
            report_case "$name"
            echo "  oracle refused a valid corpus case" >&2
            mismatch=1
            continue
        fi
        if ! cmp -s "$oracle_out" "$expected"; then
            report_case "$name"
            echo "  baseline oracle disagrees with the generated expectation" >&2
            diff -u "$expected" "$oracle_out" | head -20 >&2 || true
            mismatch=1
            continue
        fi
        for threads in 1 "$THREADS"; do
            actual="$WORK/$name.actual"
            if ! NTHREADS="$threads" "$BIN" "$input" > "$actual"; then
                report_case "$name"
                echo "  optimized binary failed at NTHREADS=$threads" >&2
                mismatch=1
                continue 2
            fi
            if ! cmp -s "$actual" "$oracle_out"; then
                report_case "$name"
                echo "  optimized output differs from the oracle at NTHREADS=$threads" >&2
                diff -u "$oracle_out" "$actual" | head -20 >&2 || true
                mismatch=1
                continue 2
            fi
        done
        valid_cases=$((valid_cases + 1))
        ;;
    unspecified)
        # Output and exit status are unspecified for out-of-contract input;
        # only "no fatal signal" is a contract. See docs/parser-fuzz-corpus.md.
        status=0
        NTHREADS="$THREADS" "$BIN" "$input" >/dev/null 2>&1 || status=$?
        if ((status > 128)); then
            report_case "$name"
            echo "  optimized binary died from signal $((status - 128))" >&2
            mismatch=1
            continue
        fi
        unspecified_cases=$((unspecified_cases + 1))
        ;;
    *)
        echo "parser-fuzz: unknown case class: $class ($name)" >&2
        exit 1
        ;;
    esac
done < "$CASES"

if ((mismatch != 0)); then
    echo "parser-fuzz: FAIL (oracle comparison)" >&2
    exit 1
fi

HARNESS="$WORK/parser-fuzz-harness"
"${CC:-cc}" \
    -O1 -g -fno-omit-frame-pointer \
    -march=native -mavx2 -mbmi2 -std=c11 \
    -Wall -Wextra -Wpedantic -Werror -pthread \
    -fsanitize=address,undefined -fno-sanitize-recover=undefined \
    "-DONEBRC_SOURCE=\"$SOURCE\"" \
    tests/parser-fuzz-harness.c -o "$HARNESS" -lm

if ! ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
     UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
     "$HARNESS" "$GUARDED" > "$WORK/harness.log" 2>&1; then
    failed=$(awk -F': ' '/^case: /{name=$2} END{sub(/\.txt$/, "", name); print name}' \
        "$WORK/harness.log")
    if [[ -n "$failed" ]]; then
        report_case "$failed"
    fi
    {
        echo "parser-fuzz: FAIL (guarded sanitizer harness)"
        grep -v '^case: ' "$WORK/harness.log" | head -40
        echo "  reproduce: python3 tests/generate-fuzz-corpus.py $WORK"
        echo "             $HARNESS $GUARDED"
    } >&2
    exit 1
fi
tail -n 1 "$WORK/harness.log"

if ((KEEP == 0)); then
    if [[ -f "$MARKER" ]] && grep -Fxq "$MARKER_VALUE" "$MARKER"; then
        rm -rf -- "$WORK"
    else
        echo "parser-fuzz: refusing cleanup after ownership marker changed: $WORK" >&2
        exit 1
    fi
fi

elapsed=$(( $(date +%s) - started ))
echo "parser-fuzz: PASS ($valid_cases oracle cases," \
     "$unspecified_cases out-of-contract cases, ${elapsed}s)"
