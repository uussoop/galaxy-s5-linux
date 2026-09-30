#!/usr/bin/env python3
"""Repack the reviewed DTBH-corrected boot image with kernel package r1."""

import hashlib
import struct
import sys
from pathlib import Path

BASE_SHA256 = "cd25de6c65609309f9e93fb4ec2564304ee752eac76704d9080b3d8cc8ae4d05"
KERNEL_SHA256 = "6d0c337411dcbfe6771347446af8fcab19bb5bff265496a5cddaa272de631b8d"
DT_SHA256 = "f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b"


def repack(base: Path, kernel_path: Path, dt_path: Path, output: Path) -> None:
    old = base.read_bytes()
    kernel = kernel_path.read_bytes()
    dt = dt_path.read_bytes()
    assert hashlib.sha256(old).hexdigest() == BASE_SHA256
    assert hashlib.sha256(kernel).hexdigest() == KERNEL_SHA256
    assert hashlib.sha256(dt).hexdigest() == DT_SHA256
    old_kernel_size, _, ramdisk_size, _, second_size, _, _, page_size, dt_size, _ = struct.unpack_from(
        "<10I", old, 8
    )
    assert (old_kernel_size, len(kernel), dt_size) == (7_281_488, 7_281_496, len(dt))
    offsets = []
    offset = page_size
    for size in (old_kernel_size, ramdisk_size, second_size, dt_size):
        offsets.append(offset)
        offset += (size + page_size - 1) // page_size * page_size
    assert old[offsets[3] : offsets[3] + dt_size] == dt
    assert not any(old[page_size + old_kernel_size : page_size + len(kernel)])

    new = bytearray(old)
    new[page_size : page_size + len(kernel)] = kernel
    struct.pack_into("<I", new, 8, len(kernel))
    digest = hashlib.sha1()
    for start, size in zip(offsets, (len(kernel), ramdisk_size, second_size, dt_size)):
        digest.update(new[start : start + size])
        digest.update(struct.pack("<I", size))
    new[576:596] = digest.digest()
    assert new[offsets[1] : offsets[1] + ramdisk_size] == old[offsets[1] : offsets[1] + ramdisk_size]
    assert len(new) == len(old) == 13_490_176
    assert not output.exists()
    output.write_bytes(new)
    print(hashlib.sha256(new).hexdigest(), output)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit("usage: repack-boot-kernel-r1.py old-boot.img zImage dt.img new-boot.img")
    repack(*(Path(argument) for argument in sys.argv[1:]))
