#!/usr/bin/env python3
"""Verify a boot image by walking its own bytes, not the builder's intentions.

The read-back has already proved the phone's p9 equals the file on the host. This
then opens that image the way the bootloader does, from the header, and reports
what is actually inside it. Everything here is derived from the image bytes, so
it cannot be satisfied by the builder having formed an opinion about itself.

Three things are checked, and they are the three things r26 exists to do:

  - the four diagnostic steps are present in the ramdisk's init_2nd.sh, in order,
    and all of them before exec switch_root
  - that init_2nd.sh is the file on disk, byte for byte, not something resembling
    it, so the check above is about the code that will run
  - the device tree's /chosen/bootargs says console=ttyGS0, re-read from the
    flattened tree the way the kernel will read it
"""
import gzip
import hashlib
import pathlib
import re
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fdt as fdt_mod

image = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "boot-k3gxx-r26.img")
data = image.read_bytes()
print(f"  image {image.name}  {len(data)} B  sha256 {hashlib.sha256(data).hexdigest()}")

assert data[:8] == b"ANDROID!", "not an Android boot image"
(k_size, _, r_size, _, s_size, _, _, page, dt_size, _) = struct.unpack_from("<10I", data, 8)
print(f"  kernel {k_size} B, ramdisk {r_size} B, second {s_size} B, "
      f"device tree {dt_size} B, page {page}")

# Offsets derived from the header alone, the way the bootloader derives them.
k_off = page
r_off = k_off + (k_size + page - 1) // page * page
d_off = r_off + (r_size + page - 1) // page * page
assert d_off + ((dt_size + page - 1) // page * page) == len(data), (
    "the parts do not exactly fill the image; something else is in it"
)
print(f"  parts at {k_off} / {r_off} / {d_off}, and they fill the image exactly")

dt = data[d_off:d_off + dt_size]
assert dt[:4] == b"DTBH", "the device tree lost its wrapper"
tree = fdt_mod.Fdt(dt[2048:])
bootargs = tree.get("/chosen", "bootargs")
print(f"  /chosen/bootargs = {bytes(bootargs).rstrip(bytes(1))!r}")
assert bootargs.startswith(b"console=ttyGS0"), "the shipped console is not ttyGS0"
assert b"ttySAC2" not in bootargs, "the dead UART is still named"
print("  OK  the kernel command line points the console at ttyGS0")

# The ramdisk is a gzip stream. Decompress it rather than trusting the header.
raw = gzip.decompress(data[r_off:r_off + r_size])
print(f"  ramdisk decompresses to {len(raw)} B")

# A strict newc walk. The field offsets are the same ones the builder's own
# cpio_walk_strict uses, taken from the header bytes rather than from a struct
# unpacking: the header is 6 bytes of magic plus 13 eight-character hex fields,
# so filesize is bytes 54..62 and namesize is bytes 94..102. A second, looser
# parser is how the r20 brick passed a check once, so this refuses anything
# unusual instead of stepping over it: bad magic, an empty name, an entry that
# overruns the buffer, or an archive that never reaches its TRAILER.
def walk(buf):
    names, payloads, pos = [], {}, 0
    while pos + 110 <= len(buf):
        if buf[pos : pos + 6] != b"070701":
            raise ValueError(f"bad cpio magic at {pos}: {buf[pos:pos+6]!r}")
        h = buf[pos : pos + 110]
        namesize = int(h[94:102], 16)
        fsize = int(h[54:62], 16)
        if namesize == 0:
            raise ValueError(f"zero-length name at {pos}")
        name = buf[pos + 110 : pos + 110 + namesize - 1]
        data = pos + 110 + namesize
        data += (-data) % 4
        if data + fsize > len(buf):
            raise ValueError(f"entry {name!r} overruns the archive")
        if name == b"TRAILER!!!":
            return names, payloads
        names.append(name.decode())
        payloads[name.decode()] = buf[data : data + fsize]
        pos = data + fsize
        pos += (-pos) % 4
    raise ValueError("archive ended without a TRAILER record")

names, payloads = walk(raw)
print(f"  {len(names)} cpio entries, {len(set(names))} distinct names")
assert not any("s5screen" in n for n in names), "s5screen is in the image"
print("  OK  s5screen is not in the image")

for want in ("init_2nd.sh", "usr/bin/s5iskey", "usr/bin/s5usbkeep", "usr/bin/s5diag"):
    assert want in names, f"{want} is missing from the ramdisk"
print("  OK  init_2nd.sh, s5iskey, s5usbkeep and s5diag are all present")
assert names.count("init_2nd.sh") == 1, "init_2nd.sh is not unique"

blob = payloads["init_2nd.sh"]
print(f"  init_2nd.sh {len(blob)} B  sha256 {hashlib.sha256(blob).hexdigest()}")

# Compare against the source, so "the steps are in the image" and "the steps are
# in the file I reviewed" cannot come apart.
src = pathlib.Path("source/diagnostic-ramdisk-r26/init_2nd.sh").read_bytes()
assert blob == src, (
    f"the shipped init_2nd.sh is not the source: {len(blob)} B shipped, "
    f"{len(src)} B in source, first difference at "
    f"{next((i for i, (a, b) in enumerate(zip(blob, src)) if a != b), 'length')}"
)
print("  OK  the shipped init_2nd.sh is the reviewed source, byte for byte")

t = blob.decode()
switch = t.index("exec switch_root")
print(f"  exec switch_root is at byte {switch} of {len(blob)}")
order = []
for step in ("s5usb_open_shell", "s5usb_fix_getty", "s5usb_console_on", "s5usb_neutralise"):
    needle = "\n%s\n" % step
    assert t.count(needle) == 1, f"{step} is called 0 or more than once"
    at = t.index(needle)
    order.append((at, step))
    assert at < switch, f"{step} runs after switch_root and cannot work"
    print(f"  OK  {step} at byte {at}, before switch_root")
assert [s for _, s in sorted(order)] == [s for _, s in order], (
    "the steps are not in the order they are listed in"
)
print("  OK  the four steps run in order, all before switch_root")

# The claims r26 makes about itself, read out of the shipped bytes.
code = "\n".join(re.sub(r"^\s*#.*$", "", l) for l in t.splitlines())
checks = [
    ("the repaired getty line is what gets written",
     'print "ttyGS0::respawn:/sbin/getty -L 115200 ttyGS0 vt100"'
     in [l.strip() for l in code.splitlines()]),
    ("no service is named literally",
     "s5-usb-ncm" not in code and "s5-usb-ocn" not in code),
    ("runlevel links are tested with -L", '[ -L "$link" ]' in code),
    ("and never with -e", '[ -e "$link" ]' not in code),
    ("the CACHE dump is under the device mount",
     "out=/mnt/s5r25/codex-s5-diagnostics/r26" in [l.strip() for l in code.splitlines()]),
    ("no bind mount anywhere", "mount -o bind" not in code),
    ("the keeper logs to r26",
     "codex-s5-diagnostics/r26" in blob.decode()),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(f"  {'OK  ' if ok else 'FAIL'} {n}")
assert not bad, f"the shipped image does not do what it claims: {bad}"
print("\n  all checks passed on the image's own bytes")
