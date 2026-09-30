#!/usr/bin/env python3
"""Replace only init_functions.sh in the verified diagnostic BOOT ramdisk."""

import gzip
import hashlib
import io
import struct
import sys
from pathlib import Path

BASE_SHA256 = "31e2fccefa297c5eadf8d69bc4f91e9dfce29d47a8601cbd83eb41363887b5ae"
KERNEL_SHA256 = "6d0c337411dcbfe6771347446af8fcab19bb5bff265496a5cddaa272de631b8d"
DT_SHA256 = "f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b"
BOOT_LIMIT = 13_631_488


def align(n, block=4):
    return (n + block - 1) // block * block


def replace_cpio(raw, replacement):
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
        if name == "init_functions.sh":
            assert raw[data_start : data_start + fields[6]].count(b'local losetup_args="--show -Pf --direct-io=on"') == 1
            assert replacement == raw[data_start : data_start + fields[6]].replace(
                b'local losetup_args="--show -Pf --direct-io=on"', b'local losetup_args="--show -Pf"'
            )
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
    assert count == 1
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
    assert len(image) <= BOOT_LIMIT and not output_path.exists()
    output_path.write_bytes(image)
    print(hashlib.sha256(image).hexdigest(), len(image), output_path)
    print("ramdisk", hashlib.sha256(new_ramdisk).hexdigest(), len(new_ramdisk))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: repack-boot-subpartfix.py diagnostic-boot.img init_functions.sh output.img")
    run(*(Path(x) for x in sys.argv[1:]))
