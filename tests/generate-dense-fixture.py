#!/usr/bin/env python3
import hashlib
import pathlib
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: generate-dense-fixture.py <output-dir>")

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
mask64 = (1 << 64) - 1


def key(name: bytes) -> int:
    first8 = int.from_bytes(name[:8].ljust(8, b"\0"), "little")
    bits = len(name) * 8
    masked = first8 if bits >= 64 else first8 & ((1 << bits) - 1)
    hashed = ((masked + len(name)) * 0x9E3779B97F4A7C15) & mask64
    return ((hashed >> 32) | 0x80000000) & 0xFFFFFFFF


names = []
used = set()
candidate = 0
while len(names) < 413:
    name = f"S{candidate:06d}".encode()
    candidate += 1
    index = key(name) & 0xFFFF
    if index in used:
        continue
    used.add(index)
    names.append(name)

with (out / "dense-413.txt").open("wb") as handle:
    for name in names:
        handle.write(name + b";1.0\n")

stats = "=1.0/1.0/1.0"
expected = "{" + ", ".join(
    name.decode() + stats for name in sorted(names)
) + "}\n"
(out / "dense-413.expected").write_text(expected)

page_names = [b"A"]
page_used = {key(b"A") & 0xFFFF}
candidate = 0
while len(page_names) < 413:
    name = f"S{candidate:06d}".encode()
    candidate += 1
    index = key(name) & 0xFFFF
    if index in page_used:
        continue
    page_used.add(index)
    page_names.append(name)

page_rows = [b"A;0.0\n"]
page_rows.extend(name + b";0.0\n" for name in page_names[1:])
page_rows.append(b"A;-99.9\n")
page_rows.extend(b"A;0.0\n" for _ in range(539))
page_data = b"".join(page_rows)
assert len(page_data) == 8192
(out / "dense-page-eof.txt").write_bytes(page_data)

page_expected = []
for name in sorted(page_names):
    if name == b"A":
        stats = "=-99.9/-0.2/0.0"
    else:
        stats = "=0.0/0.0/0.0"
    page_expected.append(name.decode() + stats)
(out / "dense-page-eof.expected").write_text(
    "{" + ", ".join(page_expected) + "}\n"
)

generic_page_data = b"Alpha;1.0\n" * 409 + b"A;0.0\n"
assert len(generic_page_data) == 4096
(out / "generic-page-eof.txt").write_bytes(generic_page_data)
(out / "generic-page-eof.expected").write_text(
    "{A=0.0/0.0/0.0, Alpha=1.0/1.0/1.0}\n"
)

long_name = b"L" * 41
generic_long_existing = (
    long_name + b";0.0\n"
    + b"Alpha;1.0\n" * 399
    + b"A;0.0\n"
    + b"ABC;0.0\n"
    + long_name + b";0.0\n"
)
assert len(generic_long_existing) == 4096
(out / "generic-page-eof-long-existing.txt").write_bytes(
    generic_long_existing
)
(out / "generic-page-eof-long-existing.expected").write_text(
    "{A=0.0/0.0/0.0, ABC=0.0/0.0/0.0, Alpha=1.0/1.0/1.0, "
    + long_name.decode() + "=0.0/0.0/0.0}\n"
)

generic_long_new = b"Alpha;1.0\n" * 405 + long_name + b";0.0\n"
assert len(generic_long_new) == 4096
(out / "generic-page-eof-long-new.txt").write_bytes(generic_long_new)
(out / "generic-page-eof-long-new.expected").write_text(
    "{Alpha=1.0/1.0/1.0, " + long_name.decode() + "=0.0/0.0/0.0}\n"
)

long_name_65 = b"M" * 65
generic_long_65_existing = (
    long_name_65 + b";0.0\n"
    + b"Alpha;1.0\n" * 395
    + b"A;0.0\n"
    + long_name_65 + b";0.0\n"
)
assert len(generic_long_65_existing) == 4096
(out / "generic-page-eof-long-65-existing.txt").write_bytes(
    generic_long_65_existing
)
(out / "generic-page-eof-long-65-existing.expected").write_text(
    "{A=0.0/0.0/0.0, Alpha=1.0/1.0/1.0, "
    + long_name_65.decode() + "=0.0/0.0/0.0}\n"
)

generic_long_65_new = (
    b"Alpha;1.0\n" * 402
    + b"A;0.0\n"
    + long_name_65 + b";0.0\n"
)
assert len(generic_long_65_new) == 4096
(out / "generic-page-eof-long-65-new.txt").write_bytes(generic_long_65_new)
(out / "generic-page-eof-long-65-new.expected").write_text(
    "{A=0.0/0.0/0.0, Alpha=1.0/1.0/1.0, "
    + long_name_65.decode() + "=0.0/0.0/0.0}\n"
)

long_key_names = []
long_key_used = set()
candidate = 0
while len(long_key_names) < 413:
    length = (32, 64, 96)[candidate % 3]
    digest = hashlib.sha512(f"dense-long-key:{candidate}".encode()).hexdigest()
    name = (digest * 4)[:length].encode()
    candidate += 1
    index = key(name) & 0xFFFF
    if index in long_key_used:
        continue
    long_key_used.add(index)
    long_key_names.append(name)

with (out / "dense-long-key-fallback.txt").open("wb") as handle:
    for name in long_key_names:
        handle.write(name + b";1.0\n")
(out / "dense-long-key-fallback.expected").write_text(
    "{" + ", ".join(
        name.decode() + "=1.0/1.0/1.0"
        for name in sorted(long_key_names)
    ) + "}\n"
)
