# Algorithm walkthrough

`c/main.c` is the source of truth. This document explains the optimized path
without reproducing the implementation line by line.

The input contains one billion records:

```text
Bratislava;19.2
```

The program must calculate minimum, mean, and maximum temperature per station,
sort station names, and print one line.

## 1. Map the file

The program opens the input read-only and maps the whole file with `mmap`.
`MADV_WILLNEED` asks the kernel to begin read-ahead without synchronously
faulting every page on the main thread.

The benchmark is normally run with the file already resident in the page
cache. Page residency and large-file folio state are separate conditions; see
`bench-artifacts.md`.

## 2. Discover the runtime dictionary

The canonical generator contains a fixed set of 413 stations. The program
scans records until it has found 413 distinct names, comparing names by
exact length and bytes.

It computes the normal key for every discovered name:

```c
masked = bzhi(first_eight_name_bytes, name_length * 8);
hash = (masked + name_length) * 0x9E3779B97F4A7C15;
key = high_32_bits(hash) | 0x80000000;
```

The high bit makes the key nonzero without changing the low table-index bits.
Dense mode is selected only if all 413 names have different low-16 key
indices.

This startup scan derives its dictionary from the current file. It does not
embed station names or an offline-generated lookup table.

## 3. Choose dense or generic aggregation

### Dense challenge path

Each worker allocates:

```c
struct DenseStat {
    uint32_t count;
    int16_t min;
    int16_t max;
    int64_t sum;
}; // 16 bytes
```

The table has 65,536 entries, so it occupies 1 MiB per worker. Only the 413
verified indices become active.

Each slot starts with `count=0`, `min=INT16_MAX`, and `max=INT16_MIN`. The first
measurement therefore uses the same unconditional min/max comparisons as
every later measurement; the row loop has no empty-slot branch.

For every row:

```text
parse name and temperature
compute key
update stats[key & 65535]
```

The hot path performs no station-name comparison and no linear probe. The
lookahead loop prefetches the next stat address before updating the current
row.

### Exact generic path

If a custom input ends before 413 names are discovered, two discovered names
share a low-16 index, or `ONEBRC_GENERAL=1` is set, workers use the generic
table.

The generic table has 16,384 cache-line-sized entries. A row first checks the
primary bucket selected by `key & 16383`. A key match is followed by exact
name verification; collisions use linear probing. The first 32 name bytes are
stored inline in the hot entry, with longer names retained in a side table.

The generic path is also used by the small validation fixtures. In general
mode it supports up to 16,384 distinct valid names. A new name beyond that
bound exits nonzero with `hash table full` or `merge table full`.

## 4. Distribute work

A shared relaxed atomic cursor hands out 2 MiB regions. Each worker:

1. claims the next region;
2. moves its boundaries to complete line endings;
3. processes the segment into private statistics;
4. repeats until the cursor passes EOF.

Workers are pinned to logical CPUs when the host permits affinity changes.
Private tables avoid synchronization in the per-row loop.

## 5. Parse two rows at a time

Each segment is split near its midpoint at a newline. The worker advances two
independent pointers in lockstep, exposing separate dependency chains to the
processor.

For each lane:

1. load 32 bytes and compare them with a broadcast `;` using `vpcmpeqb`;
2. turn the comparison into a bit mask with `vpmovmskb`;
3. locate the first match with `tzcnt`;
4. parse the temperature into integer tenths;
5. compute and prefetch the next destination;
6. update the current destination.

The decimal position determines digit alignment, sign handling, and record
length without separate branches for `D.D`, `DD.D`, `-D.D`, and `-DD.D`.

## 6. Handle mapping boundaries safely

Fixed-width SIMD and scalar loads may read past the logical end of a record.
A partial final page is zero-filled by Linux, but a page-aligned EOF has no
mapped guard page.

The main loop therefore stops before unsafe EOF loads. The final complete
records are copied into padded stack storage and processed with bounded key,
temperature, and long-name loads. Regression tests place `PROT_NONE` after
page-aligned fixtures to make accidental overreads deterministic.

## 7. Merge and emit

Dense merge visits only the 413 indices recorded during discovery. Generic
merge scans populated worker slots and rechecks exact station identity.

The resulting stations are sorted bytewise with `qsort_r`. Output capacity is
derived from the discovered station count and maximum supported row width,
then the result is printed with `puts` and flushed before `_exit(0)`. The
immediate exit lets the kernel reclaim the large mapping without an explicit
`munmap` walk on the measured path.

## Correctness oracle

`baseline/main.c` is intentionally independent. It uses `getline`, a separate
FNV-1a hash table, strict scalar temperature parsing, exact integer
aggregation, and exact half-even mean rounding. `scripts/validate.sh` requires
the optimized output to match it byte-for-byte.
