#!/usr/bin/env python3
import pathlib
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: generate-temperature-fixture.py <output-dir>")

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


def temperature(value: int) -> str:
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    return f"{sign}{magnitude // 10}.{magnitude % 10}"


values = list(range(-999, 1000))
for chunk_id, chunk_start in enumerate(range(0, len(values), 400)):
    rows = []
    expected = []
    for station_id in range(chunk_start, min(chunk_start + 400, len(values))):
        rendered = temperature(values[station_id])
        station = f"T{station_id:04d}"
        rows.append(f"{station};{rendered}\n")
        expected.append(f"{station}={rendered}/{rendered}/{rendered}")

    stem = f"temperature-exhaustive-{chunk_id}"
    (out / f"{stem}.txt").write_text("".join(rows))
    (out / f"{stem}.expected").write_text(
        "{" + ", ".join(expected) + "}\n"
    )
