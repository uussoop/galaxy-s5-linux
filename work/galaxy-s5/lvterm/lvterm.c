/*
 * lvterm - the second Galaxy S5 terminal UI, built on LVGL instead of on
 *          hand written framebuffer and input code.
 *
 * Why this exists alongside s5term
 * -------------------------------
 * s5term draws its own pixels and parses its own events. It works, and it is
 * about 1200 lines of code that had to get every one of those details right.
 * This is the same product built on a library instead, to see what the library
 * route actually costs on this device.
 *
 * What LVGL does for us
 * ---------------------
 *   - the framebuffer display driver, including 32bpp XRGB8888 handling
 *   - the evdev input driver, including the multitouch slot protocol
 *   - lv_keyboard: a maintained QWERTY widget with popups and modifier keys,
 *     which is the single largest thing s5term had to hand roll
 *   - layout, event dispatch, redraw scheduling, the lot
 *
 * What it does not do, and what we still own
 * -------------------------------------------
 * LVGL has no terminal emulator widget, so the pty, the line discipline and the
 * screen buffer are still ours. That is the part that is genuinely ours to get
 * right, and it is much smaller than s5term's version because LVGL is laying
 * the text out rather than us blitting glyphs.
 *
 * The font is ours too: no bundled LVGL font is monospace, and a terminal
 * needs monospace. The 16x32 VGA bitmap from the PSF2 that s5term already uses
 * is exposed here as a custom lv_font_t. Its glyph layout is 32 rows of 2
 * bytes, most significant bit leftmost, 1 bit per pixel, which is exactly what
 * LVGL's bitmap font interface expects, so the existing extracted data is
 * reused as-is with no conversion.
 *
 * Two LVGL patches are recorded in apply-lvgl-patches.py:
 *   01  the evdev driver's capability test is 32 bits wide, which cannot see any
 *       ABS_MT_* code, and it tests only for ABS_X/ABS_Y. Harmless on this
 *       digitiser, which publishes those too, but it would reject a device that
 *       publishes only the multitouch axes. Insurance, not a rescue.
 *   02  the framebuffer driver maps one screen, but this panel is double
 *       buffered and the stock msm-fb-refresher pans between the halves, which
 *       is what made s5term flicker before it learned to paint both. This one is
 *       load bearing, and the log line it emits is how we know it took.
 *
 * Reversibility
 * -------------
 * Nothing persistent is changed by the terminal itself. No service, config
 * file, kernel or device tree edit comes from running it. The only persistent
 * change in this directory is the optional autostart script described in the
 * README, which is installed by hand and removable by hand. s5term and lvterm
 * both own /dev/fb0, so only one can run at a time; killing this hands the
 * screen back. The USB serial getty is untouched and stays available
 * throughout.
 *
 * The locked state turns the panel's backlight off through its sysfs node
 * (and back on to the saved value on wake); every one of those writes fails
 * open, and the exit path restores the backlight, so nothing a crash or a
 * refusal can do leaves the panel darker than it started.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include <linux/fb.h>
#include <linux/input.h>
#include <linux/kd.h>

/* Guard against a toolchain whose linux headers predate the KD_* constants. */
#ifndef KDSETMODE
#define KDSETMODE 0x4B3A
#endif
#ifndef KD_GRAPHICS
#define KD_GRAPHICS 0x00
#endif
#ifndef KD_TEXT
#define KD_TEXT 0x01
#endif

#include "lvgl.h"
#include "lv_linux_fbdev.h"
#include "lv_evdev.h"
#include "font/fmt_txt/lv_font_fmt_txt.h"

#include "../font16x32.h"

/* ------------------------------------------------------------ the terminal */

#define COLS        67            /* 1080 / 16 */
#define ROWS_FULL   60            /* 1920 / 32, whole screen */
#define ROWS_SPLIT  24            /* text rows left when the keyboard is up */

/* The upper area, top to bottom: the output, the input line, the button band.
 *
 * The three do not share a row. The band is three glyph rows tall and the input
 * line is exactly one with the text filling all of it, so a band that started
 * at the input line's top edge would cover the text being typed, and one that
 * started at its bottom edge would cover the keyboard's first row of keys. It
 * gets its own space instead, which is what ROWS_SPLIT pays for: the visible
 * output goes from 29 rows to 24.
 *
 * That is the only cost, and it is worth being precise about why. The space
 * comes out of the output, not the keyboard, so the keyboard actually gets
 * *taller* as the band grows - 976 px for its four rows now, against 992 when
 * the band was 48. The only thing lost is scrollback on screen at the moment
 * the keyboard is up, and 24 rows is still two thirds of the width in lines.
 *
 * 96 px rather than something merely larger because 48 px was not a change
 * anyone could see: against the original 32 px strip that is 16 px on a 1920 px
 * screen, 0.8% of the height, sitting next to a width that grew 4.7x. The
 * buttons were measurably taller the whole time - taps at y 900 and y 940 both
 * landed on a 896..943 band - and it read as "only wider" anyway. A target has
 * to look big to be worth aiming at.
 *
 * Wider was free from the start. The input line is a fixed-size label, not a
 * grid the text has to line up with, so it keeps the full terminal width and
 * the three buttons split the width between them. */
#define OUTPUT_H    (ROWS_SPLIT * GLYPH_H)   /* 768 */
#define INPUT_H     GLYPH_H                  /* 32  */
#define INPUT_Y     (OUTPUT_H)               /* 768 */
#define BTN_H       (3 * GLYPH_H)            /* 96 */
#define BTN_Y       (INPUT_Y + INPUT_H)      /* 800 */

/* Height of the text area, and therefore the keyboard. Not called LV_TEXT_H
 * because LVGL already defines that, and a redefinition here would be a
 * warning that is easy to misread as something else. Not a multiple of GLYPH_H
 * any more, and does not need to be: nothing in the text area is a grid except
 * the output itself, which is. */
#define TEXT_AREA_H (BTN_Y + BTN_H)          /* 896 */

#define INPUT_W     (COLS * GLYPH_W)
#define BTN_W       (COLS * GLYPH_W / 3)     /* 357, and 3 * 357 is the width */

/* A line of the visible screen. One byte per column plus a terminator, with
 * room for the widest printable character plus one. */
typedef struct {
    char cell[COLS * 4 + 1];
    int  len;
} line_t;

static line_t screen[ROWS_FULL];
static int    cur_row, cur_col;
static int    rows_visible = ROWS_FULL;

/* Verbose trace of the terminal state machine, on request. Used only when an
 * output byte is unaccounted for, which is not a place where guessing is
 * cheaper than looking. */
static uint32_t now_ms(void);
static int term_trace;
#define TERM_TRACE(...) do { if(term_trace) { \
    printf("t%5u", (unsigned)now_ms());       \
    printf(" r%d c%d", cur_row, cur_col);     \
    printf(" " __VA_ARGS__);                  \
    fflush(stdout);                           \
} } while(0)

/* Minimal line handling: carriage return, newline, backspace, and a bell we
 * ignore. This is deliberately not a VT emulator. It does not do scrolling in
 * the sense of moving text up, it just advances and wraps, which is all a shell
 * prompt and its output need, and which keeps the visible text stable instead
 * of scrolling the whole label every time a line is produced. */
static void term_clear(void)
{
    memset(screen, 0, sizeof screen);
    cur_row = cur_col = 0;
}

static void term_putc(char c)
{
    if(c == '\r') { cur_col = 0; return; }
    if(c == '\n') {
        TERM_TRACE("newline\n");
        cur_row++;
        if(cur_row >= rows_visible) {
            memmove(&screen[0], &screen[1],
                    (size_t)(rows_visible - 1) * sizeof screen[0]);
            memset(&screen[rows_visible - 1], 0, sizeof screen[0]);
            cur_row = rows_visible - 1;
        }
        return;
    }
    if(c == '\b') { if(cur_col) cur_col--; return; }
    if(c == '\t') { cur_col = (cur_col + 8) & ~7; return; }
    if((unsigned char)c < 32) return;

    if(cur_col >= COLS) { cur_col = 0; term_putc('\n'); }

    TERM_TRACE("cell %02x\n", (unsigned char)c);
    screen[cur_row].cell[cur_col] = c;
    if(cur_col + 1 > screen[cur_row].len)
        screen[cur_row].len = cur_col + 1;
    cur_col++;
}

/* Flatten the visible rows into one string for the label. */
static void term_render(char *out, size_t cap)
{
    size_t n = 0;
    for(int r = 0; r < rows_visible; r++) {
        for(int i = 0; i < screen[r].len && n + 2 < cap; i++)
            out[n++] = screen[r].cell[i];
        if(r + 1 < rows_visible && n + 2 < cap)
            out[n++] = '\n';
    }
    out[n] = '\0';
}

/* ---------------------------------------------- drawing the input line in
 *
 * The pty's echo is turned off, so what the user types has to appear on screen
 * from here, exactly as it would if a person were typing at the real terminal
 * this pty is attached to. That means overwriting the same cells the shell
 * would have echoed into, starting at the column the prompt left off at, and
 * leaving the cursor after the last character.
 *
 * Doing it as redraw-the-whole-line rather than insert-a-character is what
 * makes backspace come free: the textarea is the model, the screen is a view of
 * it, and erasing to end of line before repainting is the whole edit. */

/* Forward, because repaint() below is the terminal's only route to the display
 * and is needed by the keyboard helpers further up. */
static lv_obj_t *lbl_term;

/* Column on the current row where the input line begins, that is, where the
 * shell's prompt stopped. */
static int in_col;

static void term_erase_to_eol(void)
{
    for(int i = cur_col; i < COLS; i++) screen[cur_row].cell[i] = ' ';
    if(cur_col < screen[cur_row].len) screen[cur_row].len = cur_col;
    if(screen[cur_row].len < cur_col) screen[cur_row].len = cur_col;
}

static void term_draw_input(const char *txt, bool masked)
{
    int n = (int)strlen(txt);
    if(in_col + n > COLS) n = COLS - in_col;
    if(n < 0) n = 0;

    cur_col = in_col;
    term_erase_to_eol();

    for(int i = 0; i < n; i++) {
        screen[cur_row].cell[i + in_col] = masked ? '*' : txt[i];
        if(i + in_col + 1 > screen[cur_row].len)
            screen[cur_row].len = i + in_col + 1;
    }
    cur_col = in_col + n;
}

/* Push the visible screen into the label. One place, so that every path which
 * changes a cell ends up on the display without having to remember. */
static void repaint(void)
{
    char flat[sizeof screen[0].cell * ROWS_FULL];
    term_render(flat, sizeof flat);
    lv_label_set_text(lbl_term, flat);
}

/* Whether the visible last line is a password prompt, so the input line stops
 * drawing what is typed into it.
 *
 * "Password" is the only word worth matching. It is what sudo prints, and on
 * this device the screen is the only place the text exists, so a password
 * echoed in the clear would be a password written down in a room someone else
 * can walk into. Nothing else is guessed at, because a false positive would
 * silently hide what the user typed. */
static bool last_line_is_password(void)
{
    static const char needle[] = "assword";
    const line_t *l = &screen[cur_row];
    int end = l->len;
    while(end > 0 && l->cell[end - 1] == ' ') end--;
    if(end < 1 || l->cell[end - 1] != ':') return false;

    for(int i = 0; i + (int)sizeof needle - 1 <= end; i++) {
        int k;
        for(k = 0; needle[k]; k++) {
            char a = l->cell[i + k];
            if(a >= 'A' && a <= 'Z') a = (char)(a + 32);
            if(a != needle[k]) break;
        }
        if(!needle[k]) return true;
    }
    return false;
}

static int screen_contains(const char *s)
{
    for(int r = 0; r < rows_visible; r++) {
        char line[COLS + 1];
        int n = screen[r].len;
        if(n > COLS) n = COLS;
        memcpy(line, screen[r].cell, (size_t)n);
        line[n] = '\0';
        if(strstr(line, s)) return 1;
    }
    return 0;
}

/* ---------------------------------------------------------------- the font */

/* The terminal uses the 16x32 VGA bitmap font that s5term already uses, fed to
 * LVGL through the same descriptor every bundled LVGL font uses. No bundled
 * font is monospace and a terminal needs monospace, so this is the one piece of
 * the display that has to be ours.
 *
 * It fits with no conversion at all, which is the neat part. A PSF2 16x32 1bpp
 * glyph is 32 rows of 2 bytes, most significant bit leftmost, 64 bytes total.
 * LVGL's fmt_txt layout for bpp 1, box_w 16, box_h 32 is bytes_in_row = 2,
 * glyph stride 64, bitmap_index a byte offset into one flat array. That is the
 * same array, in the same order, so s5_font can be pointed at directly.
 *
 * The descriptor tables are filled at startup rather than generated as C,
 * because 256 identical entries is a loop, not a table worth writing out.
 */

/* Byte offset of each glyph. 16 pixels wide at 1 bit per pixel is 2 bytes a
 * row, 32 rows, so every glyph is exactly 64 bytes and glyph n starts at n*64.
 * The PSF2 glyph order is what it is, so this is a copy rather than a cast:
 * the arrays are laid out the same way but the compiler is not obliged to know
 * that, and being explicit costs 16 KB of RAM. */
static uint8_t s5_bitmap[GLYPH_COUNT * (GLYPH_W * GLYPH_H / 8)];

static lv_font_fmt_txt_glyph_dsc_t s5_glyph_dsc[GLYPH_COUNT];

/* Identity mapping across the whole 8 bit range, so any byte a shell can emit
 * finds the glyph s5term would have drawn for it. Format 0 tiny means
 * glyph_id = glyph_id_start + (codepoint - range_start), which with both set
 * to zero is the identity we want. */
static const lv_font_fmt_txt_cmap_t s5_cmap = {
    .range_start      = 0x00,
    .range_length     = 0x100,
    .glyph_id_start   = 0x00,
    .unicode_list     = NULL,
    .glyph_id_ofs_list = NULL,
    .list_length      = 0,
    .type             = LV_FONT_FMT_TXT_CMAP_FORMAT0_TINY,
};

static lv_font_fmt_txt_dsc_t s5_font_dsc;

static const lv_font_t font_s5_16x32 = {
    .get_glyph_dsc    = lv_font_get_glyph_dsc_fmt_txt,
    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,
    .line_height      = GLYPH_H,
    .base_line        = 0,
    .dsc              = &s5_font_dsc,
};

static void font_init(void)
{
    memcpy(s5_bitmap, s5_font, sizeof s5_bitmap);

    for(int i = 0; i < GLYPH_COUNT; i++) {
        s5_glyph_dsc[i].bitmap_index = (uint32_t)i * (GLYPH_W * GLYPH_H / 8);
        s5_glyph_dsc[i].adv_w        = GLYPH_W * 16;  /* 8.4 fixed point */
        s5_glyph_dsc[i].box_w        = GLYPH_W;
        s5_glyph_dsc[i].box_h        = GLYPH_H;
        s5_glyph_dsc[i].ofs_x        = 0;
        s5_glyph_dsc[i].ofs_y        = 0;
    }

    s5_font_dsc.glyph_bitmap  = s5_bitmap;
    s5_font_dsc.glyph_dsc     = s5_glyph_dsc;
    s5_font_dsc.cmaps         = &s5_cmap;
    s5_font_dsc.kern_dsc      = NULL;
    s5_font_dsc.kern_scale    = 0;
    s5_font_dsc.cmap_num      = 1;
    s5_font_dsc.bpp           = 1;
    s5_font_dsc.kern_classes  = 0;
    s5_font_dsc.bitmap_format = LV_FONT_FMT_TXT_PLAIN;
    s5_font_dsc.stride        = GLYPH_BYTES;
}

/* Counters that exist to be read. A terminal that looks like it is working and
 * a terminal that is working are indistinguishable from outside, and the only
 * way to tell them apart without a person tapping glass is to count the things
 * that crossed each boundary. These are the numbers that do that. */
static int   chars_sent;    /* bytes written to the pty by submit_line()      */
static int   lines_sent;    /* lines submitted, that is Enter presses         */
static int   interrupts;    /* ^C presses                                    */

/* Whether the shell is asking for something that must not be drawn. See
 * last_line_is_password(). */
static bool  masked;

/* True while something has been typed into the current input line and not yet
 * submitted. Used by the self test to tell "the shell had nothing to say" apart
 * from "the shell said something else". */
static int   editing;

/* ------------------------------------------------------------------ the UI */

static lv_obj_t *lbl_term;
static lv_obj_t *kb;
static lv_obj_t *ta_in;       /* the input line; the keyboard's only output   */
static lv_obj_t *btn_lock;    /* the lock key: turns the panel off            */
static lv_obj_t *btn_int;     /* ^C, because a screen has no Ctrl key         */
static lv_obj_t *btn_ctrl;    /* sticky control, for the next key             */
static lv_obj_t *lbl_ctrl;    /* its caption, so "armed" is visible           */
static int        state_off;         /* lock key pressed: panel is dark, any
                                        tap wakes it back to the terminal    */
static int        saved_brightness = -1;  /* what to restore after a lock     */
static int        ctrl_armed;        /* the CTRL key is waiting for one key   */

/* Defined with the rest of the sticky CTRL code, well below, and called from
 * kb_hide and the input line handlers, which come first. */
static void ctrl_set_armed(int on);
static void send_ctrl_char(unsigned char c);
static unsigned char ctrl_code_for(int key);

/* The keyboard is hidden rather than destroyed, so raising it is one flag. */
static void kb_show(void)
{
    lv_obj_clear_flag(kb, LV_OBJ_FLAG_HIDDEN);
}

/* Hiding throws away a half typed line, which is what a real terminal does
 * when you dismiss its keyboard and the same is what keeps a password that was
 * started and abandoned from sitting on the screen. */
static void kb_hide(void)
{
    lv_obj_add_flag(kb, LV_OBJ_FLAG_HIDDEN);
    /* An armed CTRL is always waiting for the very next key, and the keyboard
     * going away is the context changing underneath it. Dismissing the
     * keyboard throws the half typed line away, so the latch goes with it. */
    ctrl_set_armed(0);
    /* in_col first, then clear. See the note in submit_line: clearing the
     * textarea redraws the input line from wherever in_col points. */
    in_col = cur_col;
    term_erase_to_eol();
    lv_textarea_set_text(ta_in, "");
    repaint();
    editing = 0;
}

/* --------------- backlight, lock and wake --------------------------------
 *
 * The lock key turns the panel off: the backlight node is written to 0 and
 * every widget is hidden, leaving nothing but the black LVGL background (the
 * screen object itself is never hidden, because it is the thing that catches
 * the wake tap). Any tap then restores the saved brightness and brings the
 * terminal back with the keyboard already up.
 *
 * The whole path is fail open. The backlight node is readable and writable by
 * root on this kernel, but nothing here depends on that: if the read or write
 * fails the widgets still hide and show, which is the visible part, and the
 * saved value defaults to a sane brightness when it cannot be read.
 */
#define BL_PATH "/sys/class/backlight/panel/brightness"
#define BL_FALLBACK 133

static int backlight_read(void)
{
    FILE *f = fopen(BL_PATH, "r");
    if(!f) return -1;
    int v = -1;
    if(fscanf(f, "%d", &v) != 1) v = -1;
    fclose(f);
    return v;
}

static void backlight_write(int v)
{
    FILE *f = fopen(BL_PATH, "w");
    if(!f) return;
    fprintf(f, "%d\n", v);
    fclose(f);
}

/* The same restore without stdio, for the signal handler. snprintf is not on
 * the async-signal-safe list in principle; in practice it only formats an
 * integer, but the manual conversion here costs three lines and removes the
 * question entirely. */
static void backlight_restore_urgent(void)
{
    if(!state_off || saved_brightness <= 0) return;
    int v = saved_brightness;
    char buf[16];
    int n = 0;
    if(v == 0) buf[n++] = '0';
    else {
        char d[12];
        int m = 0;
        while(v > 0 && m < 12) { d[m++] = (char)('0' + v % 10); v /= 10; }
        while(m > 0) buf[n++] = d[--m];
    }
    buf[n++] = '\n';
    int fd = open(BL_PATH, O_WRONLY);
    if(fd < 0) return;
    ssize_t r = write(fd, buf, (size_t)n);
    (void)r;
    close(fd);
}

static void show_terminal(void)
{
    lv_obj_clear_flag(lbl_term,  LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(ta_in,     LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(btn_lock,  LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(btn_int,   LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(btn_ctrl,  LV_OBJ_FLAG_HIDDEN);
}

static void lock_screen(void)
{
    state_off = 1;
    if(saved_brightness < 0) saved_brightness = backlight_read();
    backlight_write(0);
    /* An armed CTRL waiting for a key must not survive the screen going off,
     * or the first key pressed after the wake would be swallowed as a control
     * character that the person pressing it never asked for. */
    ctrl_set_armed(0);
    lv_obj_add_flag(lbl_term, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(ta_in,    LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(btn_lock, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(btn_int,  LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(btn_ctrl, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(kb,       LV_OBJ_FLAG_HIDDEN);
    lv_obj_invalidate(lv_screen_active());
}

static void wake_screen(void)
{
    int v = saved_brightness > 0 ? saved_brightness : BL_FALLBACK;
    backlight_write(v);
    saved_brightness = -1;
    state_off = 0;
    show_terminal();
    kb_show();
    /* Logged because the backlight is the one part of this that lives outside
     * the process: a lock or a wake that did not reach the panel would look
     * identical on the glass. */
    printf("wake: panel on, brightness %d\n", v);
    fflush(stdout);
}

/* The lock key: dark screen, shell still running, any tap brings it back. */
static void lock_cb(lv_event_t *e)
{
    lv_indev_reset(lv_event_get_indev(e), NULL);
    lock_screen();
    printf("lock: panel off, brightness %d -> 0\n", saved_brightness);
    fflush(stdout);
}

static void tap_handler(lv_event_t *e)
{
    lv_indev_t *indev = lv_event_get_indev(e);

    if(state_off) {
        /* Any tap on the dark screen wakes it into the terminal with the
         * keyboard already up. */
        lv_indev_reset(indev, NULL);
        wake_screen();
        return;
    }

    /* Tapping the output raises and drops the keyboard. */
    lv_indev_reset(indev, NULL);
    if(lv_obj_has_flag(kb, LV_OBJ_FLAG_HIDDEN)) kb_show();
    else                                       kb_hide();
}

static int   pty_fd = -1;
static pid_t shell_pid = -1;
static int   text_fed;

/* Everything the Enter key does, in one function, so the self test can run the
 * real thing without a finger on the glass. */
static void submit_line(void);

/* Stages of the self test, run in order when LVTERM_SELFTEST is set. Each one
 * checks a link in the input path that the previous one does not. */
enum { LVST_NONE, LVST_UNAME, LVST_MASK };

/* Type a line and press Enter, by exactly the two calls the keyboard makes. */
static void selftest_submit(const char *line)
{
    lv_textarea_set_text(ta_in, line);
    lv_obj_send_event(ta_in, LV_EVENT_READY, NULL);
}

static void submit_line(void)
{
    TERM_TRACE("submit text len unknown\n");

    /* The textarea's buffer is freed by set_text below, so this has to be a
     * copy taken first. The length is bounded by the line discipline's own
     * limit in practice; the cap here is only so a paste cannot run off the
     * end of the stack. */
    char line[COLS * 4 + 2];
    const char *txt = lv_textarea_get_text(ta_in);
    size_t n = txt ? strlen(txt) : 0;
    if(n > sizeof line - 2) n = sizeof line - 2;
    memcpy(line, txt ? txt : "", n);
    line[n]     = '\n';
    line[n + 1] = '\0';

    /* The text is already on screen from term_draw_input, so all that is left
     * is to commit it and move on. */
    term_erase_to_eol();
    /* The shell's own newlines arrive as CRLF, which resets the column; this
     * synthetic one does not, so reset it here, or the next row's output would
     * start mid-line at the old cursor column and the shell's answer to the
     * submitted line would land off the start of the row. */
    cur_col = 0;
    term_putc('\n');
    repaint();

    /* in_col moves before the textarea is cleared, never after.
     *
     * lv_textarea_set_text fires LV_EVENT_VALUE_CHANGED synchronously, which
     * lands straight back in ta_changed and term_draw_input, and that redraws
     * the input line from wherever in_col currently points and then leaves the
     * cursor there. Clearing the textarea first therefore reset the cursor to
     * the previous line's starting column, and the in_col assignment after it
     * captured that wrong value. The visible result was the input line being
     * drawn over the top of the shell's own prompt, so the prompt disappeared
     * the moment anything was typed. */
    in_col = cur_col;
    lv_textarea_set_text(ta_in, "");
    editing = 0;
    if(masked) {
        masked = false;
        lv_textarea_set_password_mode(ta_in, false);
    }

    lines_sent++;
    if(pty_fd < 0) return;
    ssize_t w = write(pty_fd, line, n + 1);
    if(w > 0) chars_sent += (int)w;
}

/* ------------------------------------------------- the input line handlers */

/* The text the input line held the last time anything changed it, so a key
 * press can be told apart from our own edits. */
static char ta_prev[256];
static int  in_ctrl_eat;      /* set while we take a character back out again */

/* The character the keyboard just inserted, or 0 if this was not a plain
 * insertion of exactly one byte at the end of the line.
 *
 * Comparing the whole line is deliberate rather than tracking the cursor. The
 * cursor can be moved with the arrow keys, so insertion is not always at the
 * end, and a cursor-position scheme would then have to also prove the cursor
 * is where it thinks it is. Anything this does not recognise - a mid-line
 * insert, a multi-byte LV_SYMBOL_ key like backspace or an arrow, a delete -
 * is reported as "not a key press", and the caller lets it do its own job with
 * the latch cleared. A digit or a space typed with the latch on is the same
 * case: no control character exists, so nothing is sent and the character is
 * left in the line rather than eaten. */
static int inserted_char(const char *before, const char *after)
{
    size_t bl = strlen(before), al = strlen(after);
    if(al != bl + 1) return 0;
    if(bl && memcmp(before, after, bl) != 0) return 0;
    return (unsigned char)after[bl];
}

/* The keyboard's only delivery mechanism is lv_textarea_add_text() on the
 * object it was given, so this is where every key press lands - and therefore
 * also where an armed CTRL key has to be noticed, since there is no key event
 * to intercept upstream of it. */
static void ta_changed(lv_event_t *e)
{
    (void)e;
    const char *txt = lv_textarea_get_text(ta_in);

    if(ctrl_armed && !in_ctrl_eat) {
        int key = inserted_char(ta_prev, txt);
        unsigned char code = key ? ctrl_code_for(key) : 0;
        if(code) {
            /* Put the character back: the control byte goes to the pty, never
             * into the input line. The cursor sits just after what the
             * keyboard inserted, so delete backwards to take exactly that one
             * back out. in_ctrl_eat keeps this edit from looking like a second
             * key press to the code it just ran. */
            in_ctrl_eat = 1;
            send_ctrl_char(code);
            lv_textarea_delete_char(ta_in);
            in_ctrl_eat = 0;
            ctrl_set_armed(0);
            txt = lv_textarea_get_text(ta_in);
        }
        else {
            ctrl_set_armed(0);
        }
    }

    editing = 1;
    snprintf(ta_prev, sizeof ta_prev, "%s", txt);
    TERM_TRACE("ta_changed text='%s'\n", txt);
    term_draw_input(txt, masked);
    repaint();
}

/* Enter, with one_line set, arrives here and nothing else. The keyboard adds a
 * newline, sees one_line, sends this, and never inserts the newline itself. */
static void ta_ready(lv_event_t *e)
{
    (void)e;
    submit_line();
}

/* The keyboard's own key event, watched from outside.
 *
 * ta_changed is the only place a character can arrive, but it is not the only
 * kind of key there is. Backspace on an empty line, an arrow, a shift or mode
 * key all report themselves without changing the text, so without ever
 * reaching ta_changed - and an armed CTRL would then sit and wait, until the
 * next letter was swallowed as a control character that the person typing it
 * had no reason to expect. Backspace on an empty line is the ordinary way to
 * hit that, since it is the one key whose nothing-happens is invisible.
 *
 * lv_keyboard registers its own handler as an ordinary callback in its
 * constructor, so this one, added later, runs after it: the text has already
 * been dealt with by the time this is called, and a latch still armed here is
 * one that no key claimed. That is the whole test. */
static void kb_key(lv_event_t *e)
{
    (void)e;
    if(ctrl_armed) ctrl_set_armed(0);
}

/* The keyboard's hide key. */
static void ta_cancel(lv_event_t *e)
{
    (void)e;
    kb_hide();
}

/* --------------------------------------------------- the sticky CTRL key
 *
 * A shell is driven by control characters, and a touchscreen keyboard can
 * only ever produce printable ones. The ^C button exists because of exactly
 * that, and it is a dead end in two ways: it can only make one of them, and
 * the on-screen keyboard's own shift/arrow keys are not usable as modifiers
 * because lv_keyboard swallows them (see ctrl_tapped).
 *
 * So the CTRL key here is a latch, not a chord. Tapping it arms it; the next
 * key pressed is combined with control and the latch clears itself, so no key
 * can be left stuck down. Tapping CTRL again cancels it without sending
 * anything, which is what makes it safe to arm by mistake.
 *
 * "The next key" means the next character from the keyboard, or the ^C button.
 * Both routes go through send_ctrl_char, and a control byte goes straight to
 * the pty rather than into the input line: the input line is a local buffer
 * that only reaches the shell on Enter, so a control character typed into it
 * would sit there doing nothing. Sending it directly is the whole point - it is
 * how ^D reaches a shell at its prompt, or ^C interrupts something, without
 * submitting a line. */

/* One byte to the master. Ctrl characters are not text, so this never touches
 * the input line or the screen buffer.
 *
 * Logged unconditionally, not under TERM_TRACE. A control character is the one
 * thing this program does that produces no visible change on the glass, so a
 * press that went somewhere unexpected has to leave a trace by default - the
 * same reason the lock and wake log themselves. */
static void send_ctrl_char(unsigned char c)
{
    printf("ctrl: sent 0x%02x\n", c);
    fflush(stdout);
    if(pty_fd < 0) return;
    ssize_t w = write(pty_fd, &c, 1);
    if(w > 0) chars_sent += (int)w;
}

/* The C0 control for a printable key, or 0 if the key has no control meaning.
 *
 * Letters follow the usual rule, and the case is dropped rather than treated as
 * distinct: on a real terminal Ctrl-Shift-C is ETX, not "Ctrl-C with a shift
 * on it", and a key that reports which shift is down would be the only reason
 * to do otherwise. The brackets follow ASCII, so Ctrl-[ really is ESC. */
static unsigned char ctrl_code_for(int key)
{
    if(key >= 'a' && key <= 'z') return (unsigned char)(key - 'a' + 1);
    if(key >= 'A' && key <= 'Z') return (unsigned char)(key - 'A' + 1);
    switch(key) {
        case ' ':  return 0x00;    /* NUL, as a real terminal sends it        */
        case '[':  return 0x1b;    /* ESC                                    */
        case '\\': return 0x1c;    /* FS   (Ctrl-\ quits less)                */
        case ']':  return 0x1d;    /* GS                                     */
        case '^':  return 0x1e;    /* RS                                     */
        case '_':  return 0x1f;    /* US                                     */
        default:   return 0;       /* digits and punctuation: no C0 meaning   */
    }
}

/* Repaint the CTRL caption so the latch is something you can see rather than
 * something you have to remember. The pressed colour alone is not enough: the
 * finger is already off the glass by the time anyone looks. */
static void ctrl_set_armed(int on)
{
    ctrl_armed = on;
    /* kb_hide runs before the buttons exist when the keyboard is dismissed
     * during setup, and a latch is just a bit until then. */
    if(!btn_ctrl) return;
    lv_label_set_text(lbl_ctrl, on ? "CTRL ON" : "CTRL");
    lv_obj_set_style_text_color(lbl_ctrl,
                                lv_color_hex(on ? 0xffe080 : 0xd0d0e0), 0);
    lv_obj_set_style_border_color(btn_ctrl,
                                  lv_color_hex(on ? 0xffc040 : 0x9090b0), 0);
    lv_obj_set_style_bg_color(btn_ctrl,
                              lv_color_hex(on ? 0x403010 : 0x181828), 0);
}

/* The CTRL key itself. Tapping it arms or cancels; it never sends anything. */
static void ctrl_cb(lv_event_t *e)
{
    (void)e;
    ctrl_set_armed(!ctrl_armed);
    TERM_TRACE("ctrl: %s\n", ctrl_armed ? "armed" : "cancelled");
}

/* Stop the command in the foreground. */
static void intr_send(void)
{
    static const char etx = 0x03;
    interrupts++;
    send_ctrl_char((unsigned char)etx);
}

/* ^C with the CTRL key armed is Ctrl-C again, which is exactly what the button
 * already is - the combination is a no-op that still has to clear the latch, or
 * a stuck CTRL would silently change the *next* command. So it sends, and
 * disarms. */
static void intr_cb(lv_event_t *e)
{
    (void)e;
    intr_send();
    ctrl_set_armed(0);
}

/* Tapping the input line only ever raises the keyboard. Tapping it must never
 * hide the keyboard, or the line being typed into would vanish on the way in. */
static void input_tap(lv_event_t *e)
{
    (void)e;
    lv_obj_clear_flag(kb, LV_OBJ_FLAG_HIDDEN);
}

/* ------------------------------------------------- the virtual tap driver
 *
 * Everything about this program being usable is downstream of a finger landing
 * on glass, and a finger cannot be asked for on demand. That is the reason the
 * question "does tapping a key put the right character in the line" sat
 * unanswerable for as long as it did.
 *
 * LVGL's whole input interface is one function: a read callback that fills in a
 * point and a pressed or released flag. That is all a digitiser is. So a second
 * indev built on the same callback, driven from a file, produces a press that
 * goes through the identical hit test, the identical button matrix and the
 * identical event dispatch as a real one.
 *
 * It is off unless LVTERM_TAPLOG names a file, and the real evdev indev stays
 * registered beside it, so this never replaces the digitiser. It is an
 * instrument, not a feature. */

static lv_indev_t *vindev;
static FILE       *taplog_f;
static int         vtap_phase;   /* 0 idle, 1 pressing, 2 release outstanding */
static int         vtap_hold;    /* iterations left to hold the press           */
static int         vtap_x, vtap_y;
static int         vtaps;

/* How many main loop iterations a synthesised press is held for.
 *
 * The indev's read timer runs every LV_DEF_REFR_PERIOD, 33ms, and the main loop
 * turns over in about 20ms, so a press held for a single iteration falls
 * between two timer fires and is never seen: the first version of this reported
 * twelve confident "press at 67 1080" lines and LVGL had observed none of them.
 * Eight is roughly 200ms, a short tap and four times the timer period.
 *
 * The read is left to that timer rather than driven by calling lv_indev_read
 * directly. Calling it by hand did deliver the press, and then killed the
 * process on the release with nothing in dmesg, which is not a trade worth
 * making for a test instrument. A real digitiser holds its pressed state for as
 * long as a finger is down and needs none of this. */
#define VTAP_HOLD 8

static void vtap_read_cb(lv_indev_t *indev, lv_indev_data_t *data)
{
    (void)indev;
    data->point.x          = vtap_x;
    data->point.y          = vtap_y;
    data->state            = vtap_phase == 1 ? LV_INDEV_STATE_PRESSED
                                            : LV_INDEV_STATE_RELEASED;
    data->continue_reading = false;

    /* Printed only on a change of state. Printing every read would be thirty
     * lines a second of "released at 0 0", and the point of this callback being
     * visible at all is to tell "LVGL never asked" apart from "LVGL asked and
     * did nothing with the answer". */
    static lv_indev_state_t prev = (lv_indev_state_t)-1;
    if(data->state != prev) {
        prev = data->state;
        printf("vread: %-8s at %d %d  disp=%s  mode=%d\n",
               data->state == LV_INDEV_STATE_PRESSED ? "PRESSED" : "RELEASED",
               vtap_x, vtap_y,
               lv_indev_get_display(indev) ? "set" : "NULL",
               (int)lv_indev_get_mode(indev));
        fflush(stdout);
    }
}

/* One phase per main loop iteration, so a tap lasts about as long as a real
 * one and the press is genuinely observed between two reads rather than being
 * pressed and released inside a single frame. See VTAP_HOLD for why it is
 * eight. */
static void vtap_pump(void)
{
    if(vtap_phase == 0 && taplog_f) {
        char line[64];
        /* clearerr before every read, not after. The file is empty when this
         * starts, so the very first fgets returns NULL and sets the stream's
         * end-of-file indicator. Nothing ever clears it, so every later fgets
         * returns NULL too and the driver silently sees no taps at all, which
         * is exactly what happened the first time this was run. A stream being
         * at end of file says nothing about whether more has been appended
         * since. */
        clearerr(taplog_f);
        while(fgets(line, sizeof line, taplog_f) != NULL) {
            if(sscanf(line, "%d %d", &vtap_x, &vtap_y) == 2) {
                vtap_phase = 1;
                vtap_hold = VTAP_HOLD;
                vtaps++;
                break;
            }
        }
    }

    if(vtap_phase == 1) {
        if(vtap_hold == VTAP_HOLD) {
            printf("vtap %d: press at %d %d\n", vtaps, vtap_x, vtap_y);
            fflush(stdout);
        }
        if(--vtap_hold <= 0) vtap_phase = 2;
    } else if(vtap_phase == 2) {
        printf("vtap %d: release at %d %d\n", vtaps, vtap_x, vtap_y);
        fflush(stdout);
        vtap_phase = 0;
    }
}

/* A short report of the object tree and the terminal state. The screen cannot
 * be looked at, so what the objects are actually doing has to be readable
 * somewhere. This is the instrument that says whether a layout is right,
 * instead of inferring it from pixel counts. */
static void diag(const char *when)
{
    lv_area_t a;
    printf("diag %-8s off=%d pty_fd=%d shell_pid=%d row=%d col=%d "
           "fed=%d sent=%d lines=%d intr=%d in_col=%d mask=%d edit=%d "
           "ctrl=%d vtaps=%d\n",
           when, state_off, pty_fd, (int)shell_pid, cur_row, cur_col,
           text_fed, chars_sent, lines_sent, interrupts, in_col,
           (int)masked, editing, ctrl_armed, vtaps);

    /* The input line's own text. It is the one piece of state whose contents
     * cannot be inferred from the counters, and it is the piece a key press is
     * supposed to change, so it is printed. */
    const char *pending = ta_in ? lv_textarea_get_text(ta_in) : NULL;
    printf("  typing: \"%s\"\n", pending ? pending : "");

    const char *names[] = { "lbl_term", "ta_in", "btn_ctrl", "btn_int",
                            "btn_lock", "kb" };
    lv_obj_t *objs[]  = { lbl_term, ta_in, btn_ctrl, btn_int, btn_lock, kb };
    for(int i = 0; i < (int)(sizeof objs / sizeof objs[0]); i++) {
        if(!objs[i]) { printf("  %-9s (null)\n", names[i]); continue; }
        lv_obj_get_coords(objs[i], &a);
        printf("  %-9s x %4d..%4d y %4d..%4d hidden=%d\n",
               names[i], a.x1, a.x2, a.y1, a.y2,
               lv_obj_has_flag(objs[i], LV_OBJ_FLAG_HIDDEN));
    }
    fflush(stdout);
}

/* The visible screen as text.
 *
 * The counters prove that bytes crossed each boundary; they cannot prove the
 * shell said the right thing, and on a device with no way to photograph the
 * display this is the only record of what is actually on the glass. Printed
 * whenever new output arrives, so the log reads as a transcript rather than a
 * series of assertions that something happened. */
static void dump_screen(const char *when)
{
    printf("screen %s:\n", when);
    for(int r = 0; r < rows_visible; r++) {
        char line[COLS + 1];
        int n = screen[r].len;
        if(n <= 0) continue;
        if(n > COLS) n = COLS;
        memcpy(line, screen[r].cell, (size_t)n);
        line[n] = '\0';
        printf("  %2d |%s|\n", r, line);
    }
    fflush(stdout);
}

static void feed_terminal(const char *buf, int n)
{
    TERM_TRACE("feed %d bytes\n", n);
    for(int i = 0; i < n; i++)
        term_putc(buf[i]);

    /* A password prompt switches the input line to masked. Doing it here
     * rather than in a timer means the switch happens on the same read that
     * printed the prompt, so there is no window where the prompt is visible and
     * the line is not yet masked. */
    bool want_mask = last_line_is_password();
    if(want_mask != masked) {
        masked = want_mask;
        lv_textarea_set_password_mode(ta_in, masked);
        if(masked) {
            /* Whatever was half typed before the prompt arrived is not part of
             * the password. in_col moves first for the same reason it does in
             * submit_line: the set_text below redraws from in_col, and with
             * in_col still pointing at the start of the line the redraw would
             * erase the prompt the shell had just printed. */
            in_col = cur_col;
            term_erase_to_eol();
            lv_textarea_set_text(ta_in, "");
        }
    }

    /* The input line always begins where the shell left the cursor. Doing it
     * here rather than at each place that moves the cursor means the prompt
     * that arrives after a line is submitted anchors the next one correctly,
     * including for the very first prompt, which is printed before anything has
     * been typed. While a line is being edited in_col is left alone, since then
     * the shell is not the one deciding where text goes. */
    if(!editing) in_col = cur_col;

    repaint();
    text_fed += n;
}

/* ----------------------------------------------------------------- the pty */

/* pty_fd and shell_pid are declared with the diagnostics above, which report
 * them. */
static void pty_start(void)
{
    char name[128];
    int master = posix_openpt(O_RDWR | O_NOCTTY);
    if(master < 0) { perror("posix_openpt"); return; }
    if(grantpt(master) < 0 || unlockpt(master) < 0) { perror("grantpt"); return; }
    if(ptsname_r(master, name, sizeof name) != 0) { perror("ptsname"); return; }

    int slave = open(name, O_RDWR);
    if(slave < 0) { perror("open pty slave"); return; }

    struct winsize ws = { .ws_row = ROWS_FULL, .ws_col = COLS };
    ioctl(slave, TIOCSWINSZ, &ws);

    pid_t pid = fork();
    if(pid == 0) {
        close(master);
        setsid();

        /* Take the slave as the controlling terminal, then claim the foreground
         * process group.
         *
         * Two things are going on and both of them cost a debugging session.
         *
         * TIOCSCTTY is called with 1, not 0. With 0 the kernel refuses outright
         * on a pty slave, returning EPERM, because on a pty the slave is not
         * acquired that way: it becomes the controlling terminal of whichever
         * session leader opens it. The refusal is silent in the sense that
         * nothing else goes wrong, the shell still runs and still prints a
         * prompt, and the only symptom is that the terminal has no foreground
         * process group at all.
         *
         * That symptom is the ^C button killing lvterm. The line discipline
         * raises SIGINT on the terminal's foreground group; there wasn't one,
         * so the signal went somewhere else, and the process that died was the
         * terminal rather than the command the user was trying to stop. Passing
         * 1 asks the kernel to force the association, which a session leader
         * may do and which we can, being root. TIOCSPGRP then has a tty to work
         * with, where before it answered "Not a tty".
         *
         * Both calls are made here, in the child, because TIOCSPGRP is only
         * permitted from the session's foreground process group, which right
         * now is this process. */
        if(ioctl(slave, TIOCSCTTY, 1) < 0) perror("TIOCSCTTY");
        pid_t fg = getpgrp();
        if(ioctl(slave, TIOCSPGRP, &fg) < 0) perror("TIOCSPGRP");

        /* Echo off, and that is the single most important line in this file.
         *
         * The pty has a line discipline that will happily echo every byte typed
         * into it back out again, which is right for a real terminal and wrong
         * here: term_draw_input already draws the input line onto the screen
         * from the textarea, so with echo left on the shell would draw a second
         * copy of every character one line further down and the two would
         * disagree. It would also put a sudo password in the clear on a screen
         * that is the only output this device has.
         *
         * Canonical mode stays on. It is what makes the shell read a whole line
         * at a time, which is the same contract the input line already has, and
         * turning it off would mean reimplementing line editing against a
         * stream that gives no backspace and no cursor. ISIG stays on too, so
         * that a single ETX byte written to the master is a real SIGINT, which
         * is what the ^C button sends. */
        struct termios tio;
        if(tcgetattr(slave, &tio) == 0) {
            tio.c_lflag &= (tcflag_t)~(ECHO | ECHOE | ECHOK | ECHONL | ECHOCTL);
            tcsetattr(slave, TCSANOW, &tio);
        }

        dup2(slave, 0); dup2(slave, 1); dup2(slave, 2);
        if(slave > 2) close(slave);
        setenv("TERM", "xterm-256color", 1);
        execl("/bin/sh", "sh", (char *)NULL);
        _exit(127);
    }

    close(slave);
    fcntl(master, F_SETFL, O_NONBLOCK);
    pty_fd = master;
    shell_pid = pid;
}

/* -------------------------------------------------------------------- main */

static volatile int want_quit;
static volatile int quit_signal;

/* Used by on_fatal below and by the exit path; defined with its own comment
 * after the handlers. */
static void console_write_mode(int mode);

static void on_signal(int sig)
{
    quit_signal = sig;
    want_quit = 1;
}

/* A crash that says nothing is the hardest kind of bug to work on, and this
 * program has now died three times with an empty dmesg. Nothing is guessed at
 * here: the handler exists purely so the log ends with a word about what
 * happened. write(2) is async-signal-safe, which is why it is not printf. */
static void on_fatal(int sig)
{
    char msg[64];
    int n = snprintf(msg, sizeof msg, "\nlvterm: FATAL signal %d\n", sig);
    if(n > 0) { ssize_t r = write(2, msg, (size_t)n); (void)r; }
    /* Hand the console back so a crash does not leave the device without one.
     * open/ioctl/close are plain syscalls, and KD_TEXT never hits perror, so
     * this stays signal-safe. */
    console_write_mode(KD_TEXT);
    /* A crash while the lock key had the panel off would otherwise leave the
     * screen dark with nothing to wake it but a power cycle. Same reasoning
     * about signal safety: open/write/close and nothing else. */
    backlight_restore_urgent();
    _exit(128 + sig);
}

static void install_fatal_handlers(void)
{
    static const int sigs[] = { SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT };
    for(size_t i = 0; i < sizeof sigs / sizeof sigs[0]; i++)
        signal(sigs[i], on_fatal);
}

/* ------------------------------------------------------------- the console
 *
 * The kernel's VGA text console is attached to the same /dev/fb0 lvterm owns,
 * and it never stopped drawing into it: its cursor keeps blinking every few
 * hundred milliseconds wherever the console's cursor last stood, in the
 * kernel's 0x00-byte colour space, so it shows up on top of our picture as a
 * small grey cell flickering near the bottom of an otherwise stable screen.
 * (Measured: 16 px at y 1918..1919 x 136..143; the blob follows the
 * console cursor when the cursor is moved, which is how it was identified.)
 *
 * The standard fix for a fullscreen framebuffer program is to take the
 * console into KD_GRAPHICS mode, which tells the kernel to stop rendering text
 * into the framebuffer altogether, and to hand it back on the way out. Both
 * calls fail open: a console that refuses the mode change leaves nothing worse
 * than the original blinking cursor, a cosmetic bug, not a reason to die for.
 */
static void console_write_mode(int mode)
{
    int fd = open("/dev/tty0", O_RDWR);
    if(fd < 0) fd = open("/dev/console", O_RDWR);
    if(fd < 0) return;
    if(ioctl(fd, KDSETMODE, mode) < 0 && mode == KD_GRAPHICS)
        perror("KDSETMODE(KD_GRAPHICS)");   /* the blink just stays; so be it */
    close(fd);
}

static uint32_t tick_cb(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

static uint32_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

static int last_reported = -1;
static int last_vtaps = -1;
static uint32_t last_beat;

int main(void)
{
    if(getuid() != 0) {
        fprintf(stderr, "lvterm: must run as root "
                        "(needs /dev/fb0 and /dev/input/event2)\n");
        return 1;
    }

    signal(SIGINT,  on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);
    install_fatal_handlers();

    /* SIGHUP is ignored rather than caught. This is started from an SSH session
     * and is meant to outlive it: the first run died without a word in dmesg,
     * at the moment its starting session went away, and the only suspect with
     * no kernel footprint is the default SIGHUP disposition. Ignoring it makes
     * the process's lifetime depend on nothing but an explicit kill. */
    signal(SIGHUP, SIG_IGN);

    lv_init();
    lv_tick_set_cb(tick_cb);
    font_init();

    lv_display_t *disp = lv_linux_fbdev_create();
    if(disp == NULL) { fprintf(stderr, "lvterm: no display\n"); return 1; }
    if(lv_linux_fbdev_set_file(disp, "/dev/fb0") != LV_RESULT_OK) {
        fprintf(stderr, "lvterm: cannot open /dev/fb0\n");
        return 1;
    }

    /* The framebuffer is ours: take the kernel console out of text mode so it
     * stops blinking its cursor over the picture (see the console comment).
     * Fails open — a refused mode change only leaves the cursor blinking. */
    console_write_mode(KD_GRAPHICS);

    /* The driver opens the node itself and reads the capability bitmap to work
     * out what kind of device it is. We name the type explicitly instead, from
     * what absprobe measured, so nothing here depends on that test. The node
     * publishes ABS_X/ABS_Y 0..1079/0..1919 as well as the multitouch axes, and
     * the driver calibrates against the legacy pair, which is why the ranges
     * line up with the 1080x1920 panel without any scaling here. */
    lv_indev_t *indev =
        lv_evdev_create(LV_INDEV_TYPE_POINTER, "/dev/input/event2");
    if(indev == NULL) {
        fprintf(stderr, "lvterm: cannot open /dev/input/event2\n");
        return 1;
    }
    printf("indev: type=%d (1=pointer)  display=%dx%d\n",
           (int)lv_indev_get_type(indev),
           lv_display_get_horizontal_resolution(disp),
           lv_display_get_vertical_resolution(disp));
    fflush(stdout);

    /* The virtual tap driver, if asked for. Created after the real one so the
     * real digitiser is index 0 and keeps priority. */
    term_trace = getenv("LVTERM_TRACE") ? 1 : 0;
    const char *taplog = getenv("LVTERM_TAPLOG");
    if(taplog) {
        int fd = open(taplog, O_RDONLY);
        if(fd < 0) {
            perror(taplog);
        } else {
            taplog_f = fdopen(fd, "r");
            vindev = lv_indev_create();
            lv_indev_set_type(vindev, LV_INDEV_TYPE_POINTER);
            lv_indev_set_read_cb(vindev, vtap_read_cb);
            lv_indev_set_display(vindev, disp);
            printf("vtap: virtual pointer registered, reading %s\n", taplog);
            fflush(stdout);
        }
    }

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x000000), 0);
    lv_obj_set_style_pad_all(scr, 0, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(scr, LV_OBJ_FLAG_CLICKABLE);
    /* Every widget is hidden while the lock key has the screen off, so a tap
     * would fall through to nothing without a handler here. This one is what
     * makes "any tap wakes it" true; see tap_handler. */
    lv_obj_add_event_cb(scr, tap_handler, LV_EVENT_CLICKED, NULL);

    /* Terminal output, top of the screen, stopping above the input line. */
    lbl_term = lv_label_create(scr);
    lv_obj_set_width(lbl_term, COLS * GLYPH_W);
    lv_obj_set_height(lbl_term, OUTPUT_H);
    lv_label_set_long_mode(lbl_term, LV_LABEL_LONG_MODE_CLIP);
    lv_obj_set_style_text_font(lbl_term, &font_s5_16x32, 0);
    lv_obj_set_style_text_color(lbl_term, lv_color_hex(0xe8e8e8), 0);
    lv_obj_set_style_bg_color(lbl_term, lv_color_hex(0x000000), 0);
    lv_obj_set_style_border_width(lbl_term, 0, 0);
    lv_obj_set_style_pad_all(lbl_term, 0, 0);
    lv_obj_align(lbl_term, LV_ALIGN_TOP_LEFT, 0, 0);
    lv_obj_add_flag(lbl_term, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(lbl_term, tap_handler, LV_EVENT_CLICKED, NULL);

    /* The input line, the bottom two text rows of the upper area.
     *
     * This object is not decoration. lv_keyboard has no idea how to report a
     * key press: its whole delivery path is lv_textarea_add_text() and friends
     * on whatever was passed to lv_keyboard_set_textarea(). Binding the keyboard
     * to the output label, as the first build did, therefore did nothing at all
     * except corrupt the output, because the next pty read overwrote whatever
     * the user had typed and nothing ever sent a byte back. A textarea is not
     * optional here; it is the only place a key press can arrive.
     *
     * one_line is what makes Enter usable: the keyboard sends a newline, sees
     * one_line, and turns it into LV_EVENT_READY on this object instead of
     * leaving a stray newline in the text. READY is the submit hook. */
    ta_in = lv_textarea_create(scr);
    lv_obj_set_size(ta_in, INPUT_W, INPUT_H);
    lv_obj_align(ta_in, LV_ALIGN_TOP_LEFT, 0, INPUT_Y);
    lv_textarea_set_one_line(ta_in, true);
    lv_textarea_set_cursor_click_pos(ta_in, false);
    /* A blinking cursor repaints its cell every half second, which is exactly
     * the kind of idle framebuffer churn this program exists to avoid: the
     * idle screen must sit still too. A steady block cursor says the same
     * thing with none of the flicker, so the blink is off (the textarea takes
     * the blink period from the CURSOR part's anim duration; zero stops it).
     * The default theme sets 400 ms for LV_PART_CURSOR | LV_STATE_FOCUSED,
     * so the override has to carry the same selector. */
    lv_obj_set_style_anim_duration(ta_in, 0, LV_PART_CURSOR | LV_STATE_FOCUSED);
    lv_obj_set_style_text_font(ta_in, &font_s5_16x32, 0);
    lv_obj_set_style_text_color(ta_in, lv_color_hex(0xe8e8e8), 0);
    lv_obj_set_style_bg_color(ta_in, lv_color_hex(0x000000), 0);
    lv_obj_set_style_border_width(ta_in, 0, 0);
    lv_obj_set_style_pad_all(ta_in, 0, 0);
    lv_obj_add_flag(ta_in, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(ta_in, ta_changed,   LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_add_event_cb(ta_in, ta_ready,    LV_EVENT_READY,         NULL);
    lv_obj_add_event_cb(ta_in, ta_cancel,   LV_EVENT_CANCEL,        NULL);
    lv_obj_add_event_cb(ta_in, input_tap,   LV_EVENT_CLICKED,       NULL);

    /* The button band. Three keys, the full width, three glyph rows tall, in a
     * band of their own under the input line. Order is CTRL, ^C, LOCK: the two
     * that act on the shell are where the hand already is, and LOCK is last
     * because it is the one you press when you are not typing.
     *
     * They are built by one loop because they are the same three lines of
     * styling three times over, and the alternative is three blocks that have
     * to be kept in agreement. The captions and callbacks are parallel arrays;
     * anything else would mean three near-identical blocks differing only in
     * colour, which is exactly the kind of duplication that goes stale. */
    static const struct {
        const char *text;
        uint32_t    fill, pressed, border, ink;
        lv_event_cb_t cb;
    } band[3] = {
        { "CTRL",   0x181828, 0x303058, 0x9090b0, 0xd0d0e0, ctrl_cb },
        { "^C",     0x301010, 0x802020, 0xc04040, 0xe0a0a0, intr_cb },
        { "LOCK",   0x102020, 0x006060, 0x00a0a0, 0x80e0e0, lock_cb },
    };
    for(int i = 0; i < 3; i++) {
        lv_obj_t *b = lv_button_create(scr);
        lv_obj_set_size(b, BTN_W, BTN_H);
        lv_obj_align(b, LV_ALIGN_TOP_LEFT, i * BTN_W, BTN_Y);
        lv_obj_set_style_pad_all(b, 0, 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(band[i].fill), 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(band[i].pressed),
                                  LV_STATE_PRESSED);
        lv_obj_set_style_border_width(b, 2, 0);
        lv_obj_set_style_border_color(b, lv_color_hex(band[i].border), 0);
        lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(b, band[i].cb, LV_EVENT_CLICKED, NULL);
        lv_obj_t *cap = lv_label_create(b);
        lv_label_set_text(cap, band[i].text);
        lv_obj_set_style_text_font(cap, &font_s5_16x32, 0);
        lv_obj_set_style_text_color(cap, lv_color_hex(band[i].ink), 0);
        lv_obj_center(cap);
        if(i == 0) { btn_ctrl = b; lbl_ctrl = cap; }
        else if(i == 1) btn_int = b;
        else btn_lock = b;
    }
    ctrl_set_armed(0);      /* paints the disarmed colours it will start in */

    /* The keyboard, lower half. It gets a proportional font rather than the
     * terminal's monospace one because its keys are labelled with LV_SYMBOL_*
     * characters (backspace, return, arrows) that live above U+2000, and the
     * 16x32 VGA font has nothing above 0xFF. This is the one place a bundled
     * font is the right answer. */
    kb = lv_keyboard_create(scr);
    lv_obj_set_width(kb, 1080);
    lv_obj_set_height(kb, 1920 - TEXT_AREA_H);
    lv_obj_set_style_text_font(kb, &lv_font_montserrat_28, 0);
    lv_keyboard_set_mode(kb, LV_KEYBOARD_MODE_TEXT_LOWER);
    lv_keyboard_set_textarea(kb, ta_in);
    lv_obj_align(kb, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_add_flag(kb, LV_OBJ_FLAG_HIDDEN);

    term_clear();
    in_col = 0;
    pty_start();
    /* Never come up on a dark panel. If the last run died while the lock key
     * had the backlight at 0, the node is still 0 and the terminal would sit
     * there invisible until the first tap. */
    if(backlight_read() == 0) backlight_write(BL_FALLBACK);
    show_terminal();

    /* Force a full screen redraw before the first flush.
     *
     * LVGL repaints invalidated areas, and on a fresh start very little is
     * invalid: each object invalidates its own rectangle, so the screen
     * background is never repainted and every pixel outside a new object keeps
     * whatever the last program on this framebuffer left there. This is not
     * theoretical. The first run of this build came up showing a QWERTY
     * keyboard that the object tree said was hidden, drawn by the previous
     * lvterm before it was killed, sitting under a correctly rendered terminal.
     *
     * On a desktop the compositor starts from a clean slate and nobody notices
     * the bug. Here the framebuffer is whatever the last program left, which is
     * why it showed up: the user's report of "the regular console came" was
     * these stale pixels, not a program that had run. */
    lv_obj_invalidate(scr);

    diag("start");

    /* The self test. Everything about this program being usable rests on a key
     * press reaching a shell, and a key press needs a finger on glass, which
     * is not something that can be asked for on demand. But the finger is the
     * only part that cannot be simulated here: the keyboard's entire delivery
     * path is "call lv_textarea_add_text on the object I was given", so setting
     * that object's text and then sending the event the Enter key sends runs
     * every line of the real path except the button press that starts it.
     *
     * It then looks for the shell's own answer on the screen. A test that only
     * checks its own write() returned would pass against a shell that never
     * ran, which is precisely the class of bug that was just fixed.
     *
     * LVTERM_SELFTEST=1 enables it. It is off otherwise, because typing a line
     * nobody asked for into a terminal someone is reading would be worse than
     * useless. */
    int selftest = getenv("LVTERM_SELFTEST") ? LVST_UNAME : LVST_NONE;
    if(selftest != LVST_NONE) {
        printf("selftest: stage 1, submitting 'uname -s' through the input "
               "path\n");
        fflush(stdout);
        selftest_submit("uname -s");
    }
    uint32_t selftest_at = now_ms();
    int stage1_ok = 0;   /* remembered until the final verdict */

    while(!want_quit) {
        struct pollfd p = { .fd = pty_fd, .events = POLLIN };
        int rc = poll(&p, 1, 20);
        if(rc > 0 && (p.revents & POLLIN)) {
            char buf[4096];
            for(;;) {
                ssize_t n = read(pty_fd, buf, sizeof buf);
                if(n > 0) { feed_terminal(buf, (int)n); continue; }
                if(n < 0 && (errno == EAGAIN || errno == EINTR)) break;
                break;
            }
        }
        if(shell_pid > 0 && waitpid(shell_pid, NULL, WNOHANG) == shell_pid)
            shell_pid = 0;

        /* Before the handler, not after. The handler is what calls the read
         * callback, so the phase has to be set before it runs: pumping
         * afterwards advances the phase between two reads and LVGL only ever
         * ever sees the released state. That produced a log full of confident
         * "press at 540 960" lines that were never pressed. */
        vtap_pump();

        uint32_t next = lv_timer_handler();
        usleep(next > 20 ? next * 1000 : 20000);

        /* A heartbeat line, so a log with nothing in it can be told apart from
         * a process that has wedged. Only when something was actually fed, plus
         * a slow one, to keep the log from filling on its own. */
        if(text_fed != last_reported || vtaps != last_vtaps
           || now_ms() - last_beat > 10000) {
            int had_out = text_fed != last_reported;
            int changed = had_out || vtaps != last_vtaps;
            last_reported = text_fed;
            last_vtaps = vtaps;
            last_beat = now_ms();
            diag("beat");
            /* Dumped on a tap as well as on output. A password test cannot be
             * settled by looking at what the shell said, only at what was drawn
             * where the user would have seen it. */
            if(changed) dump_screen(had_out ? "out" : "tap");
        }

        /* Give the shell a moment to answer, then say whether it did. Three
         * seconds is generous for uname on this hardware; a pass with a shorter
         * wait would only mean testing the machine rather than the code. */
        if(selftest == LVST_UNAME) {
            /* Poll for the answer rather than waiting a fixed interval, so a
             * slow shell cannot make a passing test fail. The deadline turns a
             * wedged shell into a fail instead of a hang. */
            int saw_u = screen_contains("uname -s");
            int saw_l = screen_contains("Linux");
            if((saw_u && saw_l) || now_ms() - selftest_at > 8000) {
                stage1_ok = saw_u && saw_l;
                printf("selftest: stage 1 %s  saw_uname=%d saw_linux=%d "
                       "(sent=%d lines=%d fed=%d)\n",
                       stage1_ok ? "PASS" : "FAIL",
                       saw_u, saw_l, chars_sent, lines_sent, text_fed);
                if(!stage1_ok) {
                    for(int r = 0; r < 5; r++) {
                        int n = screen[r].len;
                        char b[COLS + 1];
                        if(n > COLS) n = COLS;
                        memcpy(b, screen[r].cell, (size_t)n);
                        b[n] = '\0';
                        printf("selftest:   row %d len %d: '%s'\n", r, n, b);
                    }
                }

                /* Stage 2. The masking is the one part of the input path that
                 * stage one does not touch, and it is the part with a
                 * consequence outside this program: a password prompt that is
                 * not recognised leaves the password on the screen in the
                 * clear. It cannot be reached by tapping on this system,
                 * because su and login are both absent, so the shell is asked
                 * to print a prompt instead. That is the honest version of the
                 * test: it exercises the detection and the switch that a real
                 * sudo prompt would trigger, and not sudo itself.
                 *
                 * The read is what makes it work. A bare printf returns at
                 * once, so the shell prints its own prompt over the end of the
                 * password one and the line then ends in '#' rather than ':'.
                 * That is the detection behaving correctly on a line that is
                 * genuinely not a password prompt any more, and the first
                 * version of this stage failed for exactly that reason. */
                printf("selftest: stage 2, submitting a password prompt\n");
                fflush(stdout);
                selftest_submit("printf 'Password: '; read pw");
                selftest = LVST_MASK;
                selftest_at = now_ms();
            }
        }
        else if(selftest == LVST_MASK) {
            if(masked || now_ms() - selftest_at > 4000) {
                printf("selftest: stage 2 %s  masked=%d, the input line hides "
                       "what is typed into it\n",
                       masked ? "PASS" : "FAIL", (int)masked);
                printf("selftest: %s\n",
                       (stage1_ok && masked) ? "ALL PASS" : "SOMETHING FAILED");
                fflush(stdout);
                selftest = LVST_NONE;
            }
        }
    }

    /* The panel first, then the console. Handing the console back while the
     * backlight is still 0 would put a login prompt on a black screen, and the
     * local.d autostart script starts this program at the end of boot, where
     * a clean exit is how it hands the phone back to the console login. */
    if(state_off) {
        backlight_write(saved_brightness > 0 ? saved_brightness : BL_FALLBACK);
        saved_brightness = -1;
        state_off = 0;
    }
    console_write_mode(KD_TEXT);   /* let the kernel console have the screen back */
    if(shell_pid > 0) kill(shell_pid, SIGTERM);
    if(pty_fd >= 0) close(pty_fd);

    /* Say why. A process that vanishes from a log is a process that has cost an
     * afternoon, and this line is the difference between "it died" and "it was
     * sent signal 2", which are very different bugs. */
    printf("lvterm: exiting, signal=%d  sent=%d lines=%d intr=%d vtaps=%d "
           "fed=%d\n",
           quit_signal, chars_sent, lines_sent, interrupts, vtaps, text_fed);
    fflush(stdout);
    return 0;
}
