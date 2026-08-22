# Parser and EOF fuzz corpus

A deterministic corpus that attacks the parser, the temperature domain, the
hash and dispatch paths, and the end of the mapping. It runs in seconds, needs
no dataset, and reproduces byte-for-byte from named seeds.

```bash
bash tests/parser-fuzz.sh          # compare against the oracle
bash tests/parser-fuzz.sh --keep   # leave the work directory in place
```

`./verify.sh` runs the same entry point, so every verification and every
validation includes the corpus.

## What it covers

`tests/generate-fuzz-corpus.py` builds 102 cases, 140,275 records, 6.5 MB.

| Family | Cases | What it attacks |
| --- | --- | --- |
| `utf8` | 5 | ASCII, 2-, 3-, and 4-byte code points, mixed widths |
| `lengths` | 2 | every name byte length 1-100, ASCII and UTF-8 |
| `prefix` | 5 | long shared prefixes, including exactly 32, 64, and 96 bytes |
| `collision` | 8 | equal `make_key` values, equal table slots, 413-name dense and dispatch-collision dictionaries |
| `temperature` | 9 | every value -99.9 to 99.9, aggregated means, exact half-tenth ties, `-0.0` |
| `eof` | 40 | page-aligned files ending in a name of length 1-100 and each temperature form |
| `truncated` | 29 | prefixes of valid files cut inside the final name or temperature |
| `malformed` | 3 | empty name, out-of-range temperature, empty file |
| `segments` | 1 | a file spanning several 2 MiB worker segments |

Names are arbitrary UTF-8 byte sequences that exclude only `;` and `\n`.
Adversarial names are derived from the implementation's own hashing in
[`c/main.c`](../c/main.c), never from dataset content:

- `make_key` uses `_bzhi_u64(first8, name_len * 8)`, and x86 BZHI reads its bit
  count from bits 7:0 of the index. Lengths 32, 64, and 96 therefore zero the
  whole word, so *all* names of those lengths share one key. The corpus builds
  crowds of such names.
- Names of length 40 hash only their first 8 bytes, so shared 8-byte prefixes
  collide. `collision-key-prefix8` exploits that.
- `collision-slot-long` and `collision-slot-short` place distinct keys in the
  same open-addressed slot to force probe chains.
- `dense-413-dispatch-collision` denies the dense dictionary by colliding two
  names in `key & DISPATCH_MASK`, which exercises the generic fallback with a
  full 413-station dictionary.

Every valid case caps distinct names at 413 and name length at 100 bytes,
because the default mode is specialized for the challenge's station
cardinality and name bound.

Sorting is checked on bytes: the generated expectation orders names by raw
`bytes` comparison, so a locale-sensitive collation would fail the comparison.

## Determinism

- `CORPUS_VERSION` in the generator versions the whole corpus.
- Eight named seeds, one per family group, are constants in the generator:
  `utf8-names`, `byte-lengths`, `shared-prefixes`, `hash-collisions`,
  `temperature-domain`, `page-eof`, `invalid-records`, `segment-splits`.
- Each case draws from `SplitMix64(seed ^ fnv1a64(case_name))`, not from
  `random` and not from the clock, so a single case regenerates identically
  without generating its neighbours.
- `manifest.json` records the corpus version, the generator's SHA-256, the seed
  table, and a SHA-256 per case.
- Page alignment in the generator assumes a 4 KiB page; the harness reads the
  real page size from `sysconf`, so guard pages hold regardless.
- A failure prints the family, seed name, seed value, class, size, and the exact
  reproduction command:

```
parser-fuzz: FAIL: prefix-boundary-32
  corpus:      version 1
  family:      prefix
  seed:        shared-prefixes = 20260903
  class:       valid
  bytes:       39693  records: 840  names: 280
  description: 280 names sharing a 32-byte prefix and differing from byte 32
  reproduce:   python3 tests/generate-fuzz-corpus.py .test-work/fuzz --only prefix-boundary-32
               c/c-linux .test-work/fuzz/prefix-boundary-32.txt
```

The generator refuses to emit an incomplete corpus: it asserts that the
temperature family covers all 1,999 values and that the length family covers all
100 byte lengths.

## How a case is checked

Valid cases are checked three ways:

1. `baseline/baseline` must agree with the expectation computed in Python, which
   is an independent implementation of the output format and of half-to-even
   rounding.
2. The optimized binary must match the oracle byte-for-byte at one thread and at
   several threads, so segment splitting and merge order are covered.
3. A sanitizer harness includes the implementation source and drives its worker
   loops directly under ASan and UBSan, with `-fno-sanitize-recover=undefined`,
   over a mapping that ends in an inaccessible page.

## Guard pages

[`tests/parser-fuzz-harness.c`](../tests/parser-fuzz-harness.c) maps
`round_up(size, page) + page`, marks the last page `PROT_NONE`, and copies the
case flush against that boundary. Every case therefore has a hard EOF, not only
the ones whose size is a page multiple, and any read past the last byte faults
deterministically instead of landing in readable padding.

The `eof`, `truncated`, `malformed`, and `segments` families are also generated
at exact page multiples so the program's own `mmap` ends on a page boundary
during the oracle comparison. `eof` walks the final name across the 8-, 32-,
64-, and 96-byte load boundaries used by `parse_line`, `name_eq_hot`, and
`make_key`, for both a new and an already-seen station.

The harness checks the dense path when the dictionary engages and the generic
path otherwise, and asserts that the dictionary decides as expected.

## Input contract

The challenge input is well formed, and the default mode is written for it. The
corpus states, rather than invents, what happens outside that contract.

**In contract** — every `valid` case. Output must match the oracle exactly.
A record is `name;temperature\n`, the name is 1-100 UTF-8 bytes without `;` or
`\n`, the temperature is `-99.9` to `99.9` with exactly one decimal, at most 413
distinct names appear. A complete final record may end either with `\n` or
directly at EOF; the no-newline EOF cases are valid and oracle-compared.

**Unspecified** — the `truncated` and `malformed` cases, which are marked
`unspecified` in `cases.tsv`. These are prefixes of valid files, or files whose
records the oracle rejects. The contract tested is memory safety only:

- no read past the end of the mapping, verified against the guard page;
- no ASan or UBSan diagnostic;
- no fatal signal.

Exit status and output are deliberately not asserted in that class. The program
may print partial aggregates, may exit non-zero, and may disagree with the
oracle. The oracle itself rejects these inputs with a diagnostic and a non-zero
status.

`ONEBRC_STRICT=1` converts the unspecified class into a specified rejection:
exit status `2`, empty stdout, and one deterministic diagnostic. See
[`docs/general-input.md`](general-input.md) for the strict contract, and
[`tests/strict-input-test.py`](../tests/strict-input-test.py) for its tests.
