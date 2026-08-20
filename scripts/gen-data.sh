#!/usr/bin/env bash
# Generates a dataset using the OFFICIAL Gunnar Morling generator.
# Usage:
#   ./scripts/gen-data.sh [OUT] [N]
# Defaults:
#   OUT = ./measurements_1B.txt
#   N   = 1000000000
# Requires: ~14 GB free disk on the filesystem holding OUT, Git, and JDK 21+.
set -euo pipefail

OUT_ARG="${1:-measurements_1B.txt}"
N="${2:-1000000000}"

# Resolve OUT to an absolute path so the symlink trick below works regardless
# of where the script is invoked from.
OUT=$(realpath -m "$OUT_ARG")

# If the target file already has N lines, skip — generation takes minutes.
if [ -f "$OUT" ] && [ ! -L "$OUT" ]; then
    LINES=$(wc -l < "$OUT")
    if [ "$LINES" = "$N" ]; then
        echo "$OUT already has $N lines, skipping."
        exit 0
    fi
fi

# Pre-check disk space on the target filesystem. The Java generator emits
# ~13.8 GB for N=1B (~14 bytes/row).
OUT_DIR=$(dirname "$OUT")
mkdir -p "$OUT_DIR"
NEED_BYTES=$(( N * 15 ))  # 15 B/row is a safe upper bound
AVAIL_BYTES=$(df -B1 --output=avail "$OUT_DIR" | tail -1)
if [ "$AVAIL_BYTES" -lt "$NEED_BYTES" ]; then
    NEED_H=$(numfmt --to=iec --suffix=B "$NEED_BYTES")
    AVAIL_H=$(numfmt --to=iec --suffix=B "$AVAIL_BYTES")
    echo "ERROR: not enough free space on $(df -h "$OUT_DIR" | tail -1 | awk '{print $6}')." >&2
    echo "       need ~$NEED_H, have $AVAIL_H." >&2
    exit 1
fi

UPSTREAM=/tmp/1brc-official

if [ ! -d "$UPSTREAM" ]; then
    git clone --depth 1 https://github.com/gunnarmorling/1brc.git "$UPSTREAM"
fi
cd "$UPSTREAM"
if [ ! -f target/average-1.0.0-SNAPSHOT.jar ]; then
    ./mvnw -q -DskipTests -ntp package
fi

# The Java generator always writes to ./measurements.txt in its working dir.
# Stream the bytes through a symlink so they land on OUT's filesystem directly
# (avoids "Disk quota exceeded" when /tmp is a small tmpfs).
rm -f "$UPSTREAM/measurements.txt" "$OUT"
ln -s "$OUT" "$UPSTREAM/measurements.txt"

if ! java --class-path target/average-1.0.0-SNAPSHOT.jar dev.morling.onebrc.CreateMeasurements "$N"; then
    unlink "$UPSTREAM/measurements.txt"
    rm -f "$OUT"
    echo "ERROR: generator failed; partial file removed." >&2
    exit 1
fi

unlink "$UPSTREAM/measurements.txt"
cd - >/dev/null

SIZE=$(stat -c %s "$OUT")
SIZE_H=$(numfmt --to=iec --suffix=B "$SIZE")
LINES=$(wc -l < "$OUT")
echo "Generated $OUT ($LINES lines, $SIZE_H)"
