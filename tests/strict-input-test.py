#!/usr/bin/env python3
"""Strict-input and runtime-envelope contract test.

Covers ``ONEBRC_STRICT`` rejection of out-of-contract records, ``NTHREADS``
argument validation, the bounded non-strict cold paths, and sanitizer
cleanliness on rejected input. Needs no dataset, no network, and no root.

Usage: python3 tests/strict-input-test.py [implementation-dir]
"""

import os
import pathlib
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
IMPL = (ROOT / (sys.argv[1] if len(sys.argv) > 1 else "c")).resolve()
BIN = IMPL / "c-linux"
BASELINE = ROOT / "baseline" / "baseline"
SCRATCH = ROOT / ".test-work" / "strict-input"
# NTHREADS is rejected above the online CPU limit, so the accepted values used
# here must stay within this host's limit.
THREADS = str(min(4, len(os.sched_getaffinity(0))))


def run(data, *, strict="1", general=None, threads=THREADS):
    environment = os.environ.copy()
    environment["ONEBRC_STRICT"] = strict
    environment["NTHREADS"] = threads
    if general is None:
        environment.pop("ONEBRC_GENERAL", None)
    else:
        environment["ONEBRC_GENERAL"] = general
    return subprocess.run(
        [str(BIN), str(data)],
        env=environment,
        capture_output=True,
        check=False,
    )


def require_rejection(path, payload, token):
    path.write_bytes(payload)
    result = run(path)
    assert result.returncode == 2, (token, result.returncode, result.stderr)
    assert result.stdout == b"", (token, result.stdout[:80])
    assert token.encode() in result.stderr, (token, result.stderr)
    assert result.returncode < 128


subprocess.run(["make", "-s", "-C", str(IMPL)], check=True)
subprocess.run(["make", "-s", "-C", str(ROOT / "baseline")], check=True)

SCRATCH.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(
    prefix="strict-input-test-",
    dir=SCRATCH,
) as temporary:
    temp = pathlib.Path(temporary)
    valid = temp / "valid.txt"
    valid.write_bytes(b"Alpha;-1.0\nBeta;2.5\nAlpha;3.0")
    expected = subprocess.run(
        [str(BASELINE), str(valid)],
        capture_output=True,
        check=True,
    ).stdout
    for threads in ("1", THREADS):
        result = run(valid, threads=threads)
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected

    cases = {
        "empty-input": (b"", "empty-input"),
        "empty-name": (b";1.0\n", "empty-name"),
        "missing-separator": (b"A\n", "missing-separator"),
        "name-too-long": (b"A" * 101 + b";1.0\n", "name-too-long"),
        "nul-name": (b"A\x00B;1.0\n", "nul-name"),
        "bad-temperature": (b"A;1\n", "bad-temperature"),
        "bad-temperature-2": (b"A;1.23\n", "bad-temperature"),
        "bad-temperature-3": (b"A;100.0\n", "bad-temperature"),
        "bad-temperature-4": (b"A;+1.0\n", "bad-temperature"),
        "bad-temperature-5": (b"A;--1.0\n", "bad-temperature"),
        "bad-temperature-6": (b"A;1.a\n", "bad-temperature"),
        "bad-temperature-7": (b"A;-1.\n", "bad-temperature"),
        "line-too-long": (b"A" * 109 + b";1.0\n", "line-too-long"),
        "crlf": (b"A;1.0\r\n", "crlf"),
        "blank-line": (b"\n", "empty-name"),
    }
    invalid = temp / "invalid.txt"
    for _, (payload, token) in cases.items():
        require_rejection(invalid, payload, token)

    invalid_strict = run(valid, strict="yes")
    assert invalid_strict.returncode == 2
    assert invalid_strict.stdout == b""
    assert b"ONEBRC_STRICT" in invalid_strict.stderr

    for hostile in (
        "",
        "0",
        "-1",
        "+1",
        " 1",
        "1 ",
        "1x",
        "999999",
        "9999999999999999999999999999999999999999",
    ):
        result = run(valid, threads=hostile)
        assert result.returncode == 2, (hostile, result.returncode, result.stderr)
        assert result.stdout == b""
        assert b"NTHREADS" in result.stderr

    names_414 = temp / "names-414.txt"
    names_414.write_bytes(
        b"".join(f"S{index:03d};1.0\n".encode() for index in range(414))
    )
    rejected = run(names_414)
    assert rejected.returncode == 2
    assert rejected.stdout == b""
    assert b"too-many-names" in rejected.stderr
    accepted = run(names_414, general="1")
    assert accepted.returncode == 0, accepted.stderr

    invalid.write_bytes(b"A\n")
    bounded_separator = run(invalid, strict="0")
    assert bounded_separator.returncode < 128

    invalid.write_bytes(b"A" * 101 + b";1.0\n")
    bounded_name = run(invalid, strict="0")
    assert bounded_name.returncode in (1, 2)
    assert bounded_name.stdout == b""

    names_16384 = temp / "names-16384.txt"
    names_16384.write_bytes(
        b"".join(f"X{index:05d};1.0\n".encode() for index in range(16_384))
    )
    maximum = run(names_16384, general="1")
    assert maximum.returncode == 0, maximum.stderr
    with names_16384.open("ab") as handle:
        handle.write(b"X16384;1.0\n")
    overflow = run(names_16384, general="1")
    assert overflow.returncode == 2
    assert overflow.stdout == b""
    assert b"too-many-names" in overflow.stderr

    sanitizer = temp / "strict-sanitizer"
    subprocess.run(
        [
            "cc",
            "-O1",
            "-g",
            "-std=c11",
            "-march=native",
            "-mavx2",
            "-mbmi2",
            "-pthread",
            "-fsanitize=address,undefined",
            "-fno-sanitize-recover=undefined",
            str(IMPL / "main.c"),
            "-lm",
            "-o",
            str(sanitizer),
        ],
        check=True,
    )
    sanitizer_env = os.environ.copy()
    sanitizer_env.update(
        {
            "ONEBRC_STRICT": "1",
            "NTHREADS": THREADS,
            "ASAN_OPTIONS": "abort_on_error=1:detect_leaks=0",
            "UBSAN_OPTIONS": "halt_on_error=1",
        }
    )
    for payload in (
        b"A\n",
        b"A" * 103 + b";1.0\n",
        b"A;1000.0\n",
        b"A;999999\n",
    ):
        invalid.write_bytes(payload)
        checked = subprocess.run(
            [str(sanitizer), str(invalid)],
            env=sanitizer_env,
            capture_output=True,
            check=False,
        )
        assert checked.returncode == 2, checked.stderr
        assert checked.stdout == b""
        assert b"AddressSanitizer" not in checked.stderr
        assert b"runtime error:" not in checked.stderr

    nonstrict_sanitizer_env = sanitizer_env.copy()
    nonstrict_sanitizer_env["ONEBRC_STRICT"] = "0"
    for payload in (b"A" * 106 + b";1.0\n",):
        invalid.write_bytes(payload)
        checked = subprocess.run(
            [str(sanitizer), str(invalid)],
            env=nonstrict_sanitizer_env,
            capture_output=True,
            check=False,
        )
        assert checked.returncode in (1, 2), checked.stderr
        assert checked.returncode < 128
        assert checked.stdout == b""
        assert b"AddressSanitizer" not in checked.stderr
        assert b"runtime error:" not in checked.stderr

print("strict input: PASS")
