#!/usr/bin/env python3
"""Assert that dm-ramdisk/init_functions.sh differs from its base only by the
two marked device-mapper bypass regions.

The base is the init_functions.sh of the currently installed BOOT candidate,
artifacts/source/subpartition-ramdisk/init_functions.sh. Removing the two
"# >>> dm-bypass: ... (begin)" .. "# <<< dm-bypass: ... (end)" regions from the
new file must reproduce the base byte for byte, so the new boot image changes
exactly one thing: the guarded device-mapper attempt in mount_subpartitions().
"""

import sys
from pathlib import Path

REGIONS = [
    ("# >>> dm-bypass: helpers (begin)", "# <<< dm-bypass: helpers (end)"),
    (
        "# >>> dm-bypass: mount_subpartitions call (begin)",
        "# <<< dm-bypass: mount_subpartitions call (end)",
    ),
]

HERE = Path(__file__).resolve().parent


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
        # Drop the whole marker line, including its indentation.
        j = i + 1
        while j < len(lines) and not lines[j].rstrip(b"\n").endswith(end.encode()):
            j += 1
        assert j < len(lines), f"unterminated region {begin!r}"
        i = j + 1
    return b"".join(out)


def main() -> int:
    base_path = HERE.parent / "subpartition-ramdisk" / "init_functions.sh"
    new_path = HERE / "init_functions.sh"
    base, new = base_path.read_bytes(), new_path.read_bytes()

    for begin, end in REGIONS:
        assert new.count(begin.encode()) == 1, f"expected exactly one {begin!r}"
        assert new.count(end.encode()) == 1, f"expected exactly one {end!r}"

    stripped = strip_regions(new)
    assert stripped == base, "delta is not limited to the marked regions"
    assert b"dm-bypass" not in stripped, "a marker survived the strip"
    print(
        f"OK  base {len(base)} B, new {len(new)} B, "
        f"{len(new) - len(base)} B inserted in {len(REGIONS)} regions"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
