#!/usr/bin/env python3
"""Deterministic parser, temperature-domain, and EOF fuzz corpus generator.

Corpus version 1. Every byte of every case is a pure function of the named
seeds and the configuration in this file: no clock, no environment, no
``random`` module, no filesystem input, and no constant taken from the
official dataset. Adversarial names are derived only from generic properties
of the key function in ``c/main.c`` -- BZHI masking of the
first eight name bytes, the ``name_len`` term, and the 32/64/96-byte name
comparison chunking.

Outputs written to ``<output-dir>``:

* ``<case>.txt``       corpus input;
* ``<case>.expected``  expected program output for in-contract cases,
                       computed here with exact integer arithmetic;
* ``cases.tsv``        one row per case with the seed that produced it;
* ``guarded.tsv``      case list for ``tests/parser-fuzz-harness.c``;
* ``manifest.json``    full versioned configuration and case metadata;
* ``corpus-version``   corpus version number.

Usage:
    python3 tests/generate-fuzz-corpus.py <output-dir> [--only CASE] [--list]
"""

import argparse
import hashlib
import json
import pathlib
import sys

CORPUS_VERSION = 1
PAGE_SIZE = 4096
MAX_NAME_BYTES = 100
# The implementation is specialized to the challenge contract of at
# most 413 distinct stations; in-contract cases never exceed it.
MAX_STATIONS = 413
MASK64 = (1 << 64) - 1
GOLDEN = 0x9E3779B97F4A7C15
TABLE_MASK = 0x3FFF
DISPATCH_MASK = 0xFFFF

# Named seeds. The values are arbitrary calendar-style constants chosen when
# the corpus was declared; changing one changes that family's bytes and must
# be accompanied by a corpus version bump.
SEEDS = {
    "utf8-names": 20260901,
    "byte-lengths": 20260902,
    "shared-prefixes": 20260903,
    "hash-collisions": 20260904,
    "temperature-domain": 20260905,
    "page-eof": 20260906,
    "invalid-records": 20260907,
    "segment-splits": 20260908,
}

# Code point pools by UTF-8 encoded width. ';' (0x3B) and '\n' (0x0A) are
# excluded because they terminate a name and a record.
ASCII_POOL = [c for c in range(0x20, 0x7F) if c != 0x3B]
TWO_BYTE_POOL = (
    list(range(0x00A1, 0x0180))
    + list(range(0x0386, 0x03CF))
    + list(range(0x0400, 0x0460))
)
THREE_BYTE_POOL = (
    list(range(0x0905, 0x0940))
    + list(range(0x3041, 0x3097))
    + list(range(0x4E00, 0x4F00))
    + list(range(0xAC00, 0xAC80))
)
FOUR_BYTE_POOL = (
    list(range(0x10400, 0x10450))
    + list(range(0x1F300, 0x1F400))
    + list(range(0x20000, 0x20080))
)
POOLS = {
    1: ASCII_POOL,
    2: TWO_BYTE_POOL,
    3: THREE_BYTE_POOL,
    4: FOUR_BYTE_POOL,
}

# Filler records of length 6..11 bytes, used to pad a case to an exact page
# multiple without adding an unbounded number of station names.
FILLER_NAMES = [b"F", b"Fa", b"Fab", b"Fabc", b"Fabcd", b"Fabcde"]


class Rng:
    """SplitMix64. Reproducible independently of the Python version."""

    def __init__(self, seed):
        self.state = seed & MASK64

    def next_u64(self):
        self.state = (self.state + GOLDEN) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    def below(self, bound):
        return self.next_u64() % bound

    def pick(self, sequence):
        return sequence[self.below(len(sequence))]


def fnv1a64(text):
    digest = 0xCBF29CE484222325
    for byte in text.encode():
        digest = ((digest ^ byte) * 0x100000001B3) & MASK64
    return digest


def case_rng(seed_name, case_name):
    """Per-case generator state, so --only reproduces one case exactly."""
    return Rng(SEEDS[seed_name] ^ fnv1a64(case_name))


def bzhi(value, index):
    """x86 BZHI: the bit count is taken from bits 7:0 of the index."""
    count = index & 0xFF
    if count >= 64:
        return value & MASK64
    return value & ((1 << count) - 1)


def station_key(name):
    """Mirror of make_key() in c/main.c."""
    first8 = int.from_bytes(name[:8].ljust(8, b"\0"), "little")
    masked = bzhi(first8, len(name) * 8)
    hashed = ((masked + len(name)) * GOLDEN) & MASK64
    return ((hashed >> 32) | 0x80000000) & 0xFFFFFFFF


def encode_name(rng, length, widths):
    """Build a valid UTF-8 name of exactly `length` bytes."""
    out = bytearray()
    while len(out) < length:
        remaining = length - len(out)
        usable = [width for width in widths if width <= remaining] or [1]
        width = rng.pick(usable)
        out += chr(rng.pick(POOLS[width])).encode()
    if out[:1] == b" ":
        out[0] = ord("_")
    if out[-1:] == b" ":
        out[-1] = ord("_")
    return bytes(out)


def unique_names(rng, count, lengths, widths, taken=None):
    names = []
    seen = set(taken or ())
    for index in range(count):
        length = lengths[index % len(lengths)]
        for _ in range(4096):
            name = encode_name(rng, length, widths)
            if name not in seen:
                break
        else:
            raise SystemExit(f"exhausted unique names at length {length}")
        seen.add(name)
        names.append(name)
    return names


def format_temperature(tenths):
    sign = "-" if tenths < 0 else ""
    magnitude = abs(tenths)
    return f"{sign}{magnitude // 10}.{magnitude % 10}"


def record(name, tenths):
    return name + b";" + format_temperature(tenths).encode() + b"\n"


def filler_record(name_length):
    return FILLER_NAMES[name_length - 1] + b";0.0\n"


def filler_block(size):
    """Exactly `size` bytes of valid filler records."""
    if size == 0:
        return b""
    if size < 6:
        raise SystemExit(f"cannot fill {size} bytes with whole records")
    remainder = size % 6
    blocks = []
    if remainder:
        blocks.append(filler_record(1 + remainder))
        size -= 6 + remainder
    blocks.extend(filler_record(1) for _ in range(size // 6))
    return b"".join(blocks)


def page_case(head, final, total=PAGE_SIZE):
    """head + filler + final, padded to exactly `total` bytes.

    `total` of None pads up to the next page multiple that leaves room for at
    least one filler record.
    """
    if total is None:
        body = len(head) + len(final)
        total = body + (-body) % PAGE_SIZE
        while total - body < 6:
            total += PAGE_SIZE
    padding = total - len(head) - len(final)
    return head + filler_block(padding) + final


def parse_records(data):
    """Strict in-contract parse; also validates what this file generates."""
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    parsed = []
    for line in lines:
        fields = line.split(b";")
        if len(fields) != 2:
            raise SystemExit(f"generated record is not in contract: {line!r}")
        name, text = fields
        if not 1 <= len(name) <= MAX_NAME_BYTES:
            raise SystemExit(f"generated name length out of contract: {line!r}")
        name.decode("utf-8")
        body = text[1:] if text[:1] == b"-" else text
        if len(body) not in (3, 4) or body[-2:-1] != b"." or not (
            body[:-2].isdigit() and body[-1:].isdigit()
        ):
            raise SystemExit(f"generated temperature malformed: {line!r}")
        tenths = int(body[:-2]) * 10 + int(body[-1:])
        if text[:1] == b"-":
            tenths = -tenths
        if not -999 <= tenths <= 999:
            raise SystemExit(f"generated temperature out of range: {line!r}")
        parsed.append((name, tenths))
    return parsed


def round_half_even(total, count):
    """Exactly the oracle's rounding: half-to-even on the magnitude."""
    magnitude = abs(total)
    quotient, remainder = divmod(magnitude, count)
    if remainder * 2 > count or (remainder * 2 == count and quotient % 2 == 1):
        quotient += 1
    return -quotient if total < 0 else quotient


def expected_output(records):
    stats = {}
    for name, tenths in records:
        entry = stats.get(name)
        if entry is None:
            stats[name] = [tenths, tenths, tenths, 1]
        else:
            entry[0] = min(entry[0], tenths)
            entry[1] += tenths
            entry[2] = max(entry[2], tenths)
            entry[3] += 1
    parts = []
    for name in sorted(stats):
        minimum, total, maximum, count = stats[name]
        parts.append(
            name.decode("utf-8")
            + "="
            + format_temperature(minimum)
            + "/"
            + format_temperature(round_half_even(total, count))
            + "/"
            + format_temperature(maximum)
        )
    return "{" + ", ".join(parts) + "}\n"


class Case:
    def __init__(self, name, family, seed, data, description, guard="generic"):
        self.name = name
        self.family = family
        self.seed = seed
        self.data = data
        self.description = description
        self.guard = guard
        if guard == "unspecified":
            self.klass = "unspecified"
            self.records = []
            self.expected = None
        else:
            self.klass = "valid"
            self.records = parse_records(data)
            distinct = {name for name, _ in self.records}
            if len(distinct) > MAX_STATIONS:
                raise SystemExit(f"{name}: {len(distinct)} names exceed contract")
            self.expected = expected_output(self.records)

    @property
    def distinct(self):
        return len({name for name, _ in self.records})

    def guard_row(self):
        if self.klass == "unspecified":
            return (self.name + ".txt", "unspecified", "-", "-", "-", "-", "-")
        extremes = {}
        for name, tenths in self.records:
            low, high = extremes.get(name, (tenths, tenths))
            extremes[name] = (min(low, tenths), max(high, tenths))
        return (
            self.name + ".txt",
            self.guard,
            str(len(self.records)),
            str(sum(tenths for _, tenths in self.records)),
            str(self.distinct),
            str(sum(low for low, _ in extremes.values())),
            str(sum(high for _, high in extremes.values())),
        )


def build_utf8_cases():
    seed = "utf8-names"
    plans = (
        ("utf8-ascii", (1,), list(range(1, 41)), "ASCII names, lengths 1-40"),
        (
            "utf8-two-byte",
            (2,),
            list(range(2, 61, 2)),
            "Latin/Greek/Cyrillic names, 2-byte code points",
        ),
        (
            "utf8-three-byte",
            (3,),
            list(range(3, 100, 3)),
            "Devanagari/Kana/CJK/Hangul names, 3-byte code points",
        ),
        (
            "utf8-four-byte",
            (4,),
            list(range(4, 101, 4)),
            "astral-plane names, 4-byte code points",
        ),
        (
            "utf8-mixed-widths",
            (1, 2, 3, 4),
            list(range(1, 101)),
            "mixed 1-4 byte code points, lengths 1-100",
        ),
    )
    cases = []
    for case_name, widths, lengths, description in plans:
        rng = case_rng(seed, case_name)
        names = unique_names(rng, 200, lengths, widths)
        rows = []
        for name in names:
            for _ in range(1 + rng.below(6)):
                rows.append(record(name, rng.below(1999) - 999))
        cases.append(Case(case_name, "utf8", seed, b"".join(rows), description))
    return cases


def build_length_cases():
    seed = "byte-lengths"
    cases = []
    for case_name, widths, description in (
        ("lengths-ascii-1-100", (1,), "one ASCII name of every byte length 1-100"),
        (
            "lengths-utf8-1-100",
            (1, 2, 3, 4),
            "one mixed-width UTF-8 name of every byte length 1-100",
        ),
    ):
        rng = case_rng(seed, case_name)
        rows = []
        seen = set()
        for length in range(1, MAX_NAME_BYTES + 1):
            for _ in range(4096):
                name = encode_name(rng, length, widths)
                if name not in seen:
                    break
            seen.add(name)
            for _ in range(3):
                rows.append(record(name, rng.below(1999) - 999))
        cases.append(Case(case_name, "lengths", seed, b"".join(rows), description))
    return cases


def build_prefix_cases():
    seed = "shared-prefixes"
    plans = (
        ("prefix-shared-8", 8, list(range(9, 60)), (1,), 300),
        ("prefix-boundary-32", 32, [33, 34, 35, 40, 47, 48, 49], (1,), 280),
        ("prefix-boundary-64", 64, [65, 66, 70, 79, 80, 81, 95], (1,), 280),
        ("prefix-boundary-96", 96, [97, 98, 99, 100], (1,), 200),
        ("prefix-utf8-shared-24", 24, list(range(25, 90, 3)), (3,), 300),
    )
    cases = []
    for case_name, shared, lengths, widths, count in plans:
        rng = case_rng(seed, case_name)
        prefix = encode_name(rng, shared, widths)
        names = []
        seen = set()
        while len(names) < count:
            length = lengths[len(names) % len(lengths)]
            name = prefix + encode_name(rng, length - shared, widths)
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
        rows = []
        for name in names:
            for _ in range(3):
                rows.append(record(name, rng.below(1999) - 999))
        cases.append(
            Case(
                case_name,
                "prefix",
                seed,
                b"".join(rows),
                f"{count} names sharing a {shared}-byte prefix and "
                f"differing from byte {shared}",
            )
        )
    return cases


def collision_search(rng, mask, lengths, widths, wanted, candidates):
    """Group generated names by masked key and return the largest bucket."""
    buckets = {}
    seen = set()
    for index in range(candidates):
        name = encode_name(rng, lengths[index % len(lengths)], widths)
        if name in seen:
            continue
        seen.add(name)
        buckets.setdefault(station_key(name) & mask, []).append(name)
    best = max(buckets.items(), key=lambda item: (len(item[1]), -item[0]))[1]
    if len(best) < wanted:
        raise SystemExit(
            f"collision search found {len(best)} names, wanted {wanted}"
        )
    return best[:wanted]


def build_collision_cases():
    seed = "hash-collisions"
    cases = []

    # BZHI takes its bit count from bits 7:0 of name_len * 8, so every name of
    # length 32, 64, or 96 masks to zero and shares one key: one probe chain.
    for length, count in ((32, 200), (64, 150), (96, 120)):
        case_name = f"collision-key-len{length}"
        rng = case_rng(seed, case_name)
        names = unique_names(rng, count, [length], (1, 2, 3, 4))
        assert len({station_key(name) for name in names}) == 1
        rows = []
        for name in names:
            for _ in range(4):
                rows.append(record(name, rng.below(1999) - 999))
        cases.append(
            Case(
                case_name,
                "collision",
                seed,
                b"".join(rows),
                f"{count} names of {length} bytes sharing one 32-bit key",
            )
        )

    # Length 40 masks 64 bits, so a shared eight-byte prefix is a shared key.
    case_name = "collision-key-prefix8"
    rng = case_rng(seed, case_name)
    prefix = encode_name(rng, 8, (1,))
    names = [prefix + encode_name(rng, 32, (1, 2, 3, 4)) for _ in range(120)]
    names = sorted(set(names))
    assert len({station_key(name) for name in names}) == 1
    rows = []
    for name in names:
        for _ in range(3):
            rows.append(record(name, rng.below(1999) - 999))
    cases.append(
        Case(
            case_name,
            "collision",
            seed,
            b"".join(rows),
            f"{len(names)} 40-byte names sharing the hashed eight-byte prefix",
        )
    )

    # Distinct keys that land in one hot-table slot: probe chains with key
    # comparisons that must fail before the name comparison runs.
    for case_name, lengths in (
        ("collision-slot-long", list(range(12, 20))),
        ("collision-slot-short", list(range(1, 8))),
    ):
        rng = case_rng(seed, case_name)
        names = collision_search(rng, TABLE_MASK, lengths, (1,), 10, 60000)
        rows = []
        for name in names:
            for _ in range(5):
                rows.append(record(name, rng.below(1999) - 999))
        cases.append(
            Case(
                case_name,
                "collision",
                seed,
                b"".join(rows),
                f"{len(names)} names colliding in one hot-table slot",
            )
        )

    # Exactly 413 names with distinct dispatch slots: the dense path engages.
    case_name = "dense-413-distinct"
    rng = case_rng(seed, case_name)
    names = list(FILLER_NAMES)
    used = {station_key(name) & DISPATCH_MASK for name in names}
    if len(used) != len(names):
        raise SystemExit("filler names collide in the dispatch table")
    while len(names) < MAX_STATIONS:
        name = encode_name(rng, 1 + rng.below(MAX_NAME_BYTES), (1, 2, 3, 4))
        slot = station_key(name) & DISPATCH_MASK
        if slot in used or name in names:
            continue
        used.add(slot)
        names.append(name)
    rows = [record(name, rng.below(1999) - 999) for name in names]
    for _ in range(600):
        rows.append(record(rng.pick(names), rng.below(1999) - 999))
    cases.append(
        Case(
            case_name,
            "collision",
            seed,
            page_case(b"".join(rows), b"", None),
            "413 names with distinct dispatch slots; dense path, page-aligned",
            guard="dense",
        )
    )

    # Exactly 413 names where two share a dispatch slot: dense must fall back.
    case_name = "dense-413-dispatch-collision"
    rng = case_rng(seed, case_name)
    twins = unique_names(rng, 2, [32], (1, 2))
    assert (station_key(twins[0]) & DISPATCH_MASK) == (
        station_key(twins[1]) & DISPATCH_MASK
    )
    names = list(FILLER_NAMES) + twins
    used = {station_key(name) & DISPATCH_MASK for name in names}
    while len(names) < MAX_STATIONS:
        name = encode_name(rng, 1 + rng.below(MAX_NAME_BYTES), (1, 2, 3, 4))
        slot = station_key(name) & DISPATCH_MASK
        if slot in used or name in names:
            continue
        used.add(slot)
        names.append(name)
    rows = [record(name, rng.below(1999) - 999) for name in names]
    for _ in range(600):
        rows.append(record(rng.pick(names), rng.below(1999) - 999))
    cases.append(
        Case(
            case_name,
            "collision",
            seed,
            page_case(b"".join(rows), b"", None),
            "413 names with a dispatch collision; generic fallback, page-aligned",
        )
    )
    return cases


def build_temperature_cases():
    """Every value from -99.9 through 99.9 in 0.1 steps, twice over."""
    seed = "temperature-domain"
    domain = list(range(-999, 1000))
    cases = []

    chunk = 400
    for index, start in enumerate(range(0, len(domain), chunk)):
        case_name = f"temperature-domain-{index}"
        rng = case_rng(seed, case_name)
        values = domain[start:start + chunk]
        names = unique_names(rng, len(values), list(range(1, 41)), (1, 2, 3, 4))
        rows = [record(name, value) for name, value in zip(names, values)]
        cases.append(
            Case(
                case_name,
                "temperature",
                seed,
                b"".join(rows),
                f"values {format_temperature(values[0])} to "
                f"{format_temperature(values[-1])} as single-record stations",
            )
        )

    # The same 1,999 values again, this time aggregated so that min, max, and
    # the rounded mean are exercised over the whole domain.
    for index, start in enumerate(range(0, len(domain), 1000)):
        case_name = f"temperature-aggregate-{index}"
        rng = case_rng(seed, case_name)
        values = domain[start:start + 1000]
        names = unique_names(rng, 100, list(range(1, 101)), (1, 2, 3, 4))
        rows = [
            record(names[position % len(names)], value)
            for position, value in enumerate(values)
        ]
        cases.append(
            Case(
                case_name,
                "temperature",
                seed,
                b"".join(rows),
                f"{len(values)} values aggregated across {len(names)} stations",
            )
        )

    # Exact half-way means, which must round to even in both implementations.
    case_name = "temperature-half-even-ties"
    rng = case_rng(seed, case_name)
    names = unique_names(rng, 200, list(range(1, 101)), (1, 2, 3, 4))
    rows = []
    for index, name in enumerate(names):
        count = (2, 4, 6, 10)[index % 4]
        center = index * 9 - 900
        center = max(-990 + count, min(990 - count, center))
        values = [center] * (count - 1)
        values.append(center * count + count // 2 - sum(values))
        if not all(-999 <= value <= 999 for value in values):
            continue
        assert sum(values) * 2 == count * (2 * center + 1)
        for value in values:
            rows.append(record(name, value))
    cases.append(
        Case(
            case_name,
            "temperature",
            seed,
            b"".join(rows),
            "stations whose mean is exactly a half tenth",
        )
    )

    # "-0.0" is a distinct rendering of zero that both implementations accept.
    case_name = "temperature-negative-zero"
    rng = case_rng(seed, case_name)
    names = unique_names(rng, 20, list(range(1, 101)), (1, 2, 3, 4))
    rows = []
    for name in names:
        rows.append(name + b";-0.0\n")
        rows.append(record(name, 0))
        rows.append(record(name, -1))
    cases.append(
        Case(
            case_name,
            "temperature",
            seed,
            b"".join(rows),
            "negative-zero temperature rendering",
        )
    )
    return cases


EOF_NAME_LENGTHS = (1, 2, 7, 8, 9, 15, 16, 31, 32, 33, 63, 64, 65, 95, 96, 99, 100)
EOF_TEMPERATURES = ("0.0", "-0.0", "9.9", "-9.9", "99.9", "-99.9")


def build_eof_cases():
    """Final records that end exactly on a page boundary."""
    seed = "page-eof"
    cases = []
    for index, length in enumerate(EOF_NAME_LENGTHS):
        temperature = EOF_TEMPERATURES[index % len(EOF_TEMPERATURES)]
        for mode in ("new", "existing"):
            case_name = f"eof-page-len{length}-{mode}"
            rng = case_rng(seed, case_name)
            name = encode_name(rng, length, (1, 2, 3, 4))
            final = name + b";" + temperature.encode() + b"\n"
            head = record(name, -37) if mode == "existing" else b""
            cases.append(
                Case(
                    case_name,
                    "eof",
                    seed,
                    page_case(head, final),
                    f"{length}-byte {mode} name and {temperature} at a page EOF",
                )
            )

    for length in (8, 32, 100):
        case_name = f"eof-page-len{length}-no-newline"
        rng = case_rng(seed, case_name)
        name = encode_name(rng, length, (1, 2, 3, 4))
        final = name + b";-99.9"
        cases.append(
            Case(
                case_name,
                "eof",
                seed,
                page_case(record(name, 12), final),
                f"{length}-byte name, final record without a newline at page EOF",
            )
        )

    for length in (33, 65, 100):
        case_name = f"eof-two-page-len{length}"
        rng = case_rng(seed, case_name)
        name = encode_name(rng, length, (1, 2, 3, 4))
        final = name + b";-99.9\n"
        cases.append(
            Case(
                case_name,
                "eof",
                seed,
                page_case(record(name, 5), final, 2 * PAGE_SIZE),
                f"{length}-byte name at the end of a two-page file",
            )
        )
    return cases


TRUNCATION_POINTS = (1, 2, 7, 8, 9, 31, 32, 33, 63, 64, 65, 95, 96, 99, 100)


def build_invalid_cases():
    """Out-of-contract inputs: output is unspecified, memory safety is not."""
    seed = "invalid-records"
    cases = []
    for cut in TRUNCATION_POINTS:
        case_name = f"truncated-name-{cut}"
        rng = case_rng(seed, case_name)
        name = encode_name(rng, MAX_NAME_BYTES, (1, 2, 3, 4))
        cases.append(
            Case(
                case_name,
                "truncated",
                seed,
                page_case(b"", name[:cut]),
                f"page-aligned cut {cut} bytes into a 100-byte name",
                guard="unspecified",
            )
        )

    for suffix in (b"", b"-", b"9", b"9.", b"-9", b"-9.", b"-99."):
        label = suffix.decode().replace("-", "neg").replace(".", "dot") or "semicolon"
        for length in (8, 100):
            case_name = f"truncated-temp-{label}-{length}"
            rng = case_rng(seed, case_name)
            name = encode_name(rng, length, (1, 2, 3, 4))
            cases.append(
                Case(
                    case_name,
                    "truncated",
                    seed,
                    page_case(record(name, 101), name + b";" + suffix),
                    f"page-aligned cut inside the temperature after a "
                    f"{length}-byte name",
                    guard="unspecified",
                )
            )

    rng = case_rng(seed, "malformed-empty-name")
    filler = filler_block(PAGE_SIZE - 5 * 100)
    cases.append(
        Case(
            "malformed-empty-name",
            "malformed",
            seed,
            filler + b";1.0\n" * 100,
            "records with an empty station name",
            guard="unspecified",
        )
    )
    name = encode_name(rng, 12, (1, 2))
    cases.append(
        Case(
            "malformed-out-of-range-temp",
            "malformed",
            seed,
            page_case(b"", (name + b";100.0\n") * 32),
            "three-digit temperatures outside the challenge range",
            guard="unspecified",
        )
    )
    cases.append(
        Case(
            "malformed-empty-file",
            "malformed",
            seed,
            b"",
            "an empty file",
            guard="unspecified",
        )
    )
    # Newline-terminated or mid-file records with no ';' are deliberately
    # absent: the parser searches for the separator without an upper
    # bound. EOF truncations inside a final name are covered above.
    return cases


def build_segment_cases():
    """A file larger than the 2 MiB worker segment, page-aligned."""
    seed = "segment-splits"
    case_name = "segments-multi-2mib"
    rng = case_rng(seed, case_name)
    names = unique_names(rng, 300, list(range(1, 101)), (1, 2, 3, 4))
    target = 5 * (1 << 20)
    rows = []
    size = 0
    while size < target:
        row = record(rng.pick(names), rng.below(1999) - 999)
        rows.append(row)
        size += len(row)
    body = b"".join(rows)
    padding = (-len(body)) % PAGE_SIZE
    if padding and padding < 6:
        padding += PAGE_SIZE
    data = body + filler_block(padding)
    return [
        Case(
            case_name,
            "segments",
            seed,
            data,
            "page-aligned file spanning several 2 MiB worker segments",
        )
    ]


BUILDERS = (
    build_utf8_cases,
    build_length_cases,
    build_prefix_cases,
    build_collision_cases,
    build_temperature_cases,
    build_eof_cases,
    build_invalid_cases,
    build_segment_cases,
)


def check_coverage(cases):
    """Fail the build unless the promised domains are actually exercised.

    Coverage is asserted against the families that promise it, not against
    incidental random draws in other families.
    """
    temperatures = set()
    lengths = set()
    for case in cases:
        if case.klass != "valid":
            continue
        if case.family == "temperature":
            temperatures.update(tenths for _, tenths in case.records)
        if case.family == "lengths":
            lengths.update(len(name) for name, _ in case.records)
    missing = sorted(set(range(-999, 1000)) - temperatures)
    if missing:
        raise SystemExit(
            f"temperature coverage gap: {len(missing)} values, "
            f"first {format_temperature(missing[0])}"
        )
    missing = sorted(set(range(1, MAX_NAME_BYTES + 1)) - lengths)
    if missing:
        raise SystemExit(f"name length coverage gap: {missing}")


def build_all(only=None):
    cases = []
    for builder in BUILDERS:
        for case in builder():
            if only is None or case.name == only:
                cases.append(case)
    if only is not None and not cases:
        raise SystemExit(f"unknown case: {only}")
    names = [case.name for case in cases]
    if len(set(names)) != len(names):
        raise SystemExit("duplicate case name")
    if only is None:
        check_coverage(cases)
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--only", help="generate a single named case")
    parser.add_argument(
        "--list",
        action="store_true",
        help="list case names without writing files",
    )
    args = parser.parse_args()

    if args.list:
        for case in build_all(args.only):
            print(case.name)
        return

    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    cases = build_all(args.only)

    case_rows = ["\t".join(
        ("case", "family", "seed", "seed_value", "class", "bytes", "records",
         "names", "description")
    )]
    guard_rows = []
    manifest_cases = []
    for case in cases:
        (out / f"{case.name}.txt").write_bytes(case.data)
        if case.expected is not None:
            (out / f"{case.name}.expected").write_text(
                case.expected, encoding="utf-8"
            )
        case_rows.append("\t".join((
            case.name,
            case.family,
            case.seed,
            str(SEEDS[case.seed]),
            case.klass,
            str(len(case.data)),
            str(len(case.records)),
            str(case.distinct),
            case.description,
        )))
        guard_rows.append("\t".join(case.guard_row()))
        manifest_cases.append({
            "case": case.name,
            "family": case.family,
            "seed": case.seed,
            "seed_value": SEEDS[case.seed],
            "class": case.klass,
            "guard_mode": case.guard,
            "bytes": len(case.data),
            "records": len(case.records),
            "names": case.distinct,
            "page_aligned": len(case.data) % PAGE_SIZE == 0,
            "sha256": hashlib.sha256(case.data).hexdigest(),
            "description": case.description,
        })

    (out / "cases.tsv").write_text("\n".join(case_rows) + "\n", encoding="utf-8")
    (out / "guarded.tsv").write_text("\n".join(guard_rows) + "\n", encoding="utf-8")
    (out / "corpus-version").write_text(f"{CORPUS_VERSION}\n", encoding="utf-8")
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "corpus_version": CORPUS_VERSION,
                "generator_sha256": hashlib.sha256(
                    pathlib.Path(__file__).read_bytes()
                ).hexdigest(),
                "seeds": SEEDS,
                "cases": manifest_cases,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"corpus v{CORPUS_VERSION}: {len(cases)} cases, "
        f"{sum(len(case.data) for case in cases)} bytes -> {out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
