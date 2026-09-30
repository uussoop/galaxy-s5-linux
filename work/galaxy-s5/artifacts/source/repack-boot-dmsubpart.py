#!/usr/bin/env python3
"""Add the guarded device-mapper subpartition bypass to the installed BOOT image.

The base is the currently installed and verified native BOOT candidate
(artifacts/boot-k3gxx-r11-subpartfix-35de59d0.img). Only init_functions.sh
changes in the CPIO archive: the two "# >>> dm-bypass: ... (begin)" .. "# <<<
dm-bypass: ... (end)" regions of the replacement are stripped again and the
result must equal the base's own init_functions.sh byte for byte, so the new
image differs from the installed one by exactly one variable. The r1 kernel,
the DT, the CACHE diagnostic logger and the losetup fallback are untouched.
"""

import gzip
import hashlib
import io
import struct
import sys
from pathlib import Path

BASE_SHA256 = "35de59d0d876747e6e00ff7a63887af92e5c02baf3614a075781795a55094a60"
BASE_RAMDISK_SHA256 = "20df4ee0efec141b6b0d53744fbc697a615ec648008fc2b8e30b90233b309e98"
KERNEL_SHA256 = "6d0c337411dcbfe6771347446af8fcab19bb5bff265496a5cddaa272de631b8d"
DT_SHA256 = "f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b"
BOOT_LIMIT = 13_631_488
TARGET = "init_functions.sh"

# Must stay in sync with check-delta.py, which proves the same property
# against the checked-in base source file.
REGIONS = [
    ("# >>> dm-bypass: helpers (begin)", "# <<< dm-bypass: helpers (end)"),
    (
        "# >>> dm-bypass: mount_subpartitions call (begin)",
        "# <<< dm-bypass: mount_subpartitions call (end)",
    ),
]


def align(n, block=4):
    return (n + block - 1) // block * block


def strip_regions(data: bytes) -> bytes:
    """Drop each marked region, including the whole marker line, so what is
    left is the file the region was inserted into."""
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


def replace_cpio(raw, replacement):
    """Return raw with its single TARGET entry replaced, asserting first that
    replacement is the original entry plus exactly the marked regions."""
    out = bytearray()
    pos = 0
    count = 0
    while pos < len(raw):
        header = raw[pos : pos + 110]
        assert header[:6] == b"070701"
        fields = [int(header[6 + n * 8 : 14 + n * 8], 16) for n in range(13)]
        name_end = pos + 110 + fields[11]
        name = raw[pos + 110 : name_end - 1].decode()
        data_start = align(name_end)
        end = align(data_start + fields[6])
        if name == TARGET:
            original = raw[data_start : data_start + fields[6]]
            for begin, end_marker in REGIONS:
                assert replacement.count(begin.encode()) == 1, begin
                assert replacement.count(end_marker.encode()) == 1, end_marker
            stripped = strip_regions(replacement)
            assert stripped == original, (
                f"{TARGET} is not the base file plus the marked regions "
                f"({len(stripped)} stripped vs {len(original)} original bytes)"
            )
            assert len(replacement) > len(original), "bypass added no bytes"
            new_header = bytearray(header)
            new_header[54:62] = f"{len(replacement):08x}".encode()
            out.extend(new_header)
            out.extend(raw[pos + 110 : data_start])
            out.extend(replacement)
            out.extend(b"\0" * (align(len(replacement)) - len(replacement)))
            count += 1
        else:
            out.extend(raw[pos:end])
        pos = end
        if name == "TRAILER!!!":
            out.extend(raw[end:])
            break
    assert count == 1, f"expected exactly one {TARGET} entry, replaced {count}"
    return bytes(out)


def run(base_path, functions_path, output_path):
    base = base_path.read_bytes()
    assert hashlib.sha256(base).hexdigest() == BASE_SHA256
    assert base[:8] == b"ANDROID!"
    kernel_size, _, ramdisk_size, _, second_size, _, _, page_size, dt_size, _ = struct.unpack_from("<10I", base, 8)
    assert second_size == 0 and page_size == 2048
    kernel_off = page_size
    ramdisk_off = kernel_off + align(kernel_size, page_size)
    dt_off = ramdisk_off + align(ramdisk_size, page_size)
    kernel = base[kernel_off : kernel_off + kernel_size]
    ramdisk = base[ramdisk_off : ramdisk_off + ramdisk_size]
    dt = base[dt_off : dt_off + dt_size]
    assert hashlib.sha256(kernel).hexdigest() == KERNEL_SHA256
    assert hashlib.sha256(ramdisk).hexdigest() == BASE_RAMDISK_SHA256
    assert hashlib.sha256(dt).hexdigest() == DT_SHA256
    cpio = replace_cpio(gzip.decompress(ramdisk), functions_path.read_bytes())
    stream = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=stream, compresslevel=9, mtime=0) as gz:
        gz.write(cpio)
    new_ramdisk = stream.getvalue()
    header = bytearray(base[:page_size])
    struct.pack_into("<I", header, 16, len(new_ramdisk))
    digest = hashlib.sha1()
    for part in (kernel, new_ramdisk, b"", dt):
        digest.update(part)
        digest.update(struct.pack("<I", len(part)))
    header[576:596] = digest.digest()
    image = bytes(header)
    for part in (kernel, new_ramdisk, dt):
        image += part + b"\0" * (align(len(part), page_size) - len(part))
    assert len(image) <= BOOT_LIMIT, f"image {len(image)} exceeds BOOT {BOOT_LIMIT}"
    assert not output_path.exists(), f"{output_path} already exists"
    output_path.write_bytes(image)
    print(hashlib.sha256(image).hexdigest(), len(image), output_path)
    print("ramdisk", hashlib.sha256(new_ramdisk).hexdigest(), len(new_ramdisk))
    print(f"headroom {BOOT_LIMIT - len(image)} bytes")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: repack-boot-dmsubpart.py base-boot.img init_functions.sh output.img")
    run(*(Path(x) for x in sys.argv[1:]))
