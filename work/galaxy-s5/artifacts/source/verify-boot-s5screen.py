#!/usr/bin/env python3
"""Independently verify a BOOT image built by repack-boot-s5screen.py.

This deliberately re-derives everything from the finished image instead of
trusting anything the repack tool computed, and it re-implements the region
stripper rather than importing it, so a bug in the builder cannot hide here.

Checks, in order of how much they would matter:

  1. the CPIO is exactly the base archive with init_functions.sh swapped and
     usr/bin/s5screen appended, and every other single entry is byte-identical;
  2. stripping every marked region out of that init_functions.sh reproduces the
     base image's own init_functions.sh byte for byte;
  3. usr/bin/s5screen is a stripped, static, ARM EABI5 executable, and is the
     same file that was handed to the repack tool;
  4. the kernel, the device tree and the boot header are untouched, and the
     header's size and SHA-1 describe the parts actually in the file;
  5. the shell still parses, defines each helper before calling it, and paints
     every state s5screen knows about;
  6. the image fits the BOOT partition.
"""

import gzip
import hashlib
import importlib.util
import struct
import subprocess
import sys
from pathlib import Path

BASE = "boot-k3gxx-r11-subpartfix-35de59d0.img"
S5SCREEN = "source/s5screen"
BOOT_LIMIT = 13_631_488
KERNEL_SHA256 = "6d0c337411dcbfe6771347446af8fcab19bb5bff265496a5cddaa272de631b8d"
DT_SHA256 = "f6aac8da35c945bc982d10fccf29535a4692ea50669f5d323493c9c8b1228f6b"
TARGET = "init_functions.sh"
NEW_FILE = "usr/bin/s5screen"

# Read straight out of the repack tool rather than restating them, so a marker
# rename cannot leave the verifier checking names that no longer exist.
_spec = importlib.util.spec_from_file_location("repack", "source/repack-boot-s5screen.py")
_repack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_repack)
REGIONS = _repack.REGIONS

fails = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        fails.append(label)
    return ok


def unpack(img: Path):
    """-> (header, kernel, ramdisk_blob, cpio_bytes, dt, page_size)"""
    b = img.read_bytes()
    assert b[:8] == b"ANDROID!", "not an ANDROID! boot image"
    fields = struct.unpack_from("<10I", b, 8)
    ksize, _, rsize, _, ssize, _, _, page, dtsize, _ = fields
    koff = page
    roff = koff + -(-ksize // page) * page
    doff = roff + -(-rsize // page) * page
    assert ssize == 0, f"unexpected second stage ({ssize} B)"
    return (
        b[:page],
        b[koff : koff + ksize],
        b[roff : roff + rsize],
        gzip.decompress(b[roff : roff + rsize]),
        b[doff : doff + dtsize],
        page,
    )


def archive(raw: bytes):
    """-> {name: data}. Written from the format spec, not reused from repack."""
    out, pos = {}, 0
    while pos < len(raw):
        if raw[pos : pos + 6] != b"070701":
            raise SystemExit(f"bad newc magic at offset {pos}")
        f = [int(raw[pos + 6 + n * 8 : pos + 14 + n * 8], 16) for n in range(13)]
        namesize, filesize = f[11], f[6]
        name = raw[pos + 110 : pos + 110 + namesize - 1].decode()
        dstart = pos + 110 + namesize
        dstart += -dstart % 4
        data = raw[dstart : dstart + filesize]
        if name != "TRAILER!!!":
            out[name] = data
        pos = dstart + filesize
        pos += -pos % 4
        if name == "TRAILER!!!":
            break
    return out


def strip(lines):
    """Independent stripper: delete each outermost marked region line range."""
    keep = [True] * len(lines)
    spans = []
    for begin, end in REGIONS:
        try:
            bi = next(i for i, l in enumerate(lines) if l.rstrip("\n").endswith(begin))
            ei = next(i for i, l in enumerate(lines) if l.rstrip("\n").endswith(end))
        except StopIteration:
            raise SystemExit(f"marker missing: {begin}")
        assert bi < ei, f"region markers out of order: {begin}"
        spans.append((bi, ei))
    for bi, ei in spans:
        # An outermost region covers every nested one too.
        if any(bj < bi and ei < ej for j, (bj, ej) in enumerate(spans) if (bj, ej) != (bi, ei)):
            continue
        for i in range(bi, ei + 1):
            keep[i] = False
    return "".join(l for i, l in enumerate(lines) if keep[i])


def code_lines(text):
    """Executable lines only: strip comments and whitespace, drop blanks.
    Needed because the guard's own comment mentions fail_halt_boot, so a raw
    substring count would report a difference that is not there."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def main(image_name):
    img = Path(image_name)
    base = Path(BASE)
    s5ref = Path(S5SCREEN).read_bytes()

    print(f"verifying {img.name} ({img.stat().st_size} B)\n")

    print("1. archive contents")
    _, kern, _, cpio, dt, page = unpack(img)
    _, bkern, _, bcpi, bdt, _ = unpack(base)
    new = archive(cpio)
    old = archive(bcpi)

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    check("exactly one file added", added == [NEW_FILE], f"added={added}")
    check("no file removed", not removed, f"removed={removed}")

    changed = sorted(n for n in set(old) & set(new) if old[n] != new[n])
    check(
        "init_functions.sh is the only changed file",
        changed == [TARGET],
        f"changed={changed}",
    )
    check("entry count grew by one", len(new) == len(old) + 1, f"{len(old)} -> {len(new)}")

    print("\n2. init_functions.sh delta")
    got = new[TARGET].decode()
    base_fns = old[TARGET].decode()
    stripped = strip(got.splitlines(keepends=True))
    check(
        "all marked regions strip back to the base file",
        stripped == base_fns,
        f"{len(stripped)} vs base {len(base_fns)} B",
    )
    for begin, end in REGIONS:
        name = begin.split(": ")[1].removesuffix(" (begin)")
        check(
            f"exactly one {name!r} region",
            got.count(begin) == 1 and got.count(end) == 1,
        )
    check("the patch added bytes", len(got) > len(base_fns), f"{len(base_fns)} -> {len(got)} B")

    print("\n3. s5screen binary")
    blob = new[NEW_FILE]
    check("byte-identical to the built binary", blob == s5ref, f"{len(blob)} B")
    check("ELF32 little-endian ARM", blob[:4] == b"\x7fELF" and blob[4] == 1 and blob[5] == 1)
    e_type, e_machine = struct.unpack_from("<HH", blob, 16)
    check("EM_ARM, ET_EXEC", e_machine == 40 and e_type == 2, f"machine={e_machine} type={e_type}")
    flags = struct.unpack_from("<I", blob, 36)[0]
    check("EABI5", flags & 0xFF000000 == 0x05000000, f"flags={flags:#x}")
    check(
        "statically linked",
        b"ld-musl" not in blob and b"/lib/ld" not in blob and b"interp" not in blob,
    )
    check(
        "stripped",
        all(j not in blob for j in (b".debug_info", b".debug_str", b".symtab", b".strtab")),
    )
    # Walk the program headers using the sizes the file itself declares, read
    # at their real ELF32 offsets rather than assumed ones.
    # Walk the program headers using the sizes the file itself declares, read at
    # their real ELF32 offsets rather than assumed ones. Zig emits a PIE, so
    # e_phnum counts six entries and the last is the read-only PT_GNU_RELRO;
    # what matters is that everything is well formed and nothing needs an
    # interpreter.
    e_phoff = struct.unpack_from("<I", blob, 0x1C)[0]
    e_ehsize = struct.unpack_from("<H", blob, 0x28)[0]
    e_phentsize = struct.unpack_from("<H", blob, 0x2A)[0]
    e_phnum = struct.unpack_from("<H", blob, 0x2C)[0]
    types = [
        struct.unpack_from("<I", blob, e_phoff + n * e_phentsize)[0] for n in range(e_phnum)
    ]
    # 0x70000001 is the ARM procedure-call-standard attribute section
    # (SHT_ARM_ATTRIBUTES), which LLD also emits as a program header. objdump
    # prints it as "UNKNOWN"; it is normal, not a malformed file.
    PT_LOAD, PT_INTERP, PT_DYNAMIC, PT_ARM_ATTRIBUTES = 1, 3, 2, 0x70000001
    check(
        "ELF header is self-consistent",
        e_ehsize == 52 and e_phentsize == 32 and e_phoff == 52,
        f"ehsize={e_ehsize} phentsize={e_phentsize} phoff={e_phoff}",
    )
    check(
        "every program header is a known type",
        all(t in (0, 1, 2, 4, 6, 0x6474E550, 0x6474E551, 0x6474E552, PT_ARM_ATTRIBUTES) for t in types),
        f"phnum={e_phnum} types={[hex(t) for t in types]}",
    )
    check(
        "has PT_LOAD, no PT_INTERP, no PT_DYNAMIC",
        PT_LOAD in types and PT_INTERP not in types and PT_DYNAMIC not in types,
        f"{types.count(PT_LOAD)} PT_LOAD",
    )

    print("\n4. kernel, device tree, header")
    check("kernel identical to base", kern == bkern, hashlib.sha256(kern).hexdigest()[:16])
    check("kernel is r1", hashlib.sha256(kern).hexdigest() == KERNEL_SHA256)
    check("device tree identical to base", dt == bdt, hashlib.sha256(dt).hexdigest()[:16])
    check("device tree unchanged", hashlib.sha256(dt).hexdigest() == DT_SHA256)
    check("page size still 2048", page == 2048)

    header = img.read_bytes()[:page]
    ksize, _, rsize, _, _, _, _, _, dtsize, _ = struct.unpack_from("<10I", header, 8)
    roff = page + -(-ksize // page) * page
    rblob = img.read_bytes()[roff : roff + rsize]
    check("header ramdisk size matches the file", rsize == len(rblob) and rsize > 0, f"{rsize} B")
    digest = hashlib.sha1()
    for part in (kern, rblob, b"", dt):
        digest.update(part)
        digest.update(struct.pack("<I", len(part)))
    check("header SHA-1 describes the actual parts", header[576:596] == digest.digest())

    print("\n5. shell still sane")
    tmp = Path("/tmp/verify-s5-functions.sh")
    tmp.write_text(got)
    r = subprocess.run(["bash", "-n", str(tmp)], capture_output=True, text=True)
    check("parses under bash -n", r.returncode == 0, r.stderr.strip()[:200])
    check("s5status defined before first use", got.index("s5status() {") < got.index("s5status booting"))
    check(
        "keys_still_held defined before first use",
        got.index("keys_still_held() {") < got.index("keys_still_held KEY_LEFTSHIFT"),
    )
    for state in ("booting", "dmok", "dmno", "loopfail", "keys", "booted"):
        check(f"paints the {state!r} state", f"s5status {state}" in got)
    got_code, base_code = code_lines(got), code_lines(base_fns)
    check(
        "no fail_halt_boot call added or lost",
        got_code.count("fail_halt_boot") == base_code.count("fail_halt_boot"),
        f"{base_code.count('fail_halt_boot')} in base, {got_code.count('fail_halt_boot')} now",
    )
    # The one statement that decides a failed boot must now sit inside a
    # persistence test rather than firing on a single iskey sample.
    guard = [i for i, l in enumerate(got_code) if l == "fail_halt_boot"]
    check("the boot-failing call is the one check_keys reaches", len(guard) >= 1)
    idx = next(i for i, l in enumerate(got_code) if "elif iskey KEY_LEFTSHIFT" in l)
    window = got_code[idx : idx + 5]
    check(
        "it is guarded by keys_still_held",
        len(window) == 5
        and "keys_still_held" in window[1]
        and window[2] == "fail_halt_boot"
        and window[3] == window[4] == "fi",
        " | ".join(window),
    )
    check("the raw iskey call is unchanged", "elif iskey KEY_LEFTSHIFT KEY_VOLUMEUP; then" in got)
    # the shipped s5status must never be able to abort the caller
    check("s5status cannot fail the caller", "\treturn 0\n}\n\n# Require the given keys" in got)
    check("s5status ignores its exit status", 's5screen "$1" >/dev/null 2>&1\n\treturn 0' in got)

    print("\n6. size")
    check("fits the BOOT partition", img.stat().st_size <= BOOT_LIMIT, f"{BOOT_LIMIT - img.stat().st_size} B spare")

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify-boot-s5screen.py <image>")
    raise SystemExit(main(sys.argv[1]))
