#!/usr/bin/env python3
"""Minimal flat-device-tree walker, enough to find one string property.

The boot image's header cmdline is inert on this phone: the bootloader passes
only the device tree's /chosen/bootargs, which the r14 kernel log showed as an
850-byte line. So the only way to add a console= argument is to edit that string
inside the flattened tree.

Editing an FDT property in general means rebuilding the blob, because every
offset after the changed value moves. Editing a string property that only ever
grows into its own trailing NUL padding does not, which is why this exists: it
finds the property, refuses unless the new value fits in the space already
reserved, and then proves afterwards that the total size and every other byte
are untouched.

Token layout is the standard one: FDT_BEGIN_NODE 1, FDT_END_NODE 2, FDT_PROP 3,
FDT_NOP 4, FDT_END 9. A PROP is followed by a u32 length, a u32 nameoff, then
the value padded to a 4-byte boundary. Node names are NUL-terminated and padded
to 4 bytes as well.
"""
import struct

FDT_MAGIC = 0xD00DFEED
BEGIN_NODE, END_NODE, PROP, NOP, END = 1, 2, 3, 4, 9


class Fdt:
    def __init__(self, blob):
        # A copy, always, so that a caller can diff the original against the
        # edited tree byte for byte. The consequence is that an edit has to be
        # taken back off the object: tree = Fdt(tree).set_string_in_place(...)
        # would otherwise write to a temporary and quietly change nothing.
        self.blob = bytearray(blob)
        (self.magic, self.totalsize, self.off_struct, self.off_strings,
         self.off_rsvmap, self.version, self.last_comp, self.boot_cpuid,
         self.size_strings, self.size_struct) = struct.unpack_from(">10I", blob, 0)
        assert self.magic == FDT_MAGIC, "not a flattened device tree"
        assert self.totalsize <= len(blob), "FDT claims to be larger than the blob"

    def string(self, off):
        end = self.blob.index(b"\0", self.off_strings + off)
        return self.blob[self.off_strings + off:end].decode()

    def walk(self):
        """Yield (path, prop_name, value_abs_offset, value_len) for every property."""
        p = self.off_struct
        end = self.off_struct + self.size_struct
        stack = []
        while p < end:
            (tok,) = struct.unpack_from(">I", self.blob, p)
            p += 4
            if tok == NOP:
                continue
            if tok == END:
                return
            if tok == BEGIN_NODE:
                stop = self.blob.index(b"\0", p)
                stack.append(self.blob[p:stop].decode())
                p += (stop - p + 4) & ~3
                continue
            if tok == END_NODE:
                if not stack:
                    raise ValueError("END_NODE with no open node")
                stack.pop()
                continue
            if tok == PROP:
                length, nameoff = struct.unpack_from(">II", self.blob, p)
                p += 8
                path = "/" + "/".join(s for s in stack if s)
                yield path, self.string(nameoff), p, length
                p += (length + 3) & ~3
                continue
            raise ValueError("unknown FDT token %d at %d" % (tok, p - 4))

    def find(self, path, name):
        hits = [(o, l) for p, n, o, l in self.walk() if p == path and n == name]
        assert len(hits) == 1, "expected one %s/%s, found %d" % (path, name, len(hits))
        return hits[0]

    def get(self, path, name):
        off, length = self.find(path, name)
        return self.blob[off:off + length]

    def set_string_in_place(self, path, name, value):
        """Overwrite a string property, padding with NULs. Never grows the blob.

        Returns the number of bytes actually written. Refuses rather than
        silently truncating, because a truncated kernel command line is a boot
        that comes up with the wrong arguments and no way to say so.
        """
        if isinstance(value, str):
            value = value.encode()
        off, length = self.find(path, name)
        old = self.blob[off:off + length]
        if len(value) + 1 > length:
            raise ValueError(
                "%s/%s has %d bytes reserved, %d needed; the blob would have to "
                "move and that is not done here" % (path, name, length, len(value) + 1)
            )
        if b"\0" in value:
            raise ValueError("value contains a NUL")
        self.blob[off:off + len(value)] = value
        self.blob[off + len(value):off + length] = b"\0" * (length - len(value))
        return len(value)


if __name__ == "__main__":
    import hashlib
    import pathlib
    import sys

    base = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else
                       "boot-k3gxx-r11-subpartfix-35de59d0.img").read_bytes()
    # Same field order the repack builder uses, and the same offsets.
    (kernel_size, _, ramdisk_size, _, second_size, _, _, page_size,
     dt_size, _) = struct.unpack_from("<10I", base, 8)
    assert base[:8] == b"ANDROID!" and second_size == 0
    kernel_off = page_size
    ramdisk_off = kernel_off + (kernel_size + page_size - 1) // page_size * page_size
    dt_off = ramdisk_off + (ramdisk_size + page_size - 1) // page_size * page_size
    dt = bytearray(base[dt_off:dt_off + dt_size])
    # Samsung wraps the tree in a 2 KB "DTBH" blob header, and DT_SIZE counts
    # that wrapper. The flattened tree itself starts after it.
    assert dt[:4] == b"DTBH", "expected Samsung's DTBH wrapper, found %r" % bytes(dt[:4])
    tree = bytearray(dt[2048:])
    f = Fdt(tree)
    print("  dt at %d, %d B (2 KB DTBH wrapper, %d B of tree)"
          % (dt_off, dt_size, f.totalsize))
    print("  FDT %d B, version %d, struct %d B, strings %d B"
          % (f.totalsize, f.version, f.size_struct, f.size_strings))
    ba = f.get("/chosen", "bootargs")
    print("  /chosen/bootargs = %r" % ba)
    off_, len_ = f.find("/chosen", "bootargs")
    print("  reserved %d B, string is %d B, slack %d B"
          % (len_, ba.index(b"\0") + 1, len_ - (ba.index(b"\0") + 1)))
    print("  sha256 %s" % hashlib.sha256(bytes(dt)).hexdigest())
    for p, n, o, l in f.walk():
        if p == "/chosen":
            print("    /chosen/%-12s %5d B" % (n, l))
