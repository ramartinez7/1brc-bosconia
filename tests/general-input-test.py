#!/usr/bin/env python3
import os
import pathlib
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
IMPL = (ROOT / (sys.argv[1] if len(sys.argv) > 1 else "c")).resolve()
BIN = IMPL / "c-linux"
ORACLE = ROOT / "baseline" / "baseline"
GOLDEN = 0x9E3779B97F4A7C15
SCRATCH = ROOT / ".test-work" / "general-input"

def key(name: bytes) -> int:
    first = int.from_bytes(name[:8].ljust(8, b"\0"), "little")
    bits = min(len(name) * 8, 64)
    masked = first if bits == 64 else first & ((1 << bits) - 1)
    return (((masked + len(name)) * GOLDEN) >> 32) & 0xFFFFFFFF


def dispatch_unique_names(count: int) -> list[bytes]:
    names = []
    used = set()
    candidate = 0
    while len(names) < count:
        name = f"G{candidate:07d}".encode()
        candidate += 1
        index = key(name) & 0xFFFF
        if index in used:
            continue
        used.add(index)
        names.append(name)
    return names


def write_dataset(
    path: pathlib.Path,
    names: list[bytes],
    *,
    repetitions: int = 1,
) -> None:
    with path.open("wb") as handle:
        for repetition in range(repetitions):
            for index, name in enumerate(names):
                temperature = (index + repetition) % 1999 - 999
                sign = b"-" if temperature < 0 else b""
                magnitude = abs(temperature)
                handle.write(
                    name
                    + b";"
                    + sign
                    + str(magnitude // 10).encode()
                    + b"."
                    + str(magnitude % 10).encode()
                    + b"\n"
                )


def run(binary: pathlib.Path, data: pathlib.Path, *, general: bool):
    environment = os.environ.copy()
    environment["NTHREADS"] = "4"
    if general:
        environment["ONEBRC_GENERAL"] = "1"
    return subprocess.run(
        [str(binary), str(data)],
        env=environment,
        capture_output=True,
    )


subprocess.run(["make", "-C", str(IMPL), "--no-print-directory", "-s"], check=True)
subprocess.run(["make", "-C", str(ROOT / "baseline"), "--no-print-directory", "-s"], check=True)

SCRATCH.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(
    prefix="general-input-test-",
    dir=SCRATCH,
) as temporary:
    temp = pathlib.Path(temporary)

    dense_escape = dispatch_unique_names(413) + [b"ZZZextra414"]
    for label, names, repetitions in (
        ("dense-escape-414", dense_escape, 1),
        ("generic-3000", [f"N{index:07d}".encode() for index in range(3000)], 1),
        (
            "generic-long-output",
            [
                f"L{index:03d}".encode() + b"x" * 96
                for index in range(600)
            ],
            1,
        ),
        (
            "generic-capacity-multisegment",
            [f"C{index:07d}".encode() for index in range(16384)],
            16,
        ),
    ):
        data = temp / f"{label}.txt"
        write_dataset(data, names, repetitions=repetitions)
        if label == "generic-capacity-multisegment":
            assert data.stat().st_size > 2 * 1024 * 1024
        expected = subprocess.run(
            [str(ORACLE), str(data)],
            capture_output=True,
            check=True,
        ).stdout
        actual = run(BIN, data, general=True)
        assert actual.returncode == 0, (label, actual.stderr.decode(errors="replace"))
        assert actual.stdout == expected, label

    overflow = temp / "generic-over-capacity.txt"
    write_dataset(
        overflow,
        [f"X{index:07d}".encode() for index in range(16385)],
        repetitions=16,
    )
    assert overflow.stat().st_size > 2 * 1024 * 1024
    rejected = run(BIN, overflow, general=True)
    assert rejected.returncode != 0
    assert b"table full" in rejected.stderr

print("general input: PASS")
