#!/usr/bin/env python3
"""Add temporary CACHE diagnostics to the verified r1 boot ramdisk."""

import gzip
import hashlib
import io
import struct
import sys
from pathlib import Path

BASE_SHA256 = "47e7241722b5d87508be4f25f796acb338a7bc4f6dd77ebaf9da70674eed660b"
KERNEL_SHA256 = "6d0c337411dcbfe6771347446af8fcab19bb5bff265496a5cddaa272de631b8d"
DT_SHA256 = "f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b"
MAX_BOOT = 13_631_488


def aligned(n, size=4):
    return (n + size - 1) // size * size


def cpio_entry(name, data, ino):
    name = name.encode() + b"\0"
    fields = (ino, 0o100755, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0)
    header = b"070701" + b"".join(f"{field:08x}".encode() for field in fields)
    assert len(header) == 110
    start = header + name
    start += b"\0" * (aligned(len(start)) - len(start))
    return start + data + b"\0" * (aligned(len(data)) - len(data))


def patch_cpio(raw, source):
    updates = {
        "init": (source / "init").read_bytes(),
        "init_2nd.sh": (source / "init_2nd.sh").read_bytes(),
    }
    diag = (source / "s5diag").read_bytes()
    out = bytearray()
    pos = 0
    found = set()
    max_ino = 0
    while pos < len(raw):
        header = raw[pos : pos + 110]
        assert header[:6] == b"070701"
        fields = [int(header[6 + i * 8 : 14 + i * 8], 16) for i in range(13)]
        max_ino = max(max_ino, fields[0])
        name_end = pos + 110 + fields[11]
        name = raw[pos + 110 : name_end - 1].decode()
        data_start = aligned(name_end)
        end = aligned(data_start + fields[6])
        if name == "TRAILER!!!":
            assert set(updates) == found
            out.extend(cpio_entry("usr/bin/s5diag", diag, max_ino + 1))
            out.extend(raw[pos:end])
            out.extend(raw[end:])
            break
        if name in updates:
            data = updates[name]
            assert fields[1] & 0o170000 == 0o100000
            modified = bytearray(header)
            modified[54:62] = f"{len(data):08x}".encode()
            out.extend(modified)
            out.extend(raw[pos + 110 : data_start])
            out.extend(data)
            out.extend(b"\0" * (aligned(len(data)) - len(data)))
            found.add(name)
        else:
            out.extend(raw[pos:end])
        pos = end
    else:
        raise AssertionError("missing CPIO trailer")
    return bytes(out)


def run(base_path, source, output):
    base = base_path.read_bytes()
    assert hashlib.sha256(base).hexdigest() == BASE_SHA256
    assert base[:8] == b"ANDROID!"
    kernel_size, _, ramdisk_size, _, second_size, _, _, page_size, dt_size, _ = struct.unpack_from("<10I", base, 8)
    assert page_size == 2048 and second_size == 0
    kernel_offset = page_size
    ramdisk_offset = kernel_offset + aligned(kernel_size, page_size)
    second_offset = ramdisk_offset + aligned(ramdisk_size, page_size)
    dt_offset = second_offset + aligned(second_size, page_size)
    kernel = base[kernel_offset : kernel_offset + kernel_size]
    ramdisk = base[ramdisk_offset : ramdisk_offset + ramdisk_size]
    dt = base[dt_offset : dt_offset + dt_size]
    assert hashlib.sha256(kernel).hexdigest() == KERNEL_SHA256
    assert hashlib.sha256(dt).hexdigest() == DT_SHA256
    new_cpio = patch_cpio(gzip.decompress(ramdisk), source)
    stream = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=stream, compresslevel=9, mtime=0) as gz:
        gz.write(new_cpio)
    new_ramdisk = stream.getvalue()
    header = bytearray(base[:page_size])
    struct.pack_into("<I", header, 16, len(new_ramdisk))
    digest = hashlib.sha1()
    for part in (kernel, new_ramdisk, b"", dt):
        digest.update(part)
        digest.update(struct.pack("<I", len(part)))
    header[576:596] = digest.digest()
    image = bytes(header) + kernel + b"\0" * (aligned(len(kernel), page_size) - len(kernel))
    image += new_ramdisk + b"\0" * (aligned(len(new_ramdisk), page_size) - len(new_ramdisk))
    image += dt + b"\0" * (aligned(len(dt), page_size) - len(dt))
    assert len(image) <= MAX_BOOT
    assert not output.exists()
    output.write_bytes(image)
    print(f"{hashlib.sha256(image).hexdigest()} {len(image)} {output}")
    print(f"ramdisk {hashlib.sha256(new_ramdisk).hexdigest()} {len(new_ramdisk)}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: repack-boot-diagnostic.py r1-boot.img source-dir output.img")
    run(*(Path(x) for x in sys.argv[1:]))
