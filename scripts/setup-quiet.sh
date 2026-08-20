#!/usr/bin/env bash
# Run once per reboot, before benching, on Linux. Requires sudo.
# Sets performance governor and lowers perf_event_paranoid for profiling.
set -euo pipefail

echo "Setting performance governor on all CPUs..."
for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance | sudo tee "$c" >/dev/null
done

echo "Lowering perf_event_paranoid (for perf record/stat)..."
echo 0 | sudo tee /proc/sys/kernel/perf_event_paranoid >/dev/null

echo "Done. scripts/bench.sh validates, warms with sequential dd, and checks residency."
echo
echo "For most reliable numbers: close other applications, no browser, no IDE."
