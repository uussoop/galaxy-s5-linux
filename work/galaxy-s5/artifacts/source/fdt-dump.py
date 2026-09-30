#!/usr/bin/env python3
"""Read a flattened device tree and print one subtree, with property values.

Written because neither dtc nor fdtdump is available on this host, and because
guessing at the gpio_keys wiring from the raw strings block is not good enough
to decide whether a phantom key press is a DT problem or a driver problem.

Usage: fdt-dump.py <dt.img> <node-path-substring> [--props a,b,c]
"""

import struct
import sys

FDT_MAGIC = 0xD00DFEED
BEGIN_NODE, END_NODE, PROP, NOP, END = 1, 2, 3, 4, 9


def parse(blob):
    # The header is 10 u32s. Getting this count wrong shifts every field after
    # the missing one, which shows up much later as a nonsensical string offset.
    (magic, totalsize, off_struct, off_strings, off_rsvmap, version,
     last_comp, cpuid, size_strings, size_struct) = struct.unpack_from(">10I", blob, 0)
    assert magic == FDT_MAGIC, f"not a flattened device tree: {magic:#x}"
    strings = blob[off_strings:off_strings + size_strings]
    pos = off_struct
    end = off_struct + size_struct
    path, tree = [], []
    while pos < end:
        (tok,) = struct.unpack_from(">I", blob, pos)
        pos += 4
        if tok == NOP:
            continue
        if tok == END:
            break
        if tok == BEGIN_NODE:
            nul = blob.index(b"\0", pos)
            name = blob[pos:nul].decode()
            pos = (nul + 4) & ~3
            path.append(name)
            tree.append({"path": "/" + "/".join(p for p in path if p), "props": {}})
            continue
        if tok == END_NODE:
            path.pop()
            continue
        if tok == PROP:
            plen, noff = struct.unpack_from(">II", blob, pos)
            pos += 8
            val = blob[pos:pos + plen]
            pos = (pos + plen + 3) & ~3
            e = strings.index(b"\0", noff)
            key = strings[noff:e].decode()
            tree[-1]["props"][key] = val
            continue
        raise SystemExit(f"bad token {tok} at {pos - 4}")
    return tree


def show_value(key, val):
    if not val:
        return "<empty>"
    if key in ("reg", "ranges") and len(val) % 4 == 0:
        cells = struct.unpack(f">{len(val) // 4}I", val)
        return " ".join(f"0x{c:x}" for c in cells)
    if len(val) % 4 == 0 and len(val) <= 32 and not all(32 <= b < 127 for b in val):
        cells = struct.unpack(f">{len(val) // 4}I", val)
        return " ".join(str(c) for c in cells)
    if all(32 <= b < 127 or b == 0 for b in val):
        return repr(val.rstrip(b"\0").decode())
    return val.hex()


def main():
    blob = open(sys.argv[1], "rb").read()
    needle = sys.argv[2]
    only = None
    if "--props" in sys.argv:
        only = set(sys.argv[sys.argv.index("--props") + 1].split(","))
    tree = parse(blob)
    for node in tree:
        if needle not in node["path"]:
            continue
        print(node["path"] or "/")
        for key, val in node["props"].items():
            if only and key not in only:
                continue
            print(f"    {key} = {show_value(key, val)}")
        print()


if __name__ == "__main__":
    main()
