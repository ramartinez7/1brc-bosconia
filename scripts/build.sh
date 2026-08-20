#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== building optimized C ==="
make -C c --no-print-directory

echo "=== building C correctness oracle ==="
make -C baseline --no-print-directory

echo
echo "Binaries:"
echo "  c/c-linux"
echo "  baseline/baseline"
