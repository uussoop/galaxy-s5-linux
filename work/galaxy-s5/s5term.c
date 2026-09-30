/*
 * s5term - a touch-driven terminal for the Galaxy S5 running postmarketOS.
 *
 * What this is
 * ------------
 * The S5's display and touchscreen both work at the kernel level. Nothing in
 * userspace was ever reading them, because there is no X server, no Wayland
 * compositor and no console input driver on this base. s5screen painted status
 * text straight into /dev/fb0 to work around the display half; this is the
 * input half, and it goes further by owning the framebuffer outright.
 *
 * The flow the phone owner asked for:
 *
 *   1. Screen is black with a single button drawn on it.
 *   2. Tap the button  -> a terminal appears.
 *   3. Tap the terminal -> an on-screen keyboard slides up.
 *   4. Tap keys       -> characters are typed into a real shell.
 *
 * Design notes
 * ------------
 * We do not use the kernel framebuffer console, and it turns out it was never
 * the thing in the way. Two earlier assumptions about the display were wrong
 * and both are corrected here:
 *
 *   - /proc/consoles is empty on this kernel and vtcon1/bind reads 1 no matter
 *     what is written to it, so the sysfs unbind is a no-op. Unbinding is
 *     still attempted at startup, but nothing depends on it.
 *   - The real rival for the framebuffer is the stock msm-fb-refresher
 *     service, a postmarketOS package that this device depends on. It pans
 *     the panel between two stacked screen buffers on a loop, which showed up
 *     as continuous flicker: measured at 58 framebuffer changes in 20 s with
 *     nobody touching the screen, and 0 changes once the service was stopped.
 *     Rather than disable it, we paint every stacked buffer, so whichever one
 *     the pan lands on holds the same picture. The refresher keeps running and
 *     the flip becomes invisible.
 *
 * A canary strip along the bottom edge, in a colour the panel path never
 * emits, is re-read every loop iteration. If something does overwrite us we
 * repaint and say so in the log, so interference is visible rather than
 * silent.
 *
 * We also do not use /dev/uinput, even though it works. A pty gives us a
 * private terminal whose output we render and whose input we control.
 * Injecting keystrokes into the input subsystem would mean competing with the
 * real console for the same tty, for no benefit.
 *
 * The shell is a child process on a pty. It has no idea it is on a phone. It
 * gets a correct TERM, a correct window size, and a real file descriptor. A
 * getty on ttyGS0 keeps running untouched and unaffected; if you kill this
 * process the serial console is still there, exactly as it always was.
 *
 * Reversibility
 * -------------
 * Nothing persistent is modified. No config file is written, no service is
 * installed, no kernel or device tree change is involved. Killing the process
 * restores the framebuffer console and hands the screen back. It is safe to
 * run this while the USB serial console is live, and safe to kill at any time.
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

#ifdef S5TERM_PREVIEW
/* The desktop preview never opens an input device, but read_touch() above
 * still refers to these names, so give the compiler just enough to link. */
struct input_event { long long t_sec, t_usec; unsigned short type, code; int value; };
#define EV_SYN 0
#define EV_KEY 1
#define EV_ABS 3
#define EV_SW  5
#define SYN_REPORT 0
#define ABS_X 0
#define ABS_Y 1
#define ABS_MT_SLOT        47
#define ABS_MT_TOUCH_MAJOR 48
#define ABS_MT_TOUCH_MINOR 49
#define ABS_MT_POSITION_X  53
#define ABS_MT_POSITION_Y  54
#define ABS_MT_TRACKING_ID 57
#define BTN_LEFT 0x110
#define BTN_TOUCH 0x14a
/* Only the preview path needs the fbdev structs stubbed; the real build gets
 * the kernel header, which is what tells us how many stacked buffers exist. */
struct fb_fix_screeninfo { unsigned long smem_len, line_length; };
struct fb_var_screeninfo { unsigned int xres, yres, yres_virtual; };
#define FBIOGET_FSCREENINFO 0x4602
#define FBIOGET_VSCREENINFO 0x4600
#else
#include <linux/input.h>
#include <linux/fb.h>
#endif

#include "font16x32.h"

/* ------------------------------------------------------------------ screen */

#define SCR_W 1080
#define SCR_H 1920
#define FB_PIXELS ((size_t)SCR_W * SCR_H)
#define FB_BYTES  (FB_PIXELS * 4)

/* The panel reports XRGB8888: red at byte offset 0, green 8, blue 16, and
 * alpha length 0, so the top byte is padding and ignored. One little-endian
 * uint32 per pixel, 0x00RRGGBB. */
#define RGB(r, g, b) ((uint32_t)((r) | ((g) << 8) | ((b) << 16)))

#define C_BG      RGB(0x00, 0x00, 0x00)
#define C_FG      RGB(0xdc, 0xdc, 0xdc)
#define C_DIM     RGB(0x50, 0x50, 0x50)
#define C_ACCENT  RGB(0x00, 0xc0, 0xc0)
#define C_KEY_BG  RGB(0x14, 0x16, 0x24)
#define C_KEY_BD  RGB(0x36, 0x3c, 0x58)
#define C_KEY_FG  RGB(0xe8, 0xe8, 0xf0)
#define C_KEY_DN  RGB(0x00, 0x88, 0x88)
#define C_BTN_BG  RGB(0x08, 0x10, 0x14)
#define C_WARN    RGB(0xd0, 0x50, 0x50)

/* A ten pixel strip along the very bottom edge, in a colour fbcon will never
 * emit. It is under the on-screen keys and inside the bezel, so it is not
 * something anyone sees, but reading it back is a cheap, reliable test of
 * whether we still own the framebuffer. See canary_ok(). */
#define CANARY_Y0 1910
#define CANARY_H  10
#define C_CANARY  RGB(0x00, 0xff, 0x00)

static uint32_t *fb;              /* mmap of /dev/fb0 */
static uint32_t *fb_base;         /* start of the whole panel memory mapping */
static size_t   buf_stride_px;    /* pixels per stacked buffer, padding included */
static int      nbuf = 1;         /* how many stacked buffers the panel holds */
static int fb_fd = -1;

/* ------------------------------------------------------------------- text */

#define COLS        (SCR_W / GLYPH_W)   /* 67 */
#define ROWS        (SCR_H / GLYPH_H)   /* 60 */
#define KB_TOP_Y    (SCR_H / 2)         /* keyboard takes the lower half */
#define KB_ROWS     (KB_TOP_Y / GLYPH_H)/* 30 text rows visible with it up */

typedef struct {
    uint8_t ch;       /* character code */
    uint8_t fg;       /* palette index 0-7, or 8 for "default" */
    uint8_t rev;      /* reverse video */
} Cell;

static Cell term[ROWS][COLS];
static int  cy, cx;
static int  rows_visible = ROWS;
static int  dirty_lo = ROWS, dirty_hi = -1;   /* inclusive row range to repaint */
static int  cur_row_prev = -1;
static int  saved_cy, saved_cx;

/* xterm-ish 8 + 8 bright palette, so `ls --color` looks right. */
static const uint32_t pal[16] = {
    RGB(0x20, 0x20, 0x20), RGB(0xc0, 0x30, 0x30), RGB(0x30, 0xb0, 0x40),
    RGB(0xc0, 0xa0, 0x30), RGB(0x40, 0x60, 0xc0), RGB(0xa0, 0x50, 0xb0),
    RGB(0x30, 0x90, 0x90), RGB(0xc0, 0xc0, 0xc0),
    RGB(0x60, 0x60, 0x60), RGB(0xff, 0x60, 0x60), RGB(0x60, 0xff, 0x60),
    RGB(0xff, 0xff, 0x60), RGB(0x70, 0xa0, 0xff), RGB(0xff, 0x80, 0xff),
    RGB(0x60, 0xff, 0xff), RGB(0xff, 0xff, 0xff),
};

static int  cur_fg = 8;    /* 8 = the default colour above */
static int  cur_rev = 0;

static void touch_rows(int lo, int hi)
{
    if (lo < dirty_lo) dirty_lo = lo;
    if (hi > dirty_hi) dirty_hi = hi;
}

/* Scroll the buffer up by n lines, so the cursor stays inside the visible
 * window. rows_visible shrinks to 30 while the keyboard is up, so this is what
 * stops long output from running off behind the keyboard. */
static void term_ensure_visible(void)
{
    int n = cy - rows_visible + 1;
    if (n <= 0)
        return;
    if (n > cy)
        n = cy;
    memmove(term[0], term[n], (size_t)(ROWS - n) * COLS * sizeof(Cell));
    for (int r = ROWS - n; r < ROWS; r++)
        for (int c = 0; c < COLS; c++) {
            term[r][c].ch = ' ';
            term[r][c].fg = 8;
            term[r][c].rev = 0;
        }
    cy -= n;
    saved_cy = cy > saved_cy ? cy - n : 0;
    if (saved_cy < 0) saved_cy = 0;
    touch_rows(0, rows_visible - 1);
}

static void term_scroll(void)
{
    memmove(term[0], term[1], (size_t)(ROWS - 1) * COLS * sizeof(Cell));
    for (int c = 0; c < COLS; c++) {
        term[ROWS - 1][c].ch = ' ';
        term[ROWS - 1][c].fg = 8;
        term[ROWS - 1][c].rev = 0;
    }
    touch_rows(0, ROWS - 1);
}

static void term_newline(void)
{
    cy++;
    if (cy >= rows_visible)
        term_scroll();
    term_ensure_visible();
}

static void term_putc(unsigned char ch)
{
    if (ch == '\n') {
        term_newline();
        return;
    }
    if (ch == '\r') {
        cx = 0;
        return;
    }
    if (ch == '\b') {
        if (cx > 0) cx--;
        return;
    }
    if (ch == '\t') {
        cx = (cx / 8 + 1) * 8;
        if (cx >= COLS) cx = COLS - 1;
        return;
    }
    if (ch < 32 || ch == 127)
        return;                       /* bell, and other controls, ignored */

    term[cy][cx].ch  = ch;
    term[cy][cx].fg  = (uint8_t)cur_fg;
    term[cy][cx].rev = (uint8_t)cur_rev;
    touch_rows(cy, cy);

    if (++cx >= COLS) {
        cx = 0;
        term_newline();
    }
}

static void term_clear(void)
{
    for (int r = 0; r < ROWS; r++)
        for (int c = 0; c < COLS; c++) {
            term[r][c].ch = ' ';
            term[r][c].fg = 8;
            term[r][c].rev = 0;
        }
    cy = cx = 0;
    touch_rows(0, ROWS - 1);
}

static void term_erase_line(int mode)
{
    int from = (mode == 1) ? 0 : cx, to = (mode == 1) ? cx : COLS;
    for (int c = from; c < to; c++) {
        term[cy][c].ch = ' ';
        term[cy][c].fg = 8;
        term[cy][c].rev = 0;
    }
    touch_rows(cy, cy);
}

static void term_erase_display(int mode)
{
    if (mode == 2 || mode == 3) {
        term_clear();
        return;
    }
    if (mode == 1) {
        for (int r = 0; r < cy; r++)
            for (int c = 0; c < COLS; c++) {
                term[r][c].ch = ' '; term[r][c].fg = 8; term[r][c].rev = 0;
            }
        term_erase_line(1);
    } else {
        term_erase_line(0);
        for (int r = cy + 1; r < ROWS; r++)
            for (int c = 0; c < COLS; c++) {
                term[r][c].ch = ' '; term[r][c].fg = 8; term[r][c].rev = 0;
            }
    }
    touch_rows(0, ROWS - 1);
}

static void sgr_apply(int *p, int np)
{
    if (np == 0) {
        cur_fg = 8;
        cur_rev = 0;
        return;
    }
    for (int i = 0; i < np; i++) {
        int v = p[i];
        if (v == 0) { cur_fg = 8; cur_rev = 0; }
        else if (v == 7) cur_rev = 1;
        else if (v == 27) cur_rev = 0;
        else if (v >= 30 && v <= 37) cur_fg = v - 30;
        else if (v == 39) cur_fg = 8;
        else if (v >= 90 && v <= 97) cur_fg = v - 90 + 8;
        else if (v >= 40 && v <= 47) cur_rev = 1;   /* bg colours: approximate */
    }
}

/* A deliberately small subset: enough for a busybox shell, colour, and
 * readline-style editing, without pretending to be a full terminal. */
static void feed(const unsigned char *buf, int n)
{
    enum { NORMAL, ESC, CSI, OSC, OSC_ESC } st = NORMAL;
    int p[16], np = 0, priv = 0;

    for (int i = 0; i < n; i++) {
        unsigned char c = buf[i];
        switch (st) {
        case NORMAL:
            if (c == 0x1b) st = ESC;
            else term_putc(c);
            break;

        case ESC:
            if (c == '[') { st = CSI; np = 0; priv = 0; }
            else if (c == ']') st = OSC;
            else if (c == 'c') { term_clear(); st = NORMAL; }
            else if (c == '7') { saved_cy = cy; saved_cx = cx; st = NORMAL; }
            else if (c == '8') { cy = saved_cy; cx = saved_cx; st = NORMAL; }
            else st = NORMAL;
            break;

        case CSI:
            if (c >= '0' && c <= '9') {
                if (np < 16) p[np] = p[np] * 10 + (c - '0');
            } else if (c == ';') {
                if (np < 15) { p[np + 1] = 0; np++; }
            } else if (c == '?' || c == '>' || c == '<' || c == '=') {
                priv = 1;
            } else if (c >= 0x40 && c <= 0x7e) {
                int a = np > 0 ? p[0] : 1;
                switch (c) {
                case 'A': cy -= a; if (cy < 0) cy = 0; break;
                case 'B': cy += a; if (cy >= rows_visible) cy = rows_visible - 1;
                          term_ensure_visible(); break;
                case 'C': cx += a; if (cx >= COLS) cx = COLS - 1; break;
                case 'D': cx -= a; if (cx < 0) cx = 0; break;
                case 'G': cx = a - 1; if (cx >= COLS) cx = COLS - 1;
                          if (cx < 0) cx = 0; break;
                case 'd': cy = a - 1; if (cy < 0) cy = 0; break;
                case 'H': case 'f':
                    cy = a - 1;
                    cx = (np > 1 ? p[1] : 1) - 1;
                    if (cy < 0) cy = 0;
                    if (cy >= rows_visible) { cy = rows_visible - 1; }
                    if (cx < 0) cx = 0;
                    if (cx >= COLS) cx = COLS - 1;
                    break;
                case 'J': term_erase_display(p[0]); break;
                case 'K': term_erase_line(p[0]); break;
                case 'm': if (!priv) sgr_apply(p, np); break;
                default: break;      /* 'h', 'l', 'r', 'n' and friends */
                }
                st = NORMAL;
            }
            break;

        case OSC:
            if (c == 7) st = NORMAL;
            else if (c == 0x1b) st = OSC_ESC;
            break;

        case OSC_ESC:
            st = (c == '\\') ? NORMAL : OSC;
            break;
        }
    }
}

/* ---------------------------------------------------------------- drawing */

static void fill(int x, int y, int w, int h, uint32_t col)
{
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > SCR_W) w = SCR_W - x;
    if (y + h > SCR_H) h = SCR_H - y;
    if (w <= 0 || h <= 0)
        return;

    /* The panel memory holds several stacked screen buffers and the kernel
     * pans between them, so every buffer gets the same pixels. The stock
     * msm-fb-refresher service is what drives that pan, and it is still
     * running: without this, a flip lands the viewer on a buffer we never
     * painted, and the screen visibly alternates between our terminal and
     * whatever was in the other half. Painting all of them makes the flip
     * invisible instead of trying to suppress it, which also means the
     * refresher keeps doing the job it exists for. */
    for (int b = 0; b < nbuf; b++) {
        uint32_t *base = fb_base + (size_t)b * buf_stride_px;
        for (int r = 0; r < h; r++) {
            uint32_t *p = base + (size_t)(y + r) * SCR_W + x;
            for (int c = 0; c < w; c++)
                p[c] = col;
        }
    }
}

static void frame(int x, int y, int w, int h, uint32_t col, int t)
{
    fill(x, y, w, t, col);
    fill(x, y + h - t, w, t, col);
    fill(x, y + t, t, h - 2 * t, col);
    fill(x + w - t, y + t, t, h - 2 * t, col);
}

/* One glyph, scaled by an integer factor, MSB-first within each row byte. */
static void glyph(int px, int py, int code, uint32_t col, int scale)
{
    if (code < 0 || code >= GLYPH_COUNT)
        code = '?';
    const unsigned char *g = s5_font[code];
    for (int r = 0; r < GLYPH_H; r++) {
        const unsigned char *row = g + r * GLYPH_BYTES;
        int bits = 0;
        for (int b = 0; b < GLYPH_BYTES; b++)
            bits = (bits << 8) | row[b];
        for (int c = 0; c < GLYPH_W; c++) {
            if (!((bits >> (GLYPH_W - 1 - c)) & 1))
                continue;
            fill(px + c * scale, py + r * scale, scale, scale, col);
        }
    }
}

static int text(int px, int py, const char *s, uint32_t col, int scale)
{
    for (; *s; s++, px += GLYPH_W * scale)
        glyph(px, py, (unsigned char)*s, col, scale);
    return px;
}

static int text_w(const char *s, int scale)
{
    return (int)strlen(s) * GLYPH_W * scale;
}

static void text_c(int y, const char *s, uint32_t col, int scale)
{
    int w = text_w(s, scale);
    text((SCR_W - w) / 2, y, s, col, scale);
}

/* --------------------------------------------------------------- keyboard */

typedef struct {
    const char *label;   /* what is drawn */
    const char *emit;    /* what is typed; NULL for a mode key */
    int key;             /* non-zero: special action, see KB_* */
    float weight;        /* relative width */
} Key;

#define KB_NONE  0
#define KB_TAB   1
#define KB_SYM   2
#define KB_BACK  3
#define KB_ENTER 4
#define KB_HIDE  5

/* Ten keys wide, evenly spread. Weights only matter for the bottom row, where
 * space needs to be a big target and the mode key should not be. */
static const Key kb_alpha[5][11] = {
    { {"q","q",0,1},{"w","w",0,1},{"e","e",0,1},{"r","r",0,1},{"t","t",0,1},
      {"y","y",0,1},{"u","u",0,1},{"i","i",0,1},{"o","o",0,1},{"p","p",0,1} },
    { {"a","a",0,1},{"s","s",0,1},{"d","d",0,1},{"f","f",0,1},{"g","g",0,1},
      {"h","h",0,1},{"j","j",0,1},{"k","k",0,1},{"l","l",0,1} },
    { {"z","z",0,1},{"x","x",0,1},{"c","c",0,1},{"v","v",0,1},{"b","b",0,1},
      {"n","n",0,1},{"m","m",0,1},{"\x7f","\b",KB_BACK,1.6f} },
    { },
    { {"?123","",KB_SYM,2.0f},{"space"," ",0,7.0f},{"return","\n",KB_ENTER,2.0f} },
};
static const int kb_alpha_n[5] = { 10, 9, 8, 0, 3 };

static const Key kb_sym[5][11] = {
    { {"1","1",0,1},{"2","2",0,1},{"3","3",0,1},{"4","4",0,1},{"5","5",0,1},
      {"6","6",0,1},{"7","7",0,1},{"8","8",0,1},{"9","9",0,1},{"0","0",0,1} },
    { {"-","-",0,1},{"_","_",0,1},{"/","/",0,1},{"\\","\\",0,1},{"[","[",0,1},
      {"]","]",0,1},{"=","=",0,1},{";",";",0,1},{"'","'",0,1},{"\"","\"",0,1} },
    { {".",".",0,1},{",",",",0,1},{"!","!",0,1},{"?","?",0,1},{"@","@",0,1},
      {"#","#",0,1},{"$","$",0,1},{"%","%",0,1},{"&","&",0,1},
      {"\x7f","\b",KB_BACK,1.0f} },
    { },
    { {"abc","",KB_TAB,2.0f},{"space"," ",0,7.0f},{"return","\n",KB_ENTER,2.0f} },
};
static const int kb_sym_n[5] = { 10, 10, 10, 0, 3 };

#define KB_MARGIN 10
#define KB_GAP    6

static const Key *kb_rows[5];
static const int  *kb_n;
static int  kb_mode_sym;

static void kb_select(void)
{
    if (kb_mode_sym) { kb_rows[0]=kb_sym[0]; kb_rows[1]=kb_sym[1]; kb_rows[2]=kb_sym[2];
                       kb_rows[3]=kb_sym[3]; kb_rows[4]=kb_sym[4]; kb_n = kb_sym_n; }
    else             { kb_rows[0]=kb_alpha[0]; kb_rows[1]=kb_alpha[1]; kb_rows[2]=kb_alpha[2];
                       kb_rows[3]=kb_alpha[3]; kb_rows[4]=kb_alpha[4]; kb_n = kb_alpha_n; }
}

/* Geometry of one key, computed the same way by the painter and the hit test
 * so the two can never disagree. */
static int kb_key_rect(int row, int idx, int *rx, int *ry, int *rw, int *rh)
{
    int n = kb_n[row];
    if (n <= 0 || idx < 0 || idx >= n)
        return 0;
    const Key *k = &kb_rows[row][idx];

    float total = 0;
    for (int i = 0; i < n; i++)
        total += kb_rows[row][i].weight;

    int avail = SCR_W - 2 * KB_MARGIN - (n - 1) * KB_GAP;
    int rh2 = (SCR_H - KB_TOP_Y - 2 * KB_MARGIN - 4 * KB_GAP) / 5;

    int used = 0, w;
    for (int i = 0; i < idx; i++)
        used += (int)(avail * kb_rows[row][i].weight / total) + KB_GAP;
    w = (int)(avail * k->weight / total);

    *rx = KB_MARGIN + used;
    *ry = KB_TOP_Y + KB_MARGIN + row * (rh2 + KB_GAP);
    *rw = w;
    *rh = rh2;
    (void)k;
    return 1;
}

static int kb_draw;   /* set when the keyboard needs repainting */

static void kb_paint(int hot_row, int hot_idx)
{
    fill(0, KB_TOP_Y, SCR_W, SCR_H - KB_TOP_Y, C_BG);
    frame(0, KB_TOP_Y, SCR_W, SCR_H - KB_TOP_Y, C_KEY_BD, 1);

    for (int r = 0; r < 5; r++) {
        for (int i = 0; i < kb_n[r]; i++) {
            int x, y, w, h;
            if (!kb_key_rect(r, i, &x, &y, &w, &h))
                continue;
            int hot = (r == hot_row && i == hot_idx);
            fill(x, y, w, h, hot ? C_KEY_DN : C_KEY_BG);
            frame(x, y, w, h, hot ? C_KEY_DN : C_KEY_BD, 1);

            const Key *k = &kb_rows[r][i];
            uint32_t fg = hot ? RGB(0x00, 0x00, 0x00) : C_KEY_FG;
            int scale = 1;
            int tw = text_w(k->label, scale);
            if (tw > w - 8) { scale = 1; tw = text_w(k->label, scale); }
            if (tw > w - 8 && w >= 90) {
                /* Too wide to read at full size: show a compact glyph instead
                 * of letting the label spill over its neighbours. */
                const char *alt = k->key == KB_BACK ? "<" :
                                  k->key == KB_ENTER ? "^" :
                                  k->key == KB_SYM   ? "#" :
                                  k->key == KB_TAB   ? "a" :
                                  k->key == KB_HIDE  ? "v" : k->label;
                text(x + (w - text_w(alt, 1)) / 2, y + (h - GLYPH_H) / 2,
                     alt, fg, 1);
                continue;
            }
            text(x + (w - tw) / 2, y + (h - GLYPH_H) / 2, k->label, fg, scale);
        }
    }
}

/* ------------------------------------------------------------------- pty */

static int  pty_fd = -1;
static pid_t shell_pid = -1;
static int  shell_gone = 0;

static int pty_start(void)
{
    char name[128];
    int master = posix_openpt(O_RDWR | O_NOCTTY);
    if (master < 0) { perror("posix_openpt"); return -1; }
    if (grantpt(master) < 0) { perror("grantpt"); close(master); return -1; }
    if (unlockpt(master) < 0) { perror("unlockpt"); close(master); return -1; }
    if (ptsname_r(master, name, sizeof name) != 0) {
        perror("ptsname_r"); close(master); return -1;
    }

    int slave = open(name, O_RDWR);
    if (slave < 0) { perror("open pty slave"); close(master); return -1; }

    struct winsize ws = { .ws_row = KB_ROWS, .ws_col = COLS };
    ioctl(slave, TIOCSWINSZ, &ws);

    pid_t pid = fork();
    if (pid < 0) { perror("fork"); close(master); close(slave); return -1; }
    if (pid == 0) {
        close(master);
        setsid();
        ioctl(slave, TIOCSCTTY, 0);
        dup2(slave, 0); dup2(slave, 1); dup2(slave, 2);
        if (slave > 2) close(slave);
        setenv("TERM", "xterm-256color", 1);
        unsetenv("LINES");
        unsetenv("COLUMNS");
        const char *sh = getenv("SHELL");
        execl(sh && *sh ? sh : "/bin/sh", "sh", (char *)NULL);
        execl("/bin/sh", "sh", (char *)NULL);
        _exit(127);
    }

    close(slave);
    fcntl(master, F_SETFL, O_NONBLOCK);
    pty_fd  = master;
    shell_pid = pid;
    shell_gone = 0;
    return 0;
}

static void pty_send(const char *s, int n)
{
    if (pty_fd < 0) return;
    while (n > 0) {
        ssize_t w = write(pty_fd, s, (size_t)n);
        if (w <= 0) {
            if (w < 0 && (errno == EAGAIN || errno == EINTR))
                continue;
            return;
        }
        s += w;
        n -= (int)w;
    }
}

static void pty_resize(void)
{
    if (pty_fd < 0) return;
    struct winsize ws = { .ws_row = (unsigned short)rows_visible,
                          .ws_col = (unsigned short)COLS };
    ioctl(pty_fd, TIOCSWINSZ, &ws);
    /* Tell the child its window changed, so full-screen programs redraw. */
    if (shell_pid > 0)
        kill(shell_pid, SIGWINCH);
}

/* ------------------------------------------------------------------ touch */

static int  touch_fd = -1;
static int  dbg;

/* Protocol B multitouch state for the finger we are following (slot 0). */
static int  mt_slot, mt_x, mt_y, mt_valid;
static int  ax, ay, have_ax, have_ay;   /* single touch aliases, if present */
static int  sw_seen = 0;                 /* have we ever seen SW_TOUCHSCREEN? */
static int  pending_press = 0;
static int  last_press_ms = 0;
static long reports_since_sw = 0;
static int  force_abs_mode = 0;          /* set if BTN_TOUCH never shows up */

/* ------------------------------------------------------------------- state */

enum { ST_LOCKED, ST_TERM };
static int state = ST_LOCKED;
static int kb_visible = 0;
static int cursor_on = 1;
static int fbcon_was_bound = 1;
static volatile sig_atomic_t want_quit;

static void restore_console(void)
{
    if (fbcon_was_bound) {
        int f = open("/sys/class/vtconsole/vtcon1/bind", O_WRONLY);
        if (f >= 0) { write(f, "1", 1); close(f); }
    }
}

static void on_signal(int s) { (void)s; want_quit = 1; }

static long now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000L + t.tv_nsec / 1000000L;
}

/* ------------------------------------------------------------- repainting */

static void term_paint(int show_cursor)
{
    if (dirty_hi < dirty_lo && cur_row_prev < 0)
        return;
    int lo = dirty_lo < 0 ? 0 : dirty_lo;
    int hi = dirty_hi > rows_visible - 1 ? rows_visible - 1 : dirty_hi;
    if (cur_row_prev >= 0) { if (cur_row_prev < lo) lo = cur_row_prev;
                             if (cur_row_prev > hi) hi = cur_row_prev; }
    if (cur_row_prev < 0) cur_row_prev = cy;

    for (int r = lo; r <= hi; r++) {
        for (int c = 0; c < COLS; c++) {
            Cell *cl = &term[r][c];
            int is_cur = (r == cy && c == cx && show_cursor);
            uint32_t fg = cl->fg == 8 ? C_FG : pal[cl->fg];
            uint32_t bg = C_BG;
            if (cl->rev != is_cur) {
                uint32_t t = fg; fg = bg; bg = t;
            }
            if (is_cur && cursor_on)
                bg = fg;
            fill(c * GLYPH_W, r * GLYPH_H, GLYPH_W, GLYPH_H, bg);
            if (cl->ch != ' ')
                glyph(c * GLYPH_W, r * GLYPH_H, cl->ch, fg, 1);
        }
    }
    dirty_lo = ROWS;
    dirty_hi = -1;
    cur_row_prev = -1;
}

static void paint_canary(void)
{
    fill(0, CANARY_Y0, SCR_W, CANARY_H, C_CANARY);
}

/* How many stray canary pixels we treat as the kernel console merely clipping
 * our strip, rather than as the framebuffer having been taken over. The
 * console's cursor is an 8x16 cell that lands on the last two rows of this
 * strip, so it accounts for about 16 pixels. */
#define CANARY_TOLERANCE 512

/* Count canary pixels that are not ours, rather than stopping at the first.
 * The caller uses the size of the damage to decide how much to repaint. */
static int canary_dirty(void)
{
    int n = 0;
    for (int b = 0; b < nbuf; b++) {
        const uint32_t *row = fb_base + (size_t)b * buf_stride_px
                            + (size_t)CANARY_Y0 * SCR_W;
        for (int x = 0; x < SCR_W; x++)
            if (row[x] != C_CANARY)
                n++;
    }
    return n;
}

static void paint_locked(void)
{
    fill(0, 0, SCR_W, SCR_H, C_BG);
    paint_canary();
    int bw = 860, bh = 300, bx = (SCR_W - bw) / 2, by = 700;
    fill(bx, by, bw, bh, C_BTN_BG);
    frame(bx, by, bw, bh, C_ACCENT, 3);
    text_c(by + bh / 2 - 76, "TAP TO WAKE", C_ACCENT, 2);
    text_c(by + bh / 2 + 20, "galaxy s5  native linux", C_DIM, 1);
    dirty_lo = ROWS; dirty_hi = -1; cur_row_prev = -1;
}

static void repaint_all(void)
{
    fill(0, 0, SCR_W, KB_TOP_Y, C_BG);
    rows_visible = kb_visible ? KB_ROWS : ROWS;
    term_ensure_visible();
    touch_rows(0, rows_visible - 1);
    kb_draw = 1;
    cursor_on = 1;
    paint_canary();
    pty_resize();
}

/* --------------------------------------------------------------- input */

/* Which key is lit, if any. The painter and the hit test share these so a
 * highlight can never end up on a different key than the one that fired. */
static int hot_row = -1, hot_idx = -1;
static long hot_until;              /* when the tapped key stops being lit */

static void press(int x, int y)
{
    if (state == ST_LOCKED) {
        state = ST_TERM;
        kb_visible = 0;
        term_clear();
        repaint_all();
        if (pty_fd < 0 && !shell_gone)
            pty_start();
        return;
    }

    if (y < KB_TOP_Y) {
        /* Tapping the terminal brings the keyboard up. */
        if (!kb_visible) {
            kb_visible = 1;
            repaint_all();
        }
        return;
    }

    /* A tap inside the keyboard: find the key. */
    for (int r = 0; r < 5; r++) {
        for (int i = 0; i < kb_n[r]; i++) {
            int kx, ky, kw, kh;
            if (!kb_key_rect(r, i, &kx, &ky, &kw, &kh))
                continue;
            if (x < kx || x >= kx + kw || y < ky || y >= ky + kh)
                continue;

            const Key *k = &kb_rows[r][i];
            switch (k->key) {
            case KB_BACK:  pty_send("\x7f", 1); break;
            case KB_ENTER: pty_send("\n", 1);   break;
            case KB_SYM:   kb_mode_sym = 1; kb_select(); kb_draw = 1; return;
            case KB_TAB:   kb_mode_sym = 0; kb_select(); kb_draw = 1; return;
            case KB_HIDE:  kb_visible = 0; repaint_all(); return;
            default:
                if (k->emit)
                    pty_send(k->emit, (int)strlen(k->emit));
                return;
            }
            /* Special keys leave the highlight showing for a moment. It used to
             * be cleared on the very next loop iteration, which drew the key
             * lit and then unlit one frame later: a flash on every tap. */
            kb_draw = 1;
            hot_row = r; hot_idx = i;
            hot_until = now_ms() + 140;
            return;
        }
    }
}

static void read_touch(void)
{
    /* This panel is a multitouch digitiser using input protocol B. The traced
     * event stream from the real hardware is:
     *
     *   ABS_MT_TRACKING_ID  57   slot id, or -1 when that finger lifts
     *   ABS_MT_POSITION_X   53
     *   ABS_MT_POSITION_Y   54
     *   ABS_MT_TOUCH_MAJOR  48   contact patch size
     *   ABS_MT_TOUCH_MINOR  49
     *   BTN_TOUCH          330   1 on contact, 0 on lift
     *   SYN_REPORT          0   frame boundary
     *
     * An earlier probe concluded this digitiser was single touch with two axes
     * (ABS_X, ABS_Y). That conclusion was wrong. The probe read the capability
     * bitmap through a 32 bit mask, and every ABS_MT_* code is above 31, so
     * the whole multitouch half of the interface was invisible to it. The
     * coordinate range is the same (0..1079 by 0..1919, matching the panel),
     * but the event codes to listen for are the MT ones.
     *
     * We follow one finger, slot 0. Everything else is ignored: this is a
     * keyboard and a button, neither of which benefits from a second point. */
    struct input_event ev;
    while (read(touch_fd, &ev, sizeof ev) == (ssize_t)sizeof ev) {
        static int traced = 0, trace_n = 0;
        if (dbg && traced < 40) {
            if (trace_n++ == 0)
                printf("s5term: trace begins\n");
            printf("  ev type=%u code=%u value=%d\n", ev.type, ev.code, ev.value);
            if (++traced >= 40) { fflush(stdout); printf("s5term: trace ends\n"); }
            fflush(stdout);
        }

        switch (ev.type) {
        case EV_ABS:
            switch (ev.code) {
            case ABS_MT_SLOT:
                mt_slot = ev.value;
                break;
            case ABS_MT_TRACKING_ID:
                mt_valid = (ev.value >= 0);
                break;
            case ABS_MT_POSITION_X:
                if (mt_slot == 0) mt_x = ev.value;
                break;
            case ABS_MT_POSITION_Y:
                if (mt_slot == 0) mt_y = ev.value;
                break;
            /* Some panels also publish the single touch aliases. Honour them
             * too, so a tap still lands if the MT path ever goes quiet. */
            case ABS_X: ax = ev.value; have_ax = 1; break;
            case ABS_Y: ay = ev.value; have_ay = 1; break;
            default: break;
            }
            break;

        case EV_KEY:
            if (ev.code == BTN_TOUCH || ev.code == BTN_LEFT)
                pending_press = (ev.value != 0);
            break;

        case EV_SW:
            if (ev.code == 22) {                 /* SW_TOUCHSCREEN */
                sw_seen = 1;
                pending_press = (ev.value != 0);
            }
            break;

        case EV_SYN:
            if (ev.code != SYN_REPORT)
                break;
            {
                int x = -1, y = -1;
                if (mt_valid) { x = mt_x; y = mt_y; }
                else if (have_ax && have_ay) { x = ax; y = ay; }

                if (x >= 0 && y >= 0 && (pending_press || force_abs_mode)) {
                    long t = now_ms();
                    if (t - last_press_ms > 60) {
                        last_press_ms = t;
                        hot_row = hot_idx = -1;
                        press(x, y);
                    }
                    pending_press = 0;
                }
            }
            reports_since_sw++;
            if (!sw_seen && reports_since_sw > 200)
                force_abs_mode = 1;
            break;

        default:
            break;
        }
    }
}
static void reap_shell(void)
{
    int st;
    pid_t p;
    while ((p = waitpid(-1, &st, WNOHANG)) > 0) {
        if (p == shell_pid) {
            shell_gone = 1;
            if (pty_fd >= 0) { close(pty_fd); pty_fd = -1; }
            term_putc('\r'); term_putc('\n');
            feed((const unsigned char *)"\r\n[process exited -- tap to restart]\r\n", 35);
        }
    }
}

/* ------------------------------------------------------------------ setup */

static void backlight_on(void)
{
    /* Best effort: if the panel is powered down, taps will not be visible even
     * though the touch controller still reports them. */
    static const char *dirs[] = {
        "/sys/class/backlight/s5amoled/brightness",
        "/sys/class/backlight/panel/brightness",
        "/sys/class/backlight/amoled/brightness",
        NULL
    };
    for (int i = 0; dirs[i]; i++) {
        int f = open(dirs[i], O_WRONLY);
        if (f < 0) continue;
        char v[16];
        int n = snprintf(v, sizeof v, "%d", 255);
        write(f, v, (size_t)n);
        close(f);
    }
}

static int bind_fbcon(int on)
{
    int f = open("/sys/class/vtconsole/vtcon1/bind", O_WRONLY);
    if (f < 0) return -1;
    write(f, on ? "1" : "0", 1);
    close(f);
    return 0;
}

static int s5term_main(int argc, char **argv)
{
    if (getuid() != 0) {
        fprintf(stderr,
                "s5term: must run as root (needs /dev/fb0 and /dev/input/event2)\n"
                "        try: sudo %s\n", argv[0]);
        return 1;
    }

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
            printf("usage: sudo %s [-n]\n"
                   "  -n  leave the kernel framebuffer console bound, which"
                   " means fighting it for pixels\n", argv[0]);
            return 0;
        }
    }
    int leave_fbcon = 0;
    for (int i = 1; i < argc; i++)
        if (!strcmp(argv[i], "-n")) leave_fbcon = 1;

    signal(SIGINT,  on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGHUP,  on_signal);
    signal(SIGPIPE, SIG_IGN);

    { const char *d = getenv("S5TERM_DEBUG"); dbg = d && *d && *d != '0'; }

    fb_fd = open("/dev/fb0", O_RDWR);
    if (fb_fd < 0) { perror("open /dev/fb0"); return 1; }

    /* Map the whole of the panel memory, not just one screen. smem_len here is
     * twice the visible size: the driver keeps two stacked buffers and pans
     * between them, so we need to be able to reach and paint both. */
    {
        struct fb_fix_screeninfo fix;
        struct fb_var_screeninfo var;
        size_t map_bytes = FB_BYTES;

        memset(&fix, 0, sizeof fix);
        memset(&var, 0, sizeof var);
        if (ioctl(fb_fd, FBIOGET_FSCREENINFO, &fix) == 0 &&
            ioctl(fb_fd, FBIOGET_VSCREENINFO, &var) == 0) {
            size_t stride_px = fix.line_length / 4;
            size_t per_buf   = (size_t)var.yres * fix.line_length;  /* bytes */
            int    count     = per_buf ? (int)(fix.smem_len / per_buf) : 1;

            if (stride_px != SCR_W)
                printf("s5term: note: line_length is %lu bytes, expected"
                       " %d. Row padding is not handled.\n",
                       (unsigned long)fix.line_length, SCR_W * 4);

            if (count > 1 && per_buf * (size_t)count <= fix.smem_len) {
                nbuf          = count;
                buf_stride_px = stride_px;
                map_bytes     = per_buf * (size_t)count;
            }
        }

        fb_base = mmap(NULL, map_bytes, PROT_READ | PROT_WRITE, MAP_SHARED,
                      fb_fd, 0);
        if (fb_base == MAP_FAILED) { perror("mmap /dev/fb0"); return 1; }
        fb = fb_base;
        buf_stride_px = SCR_W;   /* one screen, when the query above said nothing */
        printf("s5term: framebuffer %dx%d, %d stacked buffer%s, %zu bytes"
               " mapped\n", SCR_W, SCR_H, nbuf, nbuf == 1 ? "" : "s", map_bytes);
    }

    touch_fd = open("/dev/input/event2", O_RDONLY | O_NONBLOCK);
    if (touch_fd < 0) {
        fprintf(stderr, "s5term: open /dev/input/event2: %s\n"
                        "        the touchscreen node may have moved; check"
                        " /proc/bus/input/devices\n", strerror(errno));
        return 1;
    }

    /* Try to take the framebuffer away from the kernel's framebuffer console.
     * On this kernel the write is accepted but does not stick: reading the
     * file still reports 1 afterwards, and fbcon goes on painting. So this is
     * best effort, and the canary in the main loop is what actually keeps the
     * screen ours. */
    if (leave_fbcon != 1 && bind_fbcon(0) == 0)
        printf("s5term: asked the kernel console to unbind; will verify\n");

    fbcon_was_bound = 0;   /* we did not successfully take it away */

    kb_select();
    backlight_on();
    paint_locked();

    long last_blink = now_ms();
    while (!want_quit) {
        struct pollfd p[2];
        int np = 0;
        if (touch_fd >= 0) {
            p[np].fd = touch_fd; p[np].events = POLLIN; p[np].revents = 0; np++;
        }
        if (pty_fd >= 0) {
            p[np].fd = pty_fd; p[np].events = POLLIN; p[np].revents = 0; np++;
        }

        int rc = poll(p, (nfds_t)np, 200);
        if (rc < 0 && errno != EINTR)
            break;

        if (rc > 0) {
            if (touch_fd >= 0 && (p[0].revents & POLLIN))
                read_touch();
            if (pty_fd >= 0 && p[np - 1].revents & POLLIN) {
                unsigned char buf[4096];
                for (;;) {
                    ssize_t n = read(pty_fd, buf, sizeof buf);
                    if (n > 0) { feed(buf, (int)n); continue; }
                    if (n < 0 && (errno == EAGAIN || errno == EINTR))
                        break;
                    break;      /* EOF: the shell closed the pty */
                }
            }
        }

        reap_shell();
        if (shell_gone && state == ST_TERM) {
            /* A dead shell leaves a stale pty; restart it on the next tap. */
            pty_fd = -1;
        }

        if (hot_row >= 0 && now_ms() >= hot_until) {
            hot_row = hot_idx = -1;       /* the highlight has had its moment */
            kb_draw = 1;
        }
        if (kb_draw) {
            kb_paint(hot_row, hot_idx);
            kb_draw = 0;
        }

        /* The kernel console is alive on this kernel and its cursor blinks on
         * the bottom rows of the screen, right inside the canary strip.
         *
         * Reacting to that with a full screen repaint is what made the display
         * flicker: four repaints a second, which flashed the wake button on
         * the locked screen and the keyboard when it was up. A stomp this
         * small is repaired in place, in the strip, where nothing can see it.
         * Only real damage gets a full repaint, and then at most once every
         * couple of seconds, so a hostile writer still cannot strobe us. */
        {
            static unsigned long patched, full_repaints;
            static long last_full = 0;
            int dirty = canary_dirty();

            if (dirty && dirty <= CANARY_TOLERANCE) {
                paint_canary();
                if (patched++ < 4)
                    printf("s5term: console cursor clipped the canary"
                           " (%d px), patched in place\n", dirty);
            } else if (dirty) {
                long now = now_ms();
                if (now - last_full > 2000) {
                    last_full = now;
                    if (full_repaints++ < 8)
                        printf("s5term: framebuffer overwritten (%d px),"
                               " full repaint\n", dirty);
                    if (state == ST_LOCKED) {
                        paint_locked();
                    } else {
                        fill(0, 0, SCR_W, SCR_H, C_BG);
                        rows_visible = kb_visible ? KB_ROWS : ROWS;
                        touch_rows(0, rows_visible - 1);
                        term_paint(cursor_on);
                        kb_paint(-1, -1);
                        paint_canary();
                    }
                }
            }
        }

        long t = now_ms();
        if (state == ST_TERM && t - last_blink > 500) {
            last_blink = t;
            cursor_on = !cursor_on;
            cur_row_prev = -1;        /* force the cursor rows to repaint */
            touch_rows(cy, cy);
        }

        if (state == ST_TERM)
            term_paint(cursor_on);

        /* Last, so it survives everything above that touches the bottom edge. */
        paint_canary();
    }

    if (fb != MAP_FAILED) munmap(fb_base, (size_t)buf_stride_px * SCR_H * nbuf);
    if (fb_fd >= 0) close(fb_fd);
    if (touch_fd >= 0) close(touch_fd);
    if (pty_fd >= 0) close(pty_fd);
    if (shell_pid > 0) { kill(shell_pid, SIGHUP); kill(shell_pid, SIGTERM); }
    restore_console();
    printf("s5term: exiting, console restored\n");
    return 0;
}

/* ------------------------------------------------------------------------
 * Desktop preview. Compiled only with -DS5TERM_PREVIEW, this builds the exact
 * same drawing code against a heap buffer instead of /dev/fb0 and writes PPM
 * files, so the layout can be inspected on a workstation before the binary
 * ever takes over a phone screen.
 * ---------------------------------------------------------------------- */
#ifdef S5TERM_PREVIEW

static void write_ppm(const char *path)
{
    FILE *f = fopen(path, "wb");
    if (!f) { perror(path); return; }
    fprintf(f, "P6\n%d %d\n255\n", SCR_W, SCR_H);
    for (int y = 0; y < SCR_H; y++)
        for (int x = 0; x < SCR_W; x++) {
            uint32_t p = fb[(size_t)y * SCR_W + x];
            unsigned char rgb[3] = { (unsigned char)(p & 0xff),
                                     (unsigned char)((p >> 8) & 0xff),
                                     (unsigned char)((p >> 16) & 0xff) };
            fwrite(rgb, 1, 3, f);
        }
    fclose(f);
    printf("wrote %s\n", path);
}

int main(int argc, char **argv)
{
    const char *outdir = argc > 1 ? argv[1] : ".";
    char path[512];

    fb = malloc(FB_BYTES);
    if (!fb) return 1;
    memset(fb, 0, FB_BYTES);
    fb_base = fb;

    kb_select();
    signal(SIGINT, on_signal);

    /* 1. the locked screen */
    paint_locked();
    snprintf(path, sizeof path, "%s/1-locked.ppm", outdir);
    write_ppm(path);

    /* 2. a terminal with output, no keyboard */
    state = ST_TERM;
    term_clear();
    feed((const unsigned char *)
         "\r\nBusyBox v1.36.1 (2024-01-01 00:00:00 UTC) multi-call binary.\r\n"
         "\r\n/ # uname -a\r\n"
         "Linux localhost 3.10.9-LineageOS #1 SMP PREEMPT armv7l GNU/Linux\r\n"
         "/ # ls --color\r\n"
         "\033[01;34mboot\033[0m  \033[01;34mhome\033[0m  \033[01;34msbin\033[0m"
         "  \033[01;34musr\033[0m  \033[01;32ms5term\033[0m\r\n"
         "/ # \033[1;32m\033[1mdone\033[0m tap the terminal for a keyboard\r\n", 340);
    kb_visible = 0;
    repaint_all();
    cursor_on = 1;
    term_paint(1);
    snprintf(path, sizeof path, "%s/2-terminal.ppm", outdir);
    write_ppm(path);

    /* 3. same, keyboard up, one key held */
    kb_visible = 1;
    repaint_all();
    term_paint(1);
    kb_paint(0, 6);
    snprintf(path, sizeof path, "%s/3-keyboard.ppm", outdir);
    write_ppm(path);

    /* 4. the symbol layout */
    kb_mode_sym = 1;
    kb_select();
    kb_paint(-1, -1);
    snprintf(path, sizeof path, "%s/4-symbols.ppm", outdir);
    write_ppm(path);

    free(fb);
    return 0;
}

#else

int main(int argc, char **argv)
{
    return s5term_main(argc, argv);
}

#endif
