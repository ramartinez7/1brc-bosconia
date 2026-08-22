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

## Strict validation

Set `ONEBRC_STRICT=1` to validate the complete input before dictionary
discovery, allocation, or worker creation:

```bash
ONEBRC_STRICT=1 c/c-linux measurements.txt
ONEBRC_STRICT=1 ONEBRC_GENERAL=1 c/c-linux measurements.txt
```

Strict mode requires a non-empty file; names of 1-100 non-NUL bytes; one
separator; temperatures matching `-?[0-9]{1,2}\.[0-9]`; records no longer than
110 bytes; no CRLF line ending; and an optional missing final newline.
Carriage return remains valid inside a station name. Default strict mode
permits at most 413 distinct names. Strict general mode permits at most 16,384.

Input or configuration rejection exits `2`, writes one deterministic
diagnostic to stderr, and writes no stdout. `ONEBRC_STRICT` accepts only `0`
and `1`; `ONEBRC_GENERAL` accepts only `0` and `1`. `NTHREADS`, in every mode,
accepts only an unsigned decimal integer between 1 and the online CPU limit.

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

Without strict mode, malformed-record output and exit status remain
unspecified, but cold-path bounds prevent missing separators, overlong names,
and tail copies from reading or writing beyond their buffers. Malformed
temperature safety requires strict prevalidation. Truncated EOF cases remain
sanitizer-tested for memory safety.

## Verification

```bash
python3 tests/general-input-test.py c
python3 tests/strict-input-test.py c
```

The cardinality test covers a dispatch-unique 414-name escape case, 3,000
short names, 600 maximum-length names whose output exceeds 64 KiB, a
multi-segment 16,384-name merge, and multi-segment rejection at 16,385.

The strict test covers every rejection class above, hostile `NTHREADS` and
`ONEBRC_STRICT` values, the 413 and 16,384 distinct-name limits, and sanitizer
cleanliness on rejected input in both strict and non-strict modes.

Both run inside [`./verify.sh`](verification.md).
