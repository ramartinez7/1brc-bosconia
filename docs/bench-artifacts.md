# Benchmark methodology and results

This document defines how a publishable performance number is produced and
what must be recorded beside it.

## Metrics

- **Mean elapsed:** average wall-clock duration.
- **Standard deviation:** run-to-run variability.
- **Fastest:** minimum wall-clock duration.
- **Mean CPU:** cumulative user plus system CPU time across all worker
  threads.

Each table summarizes 20 executions after five warmups.

## mmap preflight

Full page residency is necessary but not sufficient for stable mmap
measurements. Random page touching can produce 100% residency while replacing
large file folios with small pages, increasing minor faults and reducing
effective parallelism.

The publication preflight is:

```bash
sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
dd if="$DATA" of=/dev/null bs=8M status=progress
fincore -o RES,PAGES,SIZE "$DATA"
grep '^FileHugePages:' /proc/meminfo
./scripts/build.sh
perf stat -e task-clock,page-faults -- ./c/c-linux "$DATA" >/dev/null
```

`./scripts/build.sh` creates `c/c-linux` locally. The executable is a
gitignored build output, not a tracked repository path.

Record page residency, `FileHugePages`, minor faults, and task-clock relative
to elapsed time. Publish results only when the CPU governor, background load,
and file-folio state are controlled.

## Publishing a measurement

A measurement is published only together with:

- the tracked source path and its SHA-256;
- the measured binary SHA-256;
- the generator, row count, byte count, and MD5 of the input file;
- the SHA-256 of the program output for that input;
- the host CPU, memory, distribution, kernel, and compiler versions;
- governor, transparent-huge-page mode, page residency, and `FileHugePages`;
- the number of warmups and timed executions.

A measurement describes exactly one source revision, one host, and one input
file. When the tracked source changes, its measurements are re-run and
republished; they are never reattributed to the new source. There is no
published measurement for the current source.
