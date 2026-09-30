#!/usr/bin/env python3
"""
Apply the two LVGL changes that this device needs, to a copy of the LVGL tree.

Each edit is an exact string replacement that must match exactly once. If any
of them does not, the script fails loudly rather than leaving a half patched
tree, because a silently wrong patch here would show up as a touchscreen that
does nothing or a screen that flickers, and both of those are hard to trace
back to a build script.

Run as:  python3 apply-lvgl-patches.py <lvgl-dir>

The rationale for each edit is in the comment above it. The resulting diff can
be produced with:

    diff -ru pristine patched > lvterm/lvgl-patches.diff
"""

import sys
import pathlib

# --------------------------------------------------------------- edit one
#
# The evdev driver decides whether an evdev node is a pointer by asking for its
# capability bitmap and testing bits 0 and 1, which are ABS_X and ABS_Y:
#
#     uint32_t abs_bits = 0;
#     ioctl(dsc->fd, EVIOCGBIT(EV_ABS, sizeof(abs_bits)), &abs_bits)
#     if((abs_bits & ABS_XY_MASK) == ABS_XY_MASK) -> pointer
#
# EVIOCGBIT writes ceil(n/8) bytes, so a 32 bit request only ever learns about
# event codes 0..31. Every ABS_MT_* code is above 47, so the entire multitouch
# half of the interface is invisible to the test. (The key capability check a
# few lines below gets this right: uint32_t key_bits[KEY_MAX / 32 + 1]. The abs
# check is simply narrower than it needs to be.)
#
# On this phone that truncation is currently harmless, and it is worth saying so
# plainly rather than overstating the patch. Measured with absprobe, the
# digitiser publishes all of:
#
#     ABS_X 0, ABS_Y 1                       min 0 max 1079 / 0 max 1919
#     ABS_MT_SLOT 47, ABS_MT_TRACKING_ID 57
#     ABS_MT_POSITION_X 53, ABS_MT_POSITION_Y 54
#     ABS_MT_TOUCH_MAJOR 48, ABS_MT_TOUCH_MINOR 49
#
# ABS_X and ABS_Y are codes 0 and 1, inside the 32 bit window, so the upstream
# test happens to pass here even though the mask is too narrow. The same device
# emits only ABS_MT_POSITION_X/Y in its event stream; the legacy axes exist in
# the capability set and in the driver's calibration ioctls, which is why the
# ranges above are what LVGL uses, but they carry no events.
#
# So the mask is a real latent bug rather than the thing standing between us and
# a working touchscreen, and this patch is insurance, not a fix for an observed
# failure. It matters for a digitiser that publishes only the multitouch axes,
# where the unpatched driver would set indev_type to LV_INDEV_TYPE_NONE and
# discard the device. The rest of the driver already parses the slot protocol
# correctly; only the capability test is narrow.
#
# lvterm additionally passes LV_INDEV_TYPE_POINTER explicitly rather than
# LV_INDEV_TYPE_NONE, so the test is bypassed entirely and the device type is
# asserted from what was measured. The patch is kept because it makes the
# library correct for anyone who does rely on auto-detection.
#
# Fix: widen the mask, and accept either axis naming.

EVDEV_MASK_OLD = "#define ABS_XY_MASK ((1 << ABS_X) | (1 << ABS_Y))\n"

EVDEV_MASK_NEW = """#define ABS_XY_MASK ((1 << ABS_X) | (1 << ABS_Y))

/* A multitouch digitiser may publish ABS_MT_POSITION_X/Y instead of ABS_X/ABS_Y.
 * Those codes are all above 31, so a 32 bit capability mask cannot see them. */
#define ABS_MT_XY_MASK ((1ULL << ABS_MT_POSITION_X) | (1ULL << ABS_MT_POSITION_Y))
"""

EVDEV_TEST_OLD = """        uint32_t abs_bits = 0;
        if(ioctl(dsc->fd, EVIOCGBIT(EV_ABS, sizeof(abs_bits)), &abs_bits) >= 0) {
            /* if this device can emit absolute X and Y events, it shall be a pointer indev */
            if((abs_bits & ABS_XY_MASK) == ABS_XY_MASK) {"""

EVDEV_TEST_NEW = """        /* Wide enough to hold every EV_ABS code. 32 bits silently truncated
         * this and hid the multitouch axes. */
        uint64_t abs_bits = 0;
        if(ioctl(dsc->fd, EVIOCGBIT(EV_ABS, sizeof(abs_bits)), &abs_bits) >= 0) {
            /* if this device can emit absolute X and Y events, under either the
             * single touch or the multitouch naming, it shall be a pointer */
            if(((abs_bits & ABS_XY_MASK) == ABS_XY_MASK) ||
               ((abs_bits & ABS_MT_XY_MASK) == ABS_MT_XY_MASK)) {"""

# --------------------------------------------------------------- edit two
#
# /dev/fb0 on this panel is not one screen of memory. FBIOGET_FSCREENINFO
# reports smem_len = 16588800 bytes, exactly twice 1080 * 1920 * 4, because the
# driver keeps two stacked screen buffers and pans between them. The stock
# postmarketOS msm-fb-refresher service, which this device depends on, drives
# that pan on a loop.
#
# The upstream driver maps a single screen and writes each flushed row once, so
# whenever the pan moves, the viewer lands on a half that was never painted and
# the screen alternates between the current picture and stale content. Measured
# on the phone: 58 framebuffer changes in 20 s with nobody touching the screen.
#
# Fix: map all of smem_len, work out how many complete buffers it holds, and
# write every flushed row into all of them. The pan then becomes invisible and
# the refresher keeps running.

FBDEV_FIELD_OLD = """    size_t rotated_buf_size;
    long int screensize;
"""

FBDEV_FIELD_NEW = """    size_t rotated_buf_size;
    int stacked;                /* complete screen buffers held in smem_len */
    long int screensize;
"""

FBDEV_MAP_OLD = """    dsc->screensize =  dsc->finfo.smem_len;/*finfo.line_length * vinfo.yres;*/
"""

FBDEV_MAP_NEW = """    /* The panel memory may hold several stacked screen buffers that the kernel
     * pans between. Map all of them, and paint all of them, otherwise a pan
     * lands the viewer on a buffer we never drew into. */
    {
        struct fb_fix_screeninfo realfix;
        memset(&realfix, 0, sizeof(realfix));
        if(ioctl(dsc->fbfd, FBIOGET_FSCREENINFO, &realfix) == 0 &&
           realfix.line_length && realfix.smem_len > dsc->finfo.smem_len) {
            dsc->finfo.smem_len = realfix.smem_len;
        }
    }

    dsc->screensize =  dsc->finfo.smem_len;/*finfo.line_length * vinfo.yres;*/

    {
        size_t one = (size_t)dsc->finfo.line_length * dsc->vinfo.yres;
        dsc->stacked = one ? (int)(dsc->finfo.smem_len / one) : 1;
        if(dsc->stacked < 1) dsc->stacked = 1;
        if(dsc->stacked > 8) dsc->stacked = 8;
        LV_LOG_INFO("panel memory holds %d stacked screen buffers, painting"
                    " all of them", dsc->stacked);
    }
"""

# The row loop that copies the flush into panel memory. In DIRECT mode there is
# a near identical loop; only the PARTIAL branch is reached by our configuration
# (LV_LINUX_FBDEV_RENDER_MODE = PARTIAL), but both are patched so the file is
# correct either way.
FBDEV_FLUSH_OLD = """        for(int32_t y = clipped_area.y1; y <= clipped_area.y2; y++) {
            write_to_fb(dsc, fb_pos, color_p, w * px_size);
            fb_pos += dsc->finfo.line_length;
            color_p += stride;
        }"""

FBDEV_FLUSH_NEW = """        for(int32_t y = clipped_area.y1; y <= clipped_area.y2; y++) {
            write_to_fb(dsc, fb_pos, color_p, w * px_size);

            /* The same rows into every other stacked buffer, so whichever one
             * the kernel's pan settles on shows the same picture. */
            for(int b = 1; b < dsc->stacked; b++) {
                write_to_fb(dsc,
                            fb_pos + (size_t)b * dsc->finfo.line_length * dsc->vinfo.yres,
                            color_p, w * px_size);
            }

            fb_pos += dsc->finfo.line_length;
            color_p += stride;
        }"""

FBDEV_FLUSH_DIRECT_OLD = """        for(int32_t y = clipped_area.y1; y <= clipped_area.y2; y++) {
            write_to_fb(dsc, fb_pos, &color_p[color_pos], w * px_size);
            fb_pos += dsc->finfo.line_length;
            color_pos += disp->hor_res * px_size;
        }"""

FBDEV_FLUSH_DIRECT_NEW = """        for(int32_t y = clipped_area.y1; y <= clipped_area.y2; y++) {
            write_to_fb(dsc, fb_pos, &color_p[color_pos], w * px_size);

            for(int b = 1; b < dsc->stacked; b++) {
                write_to_fb(dsc,
                            fb_pos + (size_t)b * dsc->finfo.line_length * dsc->vinfo.yres,
                            &color_p[color_pos], w * px_size);
            }

            fb_pos += dsc->finfo.line_length;
            color_pos += disp->hor_res * px_size;
        }"""

# The display driver does not include <string.h>, so the FBIOGET_FSCREENINFO
# probe added below needs its own declaration.
FBDEV_INCLUDE_OLD = """#include <sys/mman.h>
#include <sys/ioctl.h>
"""

FBDEV_INCLUDE_NEW = """#include <sys/mman.h>
#include <sys/ioctl.h>
#include <string.h>
"""

EDITS = [
    ("src/drivers/display/fb/lv_linux_fbdev.c", FBDEV_INCLUDE_OLD, FBDEV_INCLUDE_NEW),
    ("src/drivers/evdev/lv_evdev.c", EVDEV_MASK_OLD, EVDEV_MASK_NEW),
    ("src/drivers/evdev/lv_evdev.c", EVDEV_TEST_OLD, EVDEV_TEST_NEW),
    ("src/drivers/display/fb/lv_linux_fbdev.c", FBDEV_FIELD_OLD, FBDEV_FIELD_NEW),
    ("src/drivers/display/fb/lv_linux_fbdev.c", FBDEV_MAP_OLD, FBDEV_MAP_NEW),
    ("src/drivers/display/fb/lv_linux_fbdev.c", FBDEV_FLUSH_OLD, FBDEV_FLUSH_NEW),
    ("src/drivers/display/fb/lv_linux_fbdev.c", FBDEV_FLUSH_DIRECT_OLD, FBDEV_FLUSH_DIRECT_NEW),
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    root = pathlib.Path(sys.argv[1])
    applied = 0

    for rel, old, new in EDITS:
        path = root / rel
        if not path.is_file():
            print(f"FAIL  {rel}: no such file")
            return 1

        text = path.read_text()
        n = text.count(old)
        if n != 1:
            print(f"FAIL  {rel}: pattern matched {n} times, expected exactly 1")
            print("      ---- pattern ----")
            for line in old.splitlines():
                print(f"      {line}")
            return 1

        path.write_text(text.replace(old, new))
        applied += 1
        print(f"ok    {rel}: {old.splitlines()[0].strip()[:60]}")

    print(f"\napplied {applied} edits to {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
