# General-input contract

The default execution mode is specialized for the challenge. Set
`ONEBRC_GENERAL=1` to force the exact generic engine when the input can contain
more than 413 distinct station names:

```bash
ONEBRC_GENERAL=1 c/c-linux measurements.txt
```

## Supported input

Both modes require:

- non-empty input;
- records `name;temperature`, separated by `\n`;
- an optional missing final newline;
- station names of 1-100 non-NUL bytes, with no `;` or `\n`;
- names treated as opaque bytes and sorted by unsigned byte order;
- temperatures from `-99.9` through `99.9`, with exactly one decimal digit;
- at most 4,294,967,295 rows for any one station.

The implementation does not validate UTF-8. Valid UTF-8 names are supported,
but other non-NUL, non-delimiter bytes have the same byte-oriented semantics.
Embedded NUL is excluded because process output is emitted as a C string.

## Cardinality modes

| Mode | Distinct-name bound | Behavior |
|---|---:|---|
| default | 0-413 | Uses dense specialization only after discovering 413 collision-free dispatch indices; otherwise uses the generic engine. |
| `ONEBRC_GENERAL=1` | 0-16,384 | Always uses the exact generic hash table. Output storage is sized from the discovered cardinality. |
| general mode above 16,384 | unsupported | Exits nonzero with `hash table full` or `merge table full`; it never emits a partial success result. |

The default mode intentionally does not claim correctness above 413 names.
Use general mode for broader cardinality. General mode adds no branch or state
to the row-processing loop; the environment decision occurs before workers are
created.

## Out of contract

Malformed temperatures, empty names, names longer than 100 bytes, records
without `;`, truncated records, and lines longer than 110 bytes remain
unsupported. Complete page-aligned EOF cases are sanitizer-tested; malformed
or truncated output and exit status are unspecified.

## Verification

```bash
python3 tests/general-input-test.py c
```

The deterministic test covers a dispatch-unique 414-name escape case, 3,000
short names, 600 maximum-length names whose output exceeds 64 KiB, a
multi-segment 16,384-name merge, and multi-segment rejection at 16,385.
