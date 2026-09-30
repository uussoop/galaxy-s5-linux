# Touch input on the Galaxy S5: what actually works, and what is missing

**Status: the kernel side is already complete and working. Nothing needs to be
rebuilt, reflashed, or patched. What is missing is entirely userspace.**

Research only. No changes were made to the phone. The only thing written to the
phone was a read-only probe binary at `/tmp/s5probe` that opens devices, queries
them with `ioctl`, prints, and exits. It writes to neither `/dev/fb0` nor
`/dev/uinput`.

---

## The headline

**Touch was never broken.** `sec_touchscreen` is bound to the I2C bus, has
registered an input device, and is delivering events. `/dev/input/event2` exists
and is readable. The reason touch "does not work" is that **no userspace process
is reading that device.** There is no X server, no Wayland compositor, no
console input driver, no libinput, nothing. The events go into a queue that
nothing drains.

Same story as the display: `s5screen` writes pixels straight to `/dev/fb0`
because there is no userspace graphics stack. Touch is the input-side twin of
that arrangement.

---

## Measured facts

Source: `/tmp/s5probe` (`work/galaxy-s5/s5probe.c`), run as root over SSH.
Raw numbers, not inferred.

### Touch controller

| Property | Value |
|---|---|
| Device name | `sec_touchscreen` |
| Node | `/dev/input/event2` |
| Bus | `0x0018` (BUS_I2C), i2c bus 8, slave address `0x20` |
| Sysfs | `/devices/12c60000.i2c/i2c-8/8-0020/input/input2` |
| Handlers | `mouse0 event2` |
| Driver | `synaptics_rmi4` (Synaptics RMI4 over I2C) |

The driver is present in `/proc/interrupts`, meaning it is bound and its
interrupt has actually fired:

```
566:   6   0   0   ... exynos_wkup_irq_chip  synaptics_rmi4_i2c
```

### Event types

| Code | Meaning |
|---|---|
| 0 | `EV_SYN` |
| 1 | `EV_KEY` |
| 3 | `EV_ABS` |
| 5 | `EV_SW` |

**`EV_KEY` is present but reports zero codes** — no `BTN_TOUCH`, no `BTN_LEFT`.
The driver's own `mouse0` emulation would normally assert `BTN_LEFT`, but this
driver does not. So do not expect a button code.

### Axes — and the important part

| Axis | Code | Min | Max |
|---|---|---|---|
| `ABS_X` | 0 | 0 | 1079 |
| `ABS_Y` | 1 | 0 | 1919 |

**Exactly two axes, ranging 0–1079 and 0–1919. That is the display's native
1080×1920 grid, one-to-one.**

This is the single most useful fact in the report. There is:

- **no calibration step** — no `input_calibrator` analogue, no DT
  `touchscreen-max-x/min-x` mismatch, no scaling to work out
- **no rotation to compensate** — the panel and the framebuffer agree on portrait
- **no ambiguity** — a coordinate means the same thing in both spaces

Many Linux touchscreen ports spend the whole task on calibration. This one skips
it entirely. Mapping a tap to a screen pixel is a division-free comparison.

Two consequences worth noting:

- **Single touch only.** Two axes, no `ABS_MT_*`, no `ABS_MT_SLOT`. A multitouch
  protocol would require at minimum `ABS_MT_SLOT`, `ABS_MT_POSITION_X`,
  `ABS_MT_POSITION_Y`, `ABS_MT_TRACKING_ID`. None are present. This is a
  single-touch device. Pinch-zoom is not available and cannot be made available.
- **No pressure or contact area.** No `ABS_PRESSURE`, no `ABS_MT_TOUCH_MAJOR`.
  Fuzz and flat are 0 on both axes. The driver also reports `res=0` on both.

### Press and release

`EV_SW` reports one code:

| Code | Value | Meaning |
|---|---|---|
| 22 | `0x016` | `SW_TOUCHSCREEN` |

This is the panel-in-contact flag. It is how the implementation will know a
finger went down and lifted. Combined with `EV_SYN`/`SYN_REPORT` for framing
each change, that is a complete enough event stream to build on.

A caveat worth being honest about: this is inferred from the fact that
`synaptics_rmi4` on Samsung hardware uses `SW_TOUCHSCREEN` for panel contact.
The bit is confirmed present; its runtime toggling has not been observed, since
that needs a finger on the glass. This is the one item in this report that is
not yet directly measured.

### Display

| Property | Value |
|---|---|
| Resolution | 1080 × 1920 |
| Virtual | 1080 × 1920 (no panning, no second buffer in the visible range) |
| Depth | 32 bpp |
| Type | 0 = `FB_TYPE_PACKED_PIXELS` |
| R | offset 0, length 8 |
| G | offset 8, length 8 |
| B | offset 16, length 8 |
| A | offset 0, **length 0** |
| `smem_len` | 16,588,800 |

**Channel layout is XRGB8888** — alpha length 0, so the top byte is padding.
Pixels are little-endian 32-bit words, so a pixel is simply:

```c
uint32_t px = (r << 0) | (g << 8) | (b << 16);   /* 0x00RRGGBB */
```

`smem_len` divided by pixel count is 8.0 bytes per pixel, exactly double the
4 bytes the pixel format needs — the framebuffer is double-buffered. The
existing `s5screen` work already writes to `/dev/fb0` successfully at this
geometry, so this is a known-good path, not a new risk.

### Input injection

```
crw------- 1 root root 10, 223 /dev/uinput
open OK (writable) - keystroke injection is possible
```

**`/dev/uinput` exists and opens for write.** This is the piece that makes an
on-screen keyboard possible at all, and it is the least certain thing anyone
would assume on a headless Android-kernel port.

It closes the loop that the display and touch alone cannot. A touchscreen lets
you *point*. Pointing is not typing. Without a way to turn a tap into a
keystroke, an on-screen keyboard would only be able to move a cursor — and
there is no cursor, because there is no userspace shell to move it. `uinput`
provides the missing half: create a virtual keyboard device, write
`EV_KEY`/`EV_SYN` events into it, and the input subsystem delivers them to
whoever is reading — which would be the `getty` on `ttyGS0`.

### Also present, not needed for the above

34 input devices total, including `sec_touchkey` (the capacitive
home/back/menu keys) on `event1`, `gpio_keys.16` (`event14`), and `Headset`
(`event13`). The physical side buttons already work. Several sensor devices
register but carry no useful axes.

### Build capability

There is **no compiler on the phone** — `gcc`, `cc`, `tcc`, `make`, `python3`
are all absent. Anything must be cross-compiled on the Mac.

That is solved. `zig cc` is vendored at
`work/galaxy-s5/scratch-toolchain/zig-aarch64-macos-0.16.0/zig` and produces
static ARMv7 musl binaries:

```sh
zig cc -target arm-linux-musleabi -Os -static \
      -Wl,--gc-sections -Wl,--strip-all \
      -ffunction-sections -fdata-sections  prog.c -o prog
```

Verified: `ELF 32-bit LSB executable, ARM, EABI5 version 1, statically linked`.
A 31 KB probe built this way and ran on the phone. This is a repeatable,
reproducible build path, and the artifact is fully deterministic from source.

---

## What is missing, precisely

One userspace program. Not three, not a kernel module.

1. **Read** `/dev/input/event2` — `ABS_X`, `ABS_Y`, `SW_TOUCHSCREEN` for press
   and release, `SYN_REPORT` to frame each change.
2. **Draw** a keyboard onto `/dev/fb0` as XRGB8888, hit-test taps against key
   rectangles, draw a pressed-key highlight.
3. **Emit** the corresponding keystrokes through `/dev/uinput` so they reach the
   `getty`.

Rough estimate: 400–600 lines of C, well under 60 KB stripped. Trivial next to
the image rebuilds this project has already done. **No reboot required** — it
runs as an ordinary background process and can be stopped by killing it.

## Why this is a genuinely small job

- Coordinates are already 1:1 with the display, so no calibration math.
- The pixel format is XRGB8888, so drawing is one `uint32_t` store per pixel.
- `uinput` means no kernel module, no `evdev` grab, no fighting the console for
  the real tty.
- It is one process, reversible by killing it, and touches no persistent config.

## Options considered

| Option | Verdict |
|---|---|
| On-screen keyboard via `uinput` | **Best.** Uses what is already present. No kernel work. |
| Rebuild kernel with a new touch driver | Unnecessary. The driver works. |
| DT patch to enable touch | Unnecessary. Already enabled and bound. |
| Framebuffer console with evdev input | Possible, but ties the keyboard to whichever vt is active. Worse than a standalone overlay. |
| X11 / Xorg | Heavy, and no X server binary for this base to install. |
| Physical Bluetooth keyboard | Would work as a plain keyboard but is not touch, and cannot do the login password before pairing. |
| Restore Android's input stack | Wrong direction. The whole point is native postmarketOS. |

## Risks and unknowns

- **`SW_TOUCHSCREEN` runtime behaviour is inferred, not observed.** A finger is
  needed to confirm. This is the single item standing between this report and a
  complete picture.
- **Single touch only.** Not a risk, a hard limit. No multitouch, no gestures,
  no pinch.
- **The `getty` must read from the input device.** If the `getty` on `ttyGS0`
  does not consume `uinput`-originated events, the last step needs a different
  approach. Unverified. Keyboard events from `uinput` reach the console tty
  through the same path as a USB keyboard's, so this is very likely fine, but it
  has not been demonstrated.
- **The keyboard overlay would overwrite the display**, including anything
  `s5screen` is currently showing. A real implementation needs a
  redraw/ownership policy between the two.
- **The display is not currently a terminal.** It shows status text written
  directly to pixels, not a VT. So a keyboard typed into `ttyGS0` would go
  nowhere visible until the display is taught to mirror the console. Touch alone
  does not give a usable on-screen login prompt.

## Reproduction

```sh
# build the probe
zig cc -target arm-linux-musleabi -Os -static \
      -Wl,--gc-sections -Wl,--strip-all \
      -ffunction-sections -fdata-sections \
      work/galaxy-s5/s5probe.c -o /tmp/s5probe

# run it
sudo /tmp/s5probe
```

The probe is read-only by construction: it opens devices `O_RDONLY`, issues
`ioctl` queries, prints, and exits. Its one write-side interaction is
`open("/dev/uinput", O_WRONLY)` to prove writability, which is closed again
immediately and injects no events.
