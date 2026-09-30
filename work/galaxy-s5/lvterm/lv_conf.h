/*
 * lv_conf.h - LVGL configuration for lvterm, the second Galaxy S5 UI.
 *
 * Only what this program actually needs is defined. Everything else falls back
 * to the defaults in lv_conf_internal.h, which keeps the build small and, more
 * usefully, keeps the list of deliberate choices visible.
 *
 * The device facts this is built around, all measured rather than assumed:
 *
 *   /dev/fb0      1080x1920, 32bpp, XRGB8888, two stacked screen buffers that
 *                 the kernel pans between (smem_len is twice the visible size)
 *   /dev/input/event2
 *                 multitouch digitiser, input protocol B, X 0..1079,
 *                 Y 0..1919, one finger in practice
 */

#ifndef LV_CONF_H
#define LV_CONF_H

/* The panel is XRGB8888: one little-endian 0x00RRGGBB word per pixel. */
#define LV_COLOR_DEPTH 32

/* We are a static binary linking against musl, so plain libc malloc is both
 * available and cheaper than LVGL's own allocator. */
#define LV_USE_STDLIB_MALLOC   LV_STDLIB_CLIB
#define LV_USE_STDLIB_STRING   LV_STDLIB_CLIB
#define LV_USE_STDLIB_SPRINTF  LV_STDLIB_SPRINTF
#define LV_STDLIB_CLIB_INCLUDE <stdlib.h>
#define LV_STDLIB_CLIB_MALLOC  include <stdlib.h>
#define LV_STDLIB_CLIB_FREE    include <stdlib.h>
#define LV_STDLIB_CLIB_REALLOC include <stdlib.h>
#define LV_USE_OS              LV_OS_NONE

/* Logging goes to stdout, which is /var/log/lvterm.log when we start it. This
 * is how the display driver's own "mapped /dev/fb0" and touch capability
 * messages reach us.
 *
 * The level is WARN, not INFO, and that is a measured choice rather than a
 * preference. lv_indev.c logs every press and every release at INFO, which is
 * two lines per tap: a few minutes of ordinary tapping produced 5900 lines and
 * buried the two lines that actually say what the program is doing. Our own
 * diag() heartbeat is the signal now, and anything LVGL thinks is worth a
 * message is a warning or worse. */
#define LV_USE_LOG       1
#define LV_LOG_LEVEL     LV_LOG_LEVEL_WARN
#define LV_LOG_PRINTF    1

/* Widgets used here. The keyboard is the reason to use LVGL at all: it is a
 * maintained QWERTY widget with popups, modifiers and a candidate layer, and
 * writing one is most of what s5term.c had to do by hand.
 *
 * The textarea is not a luxury. lv_keyboard has exactly one way to deliver what
 * the user pressed: it calls lv_textarea_add_text() and friends on the object
 * passed to lv_keyboard_set_textarea(). There is no key callback and no
 * character stream to subscribe to, so if nothing is a textarea then every key
 * press is silently discarded. That is the whole input path, and it is the
 * reason the first build could show a keyboard and still ignore it. */
#define LV_USE_KEYBOARD  1
#define LV_USE_LABEL     1
#define LV_USE_BUTTON    1
#define LV_USE_BTN_MATRIX 1
#define LV_USE_TEXTAREA  1

/* Default font for the widgets, and the keyboard's key labels, which need
 * LV_SYMBOL_BACKSPACE and friends from above U+2000. The terminal text itself
 * does not use this: it uses the 16x32 VGA bitmap font already extracted from
 * the PSF2, because a terminal needs monospace and no bundled font is. */
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_28 1
#define LV_FONT_DEFAULT &lv_font_montserrat_28

/* Drivers: raw framebuffer and raw evdev. No X11, no Wayland, no DRM. */
#define LV_USE_EVDEV       1
#define LV_USE_LINUX_FBDEV 1

/* Partial mode with a strip of rows per flush. Full-screen mode would allocate
 * a second 8 MB buffer and redraw far more than changes; a strip keeps the
 * flush work proportional to what actually moved. */
#define LV_LINUX_FBDEV_RENDER_MODE LV_DISPLAY_RENDER_MODE_PARTIAL
#define LV_LINUX_FBDEV_BUFFER_SIZE 64
#define LV_LINUX_FBDEV_BUFFER_COUNT 1

/* We feed the tick from clock_gettime rather than a timer thread, so the
 * process stays a single thread and can be killed cleanly. */
#define LV_TICK_CUSTOM 1

#endif /* LV_CONF_H */
