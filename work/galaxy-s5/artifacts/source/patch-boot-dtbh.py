#!/usr/bin/env python3
"""Apply the SM-G900H DTBH IDs to the frozen boot image and refresh its ID."""

import hashlib
import struct
import sys
from pathlib import Path

EXPECTED_SHA256 = "dab39a4acfbeb07e0095445b1a2144e6a3ebd911567ebd9d45cf0ff40661699c"
EXPECTED_ENTRY = (0x152E, 0x50A6, 0x217584DA, 10, 255, 2048, 112640, 32)


def main(source: Path, destination: Path) -> None:
    old = source.read_bytes()
    assert hashlib.sha256(old).hexdigest() == EXPECTED_SHA256
    kernel_size, _, ramdisk_size, _, second_size, _, _, page_size, dt_size, _ = struct.unpack_from(
        "<10I", old, 8
    )
    sizes = (kernel_size, ramdisk_size, second_size, dt_size)
    offsets = []
    offset = page_size
    for size in sizes:
        offsets.append(offset)
        offset += (size + page_size - 1) // page_size * page_size
    dt_offset = offsets[3]
    assert old[dt_offset : dt_offset + 4] == b"DTBH"
    assert struct.unpack_from("<II", old, dt_offset + 4) == (2, 1)
    assert struct.unpack_from("<8I", old, dt_offset + 12) == EXPECTED_ENTRY

    def image_id(image: bytes) -> bytes:
        digest = hashlib.sha1()
        for offset, size in zip(offsets, sizes):
            digest.update(image[offset : offset + size])
            digest.update(struct.pack("<I", size))
        return digest.digest()

    assert old[576:596] == image_id(old)
    new = bytearray(old)
    struct.pack_into("<II", new, dt_offset + 16, 0x1E92, 0x7D64F612)
    new[576:596] = image_id(new)
    assert new[576:596] == image_id(new)
    changed = [index for index, pair in enumerate(zip(old, new)) if pair[0] != pair[1]]
    assert all(576 <= index < 596 or dt_offset + 16 <= index < dt_offset + 24 for index in changed)
    assert len(new) == len(old) == 13_490_176
    assert not destination.exists()
    destination.write_bytes(new)
    print(hashlib.sha256(new).hexdigest(), destination)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: patch-boot-dtbh.py original-boot.img output-boot.img")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
