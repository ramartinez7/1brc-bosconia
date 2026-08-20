# Benchmark methodology and results

This document records the environment and dataset details for the performance
numbers published in `README.md`.

## Metrics

- **Mean elapsed:** average wall-clock duration.
- **Standard deviation:** run-to-run variability.
- **Fastest:** minimum wall-clock duration.
- **Mean CPU:** cumulative user plus system CPU time across all worker
  threads.

Each table summarizes 20 executions of the C implementation under a controlled
randomized schedule.

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

## Ryzen 7 1700 — 2026-08-20

Source:

- tracked file `c/main.c`
- source SHA-256
  `113515be47d2dcc95215df4b6439162a25e402faa4488acd27cc2ba596448091`

Dataset:

- official Java generator
- 1,000,000,000 rows
- 13,795,530,003 bytes
- MD5 `8d7d4130c179c42990f4e8cfe63af853`
- SHA-256
  `5dd374d3f059c3fb8ab730bd922c0962958ece823253f84adefda07cd3c2e514`
- output SHA-256
  `5a935340d5d98cf1a0c8936a64224ed15fd2d353c3c745abd6f592e198382c27`

Environment:

- AMD Ryzen 7 1700, 8 cores / 16 threads
- 32 GiB DDR4-2667
- Ubuntu 26.04, kernel 7.0.0-15
- gcc 15.2.0
- governor `performance`
- THP `madvise`
- all 3,368,050 pages resident
- about 13.46 GB `FileHugePages`
- monitoring and security scans stopped

Twenty executions, seed `20261216`, after two warmup blocks:

| Mean elapsed | Std. dev. | Fastest | Mean CPU |
|---:|---:|---:|---:|
| **674.5 ms** | 13.1 ms | 652.9 ms | 10,350 ms |

No execution was flagged for interference. The measured binary SHA-256 begins
`71507359...`.

## Azure Standard_F16as_v6 — 2026-08-20

Source:

- tracked file `c/main.c`
- source SHA-256
  `113515be47d2dcc95215df4b6439162a25e402faa4488acd27cc2ba596448091`

Dataset:

- official Java generator
- 13,795,344,915 bytes
- MD5 `7e37860ce7b7e5fc7ebbcebe92d156e2`
- all 3,368,005 pages resident
- `FileHugePages` 13,430,784 kB
- output SHA-256
  `4f4797ccfda99e9b6f6581ac5746f4274d2b8a53a4b77442bd31f7b9af7b7436`

Environment:

- AMD EPYC 9V74
- 16 physical cores, no SMT
- 62 GiB RAM
- Ubuntu 24.04, Azure kernel 6.17
- gcc 13.3.0
- THP `madvise`
- zero reported steal time

Twenty executions, seed `20261219`, after two warmup blocks:

| Mean elapsed | Std. dev. | Fastest | Mean CPU |
|---:|---:|---:|---:|
| **263.5 ms** | 0.7 ms | 262.2 ms | 4,089 ms |

No execution was flagged for interference. The VM exposed no hardware PMU
event source, so process CPU time is the available resource metric. The
measured binary SHA-256 begins `624e12b9...`.
