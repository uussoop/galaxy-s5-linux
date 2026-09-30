#!/usr/bin/env python3
"""Independently verify a repacked BOOT image against its base.

Checks that the r1 kernel, the DT and the boot header fields are byte-identical
to the base, that exactly one CPIO entry (init_functions.sh) differs, that the
new entry is the base entry plus only the marked dm-bypass regions, and that the
image still fits the BOOT partition. Prints the CACHE diagnostic logger's
presence so the thing that has to keep working is confirmed, not assumed.
"""

import gzip
import hashlib
import struct
import sys
from pathlib import Path

BOOT_LIMIT = 13_631_488
TARGET = "init_functions.sh"
REGIONS = [
    ("# >>> dm-bypass: helpers (begin)", "# <<< dm-bypass: helpers (end)"),
    (
        "# >>> dm-bypass: mount_subpartitions call (begin)",
        "# <<< dm-bypass: mount_subpartitions call (end)",
    ),
]
LOGGER_MARK = b"/sbin/s5diag start"


def align(n, block=4):
    return (n + block - 1) // block * block


def sections(image):
    kernel_size, _, ramdisk_size, _, second_size, _, _, page_size, dt_size, _ = struct.unpack_from("<10I", image, 8)
    assert image[:8] == b"ANDROID!" and second_size == 0 and page_size == 2048
    k_off = page_size
    r_off = k_off + align(kernel_size, page_size)
    d_off = r_off + align(ramdisk_size, page_size)
    return {
        "header": image[:page_size],
        "kernel": image[k_off : k_off + kernel_size],
        "ramdisk": image[r_off : r_off + ramdisk_size],
        "dt": image[d_off : d_off + dt_size],
    }


def cpio_entries(raw):
    entries, pos = {}, 0
    while pos < len(raw):
        header = raw[pos : pos + 110]
        assert header[:6] == b"070701"
        f = [int(header[6 + n * 8 : 14 + n * 8], 16) for n in range(13)]
        name_end = pos + 110 + f[11]
        name = raw[pos + 110 : name_end - 1].decode()
        data_start = align(name_end)
        end = align(data_start + f[6])
        if name == "TRAILER!!!":
            break
        assert name not in entries, f"duplicate cpio entry {name}"
        entries[name] = raw[data_start : data_start + f[6]]
        pos = end
    return entries


def strip_regions(data: bytes) -> bytes:
    lines = data.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        line = lines[i].rstrip(b"\n")
        begin = next((b for b, _ in REGIONS if line.endswith(b.encode())), None)
        if begin is None:
            out.append(lines[i])
            i += 1
            continue
        end = next(e for b, e in REGIONS if b == begin)
        j = i + 1
        while j < len(lines) and not lines[j].rstrip(b"\n").endswith(end.encode()):
            j += 1
        assert j < len(lines), f"unterminated region {begin!r}"
        i = j + 1
    return b"".join(out)


def main(base_path, new_path, expected_source_path):
    base, new = base_path.read_bytes(), new_path.read_bytes()
    b, n = sections(base), sections(new)
    expected = expected_source_path.read_bytes()

    print(f"base  {hashlib.sha256(base).hexdigest()[:16]} {len(base)} B")
    print(f"new   {hashlib.sha256(new).hexdigest()[:16]} {len(new)} B")

    # Boot header: only the ramdisk size and the recomputed image ID may move.
    bh, nh = bytearray(b["header"]), bytearray(n["header"])
    assert struct.unpack_from("<I", bh, 16)[0] == len(b["ramdisk"])
    assert struct.unpack_from("<I", nh, 16)[0] == len(n["ramdisk"])
    bh[16:20], nh[16:20] = b"\0" * 4, b"\0" * 4
    bh[576:596], nh[576:596] = b"\0" * 20, b"\0" * 20
    assert bh == nh, "boot header changed outside ramdisk size and image ID"
    print("  header      OK (only ramdisk size + image ID differ)")

    assert b["kernel"] == n["kernel"], "kernel changed"
    print(f"  kernel      OK {len(n['kernel'])} B, sha {hashlib.sha256(n['kernel']).hexdigest()[:16]}")
    assert b["dt"] == n["dt"], "DT changed"
    print(f"  dt          OK {len(n['dt'])} B, sha {hashlib.sha256(n['dt']).hexdigest()[:16]}")

    be, ne = cpio_entries(gzip.decompress(b["ramdisk"])), cpio_entries(gzip.decompress(n["ramdisk"]))
    assert set(be) == set(ne), "cpio entry set changed"
    changed = sorted(k for k in be if be[k] != ne[k])
    assert changed == [TARGET], f"expected only {TARGET} to change, got {changed}"
    print(f"  cpio        OK {len(ne)} entries, only {TARGET} differs")

    assert ne[TARGET] == expected, "shipped init_functions.sh is not the source file"
    stripped = strip_regions(ne[TARGET])
    assert stripped == be[TARGET], "delta is not limited to the marked regions"
    for begin, end in REGIONS:
        assert ne[TARGET].count(begin.encode()) == 1 and ne[TARGET].count(end.encode()) == 1
    print(f"  {TARGET} OK {len(be[TARGET])} B -> {len(ne[TARGET])} B, {len(ne[TARGET]) - len(be[TARGET])} B in 2 marked regions")

    assert LOGGER_MARK in ne["init"], "CACHE diagnostic logger missing from new init"
    assert be["init"] == ne["init"], "init changed"
    print("  init        OK byte-identical, CACHE diagnostic logger present")

    assert b'local losetup_args="--show -Pf"' in ne[TARGET]
    assert b"--direct-io=on" not in ne[TARGET].split(b"mount_subpartitions() {")[1][:400]
    print("  fallback    OK losetup still '--show -Pf', no --direct-io=on re-added")

    assert len(new) <= BOOT_LIMIT, f"image {len(new)} exceeds BOOT {BOOT_LIMIT}"
    print(f"  size        OK {len(new)} B, {BOOT_LIMIT - len(new)} B headroom in BOOT")
    print("VERIFY PASSED")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: verify-boot-dmsubpart.py base.img new.img init_functions.sh")
    main(*(Path(x) for x in sys.argv[1:]))
