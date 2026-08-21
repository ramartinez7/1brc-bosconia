# Benchmark methodology and results

This document records the environment and dataset details for the performance
numbers published in `README.md`.

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

## Ryzen 7 1700 — 2026-08-21

Source:

- tracked file `c/main.c`
- source SHA-256
  `e5979b03395ab7a8f64ac5b5b8e404695bf948f367c59fe8d0d760fecbd279cf`

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

Twenty executions after five warmups:

| Mean elapsed | Std. dev. | Fastest | Mean CPU |
|---:|---:|---:|---:|
| **635.7 ms** | 6.5 ms | 628.8 ms | 9,918 ms |

The measured binary SHA-256 is
`81f2a7b6cfda954563e3d30a79fc9292989e0d885a13527773eec724f032a126`.

## Azure Standard_F16as_v6 — 2026-08-21

Source:

- tracked file `c/main.c`
- source SHA-256
  `e5979b03395ab7a8f64ac5b5b8e404695bf948f367c59fe8d0d760fecbd279cf`

Dataset:

- official Java generator
- 13,795,344,915 bytes
- MD5 `7e37860ce7b7e5fc7ebbcebe92d156e2`
- all 3,368,005 pages resident
- `FileHugePages` 13,225,984 kB
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

Twenty executions after five warmups:

| Mean elapsed | Std. dev. | Fastest | Mean CPU |
|---:|---:|---:|---:|
| **265.5 ms** | 0.4 ms | 264.9 ms | 4,118 ms |

The VM exposed no hardware PMU event source, so process CPU time is the
available resource metric. The measured binary SHA-256 is
`c3cb1147c05f0663df69434780145cc2812b33735eddeaf520135e9dde637623`.
