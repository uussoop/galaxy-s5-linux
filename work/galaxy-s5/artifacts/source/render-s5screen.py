#!/usr/bin/env python3
"""Render the on-screen boot status strips for the native initramfs.

This kernel has CONFIG_VT_CONSOLE=y but # CONFIG_FRAMEBUFFER_CONSOLE is not
set, and there is no plymouth in stage 1, so no text can ever reach the panel
through a console. The panel is a 1080x1920 s6e3fa2_fhd DSI display driven by
the Exynos FIMD fbdev at /dev/fb0, and writing a strip of pixels there puts it
at a known offset in the linear framebuffer, so a pre-rendered strip needs no
placement code on the device.

Two bits per pixel are produced, 24 and 32, because the DT only says
samsung,default_bpp = 24 while the FIMD driver may present 32. The device-side
helper picks the file that matches /sys/class/graphics/fb0/bits_per_pixel.

Every screen is a solid, high-contrast block of colour with large text, so the
strips stay highly compressible inside the gzip ramdisk.
"""

import gzip
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1080
HEIGHT = 720
BPP = (24, 32)
BG = (0, 0, 0)

BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
MONO = "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"

HEADER = "postmarketOS   NATIVE BOOT"
FOOTER = "SM-G900H k3gxx  initramfs 3.12.3-r3  BOOT d0542337"

# state -> (bar colour, status line, detail lines)
SCREENS = {
    "booting": (
        (32, 78, 140),
        "STARTING INITRAMFS",
        [
            "stage 1  mdev, modules, framebuffer",
            "usb gadget up, unudhcpd 172.16.42.1",
            "reading nested MBR of USERDATA p21",
        ],
    ),
    "dmok": (
        (24, 132, 64),
        "DEVICE-MAPPER OK",
        [
            "boot  /dev/mapper/mmcblk0p21p1",
            "ext2  497664 sectors @ 2048",
            "root  /dev/mapper/mmcblk0p21p2",
            "ext4  24346624 sectors @ 499712",
            "mounting /boot, then switch_root",
        ],
    ),
    "dmno": (
        (168, 108, 8),
        "GUARD REJECTED, LOOP FALLBACK",
        [
            "no mapping was used, trying losetup",
            "the reason is in the CACHE log",
        ],
    ),
    "loopfail": (
        (150, 24, 24),
        "SUBPARTITION MOUNT FAILED",
        [
            "loop0 cannot read LBA 0 of p21",
            "debug shell: telnet 172.16.42.1 23",
            "logs: /cache/codex-s5-diagnostics",
        ],
    ),
    "booted": (
        (16, 110, 130),
        "BOOTED TO USER ROOT",
        [
            "switch_root done, OpenRC running",
            "ssh on 172.16.42.1",
        ],
    ),
}

MARGIN = 32
HEADER_H = 100
FOOTER_H = 52
DETAIL_SIZE = 38
DETAIL_STEP = 56


def hard_mask(size, text, font_path, xy):
    """Return a 0/255 mask of the text with anti-aliasing removed.

    Glyph edges are thresholded to fully on or fully off, so the whole strip
    uses a handful of distinct byte values. Smoothly shaded edges are what
    make a mostly-empty screen expensive to store in the gzip ramdisk, and the
    panel is far too small on a phone for the difference to be visible.
    """
    font = ImageFont.truetype(font_path, size)
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255)
    mask = mask.point(lambda value: 255 if value >= 128 else 0)
    box = mask.getbbox()
    assert box is not None, f"nothing rendered for {text!r}"
    left, top, right, bottom = box
    # The right edge is the one that would clip, so it is checked exactly; a
    # glyph's left side bearing may sit a pixel or two before the pen position.
    assert left >= MARGIN - 4 and right <= WIDTH - MARGIN, (
        f"{text!r} spans x {left}..{right}, outside the {MARGIN}px margin"
    )
    assert top >= 0 and bottom <= HEIGHT, f"{text!r} is clipped vertically"
    return mask


def render(state) -> Image.Image:
    bar, status, details = SCREENS[state]
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)

    # Header bar with the title knocked out of it in black.
    image.paste(Image.new("RGB", (WIDTH, HEADER_H), bar), (0, 0))
    image.paste(
        Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0)),
        (0, 0),
        hard_mask(48, HEADER, BOLD, (MARGIN, 24)),
    )

    # Status line, shrunk until it fits inside the margins.
    size = 80
    while size > 24:
        font = ImageFont.truetype(BOLD, size)
        if ImageDraw.Draw(Image.new("L", (WIDTH, HEIGHT))).textlength(status, font=font) <= WIDTH - 2 * MARGIN:
            break
        size -= 2
    image.paste(
        Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255)),
        (0, 0),
        hard_mask(size, status, BOLD, (MARGIN, 132)),
    )

    y = 268
    for line in details:
        image.paste(
            Image.new("RGB", (WIDTH, HEIGHT), (198, 198, 198)),
            (0, 0),
            hard_mask(DETAIL_SIZE, line, MONO, (MARGIN, y)),
        )
        y += DETAIL_STEP
    assert y - DETAIL_STEP + DETAIL_SIZE <= HEIGHT - FOOTER_H - 16, "details run into the footer"

    # Footer bar, same treatment as the header.
    image.paste(Image.new("RGB", (WIDTH, FOOTER_H), bar), (0, HEIGHT - FOOTER_H))
    image.paste(
        Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0)),
        (0, 0),
        hard_mask(30, FOOTER, MONO, (MARGIN, HEIGHT - 40)),
    )
    return image


def to_bpp_bytes(image: Image.Image, bpp: int) -> bytes:
    raw = image.tobytes()
    if bpp == 24:
        assert len(raw) == WIDTH * HEIGHT * 3
        return raw
    out = bytearray(WIDTH * HEIGHT * 4)
    out[0::4] = raw[0::3]  # B
    out[1::4] = raw[1::3]  # G
    out[2::4] = raw[2::3]  # R
    assert len(out) == WIDTH * HEIGHT * 4
    return bytes(out)


def main(destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    for state in SCREENS:
        image = render(state)
        for bpp in BPP:
            payload = to_bpp_bytes(image, bpp)
            path = destination / f"{state}.{bpp}.raw"
            path.write_bytes(payload)
            compressed = len(gzip.compress(payload, 9))
            total += compressed
            print(f"{path.name:>16}  raw {len(payload):>9}  gz {compressed:>7}")
    print(f"{'':>16}  total gz {total}  (BOOT headroom is the limit)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: render-s5screen.py destination-dir")
    sys.exit(main(Path(sys.argv[1])))
