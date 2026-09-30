/*
 * fbpeek - print raw framebuffer pixel values, and render the framebuffer as
 *          text.
 *
 * fbdump classifies pixels into named buckets so a screenshot can be checked at
 * a glance. That is the right tool for "is there content" and the wrong one for
 * "what exactly is in there": it compares exact 0x00RRGGBB values, so a program
 * that writes the same colours with a non zero alpha byte comes back as 100%
 * "other", and it cannot say where anything is on the screen.
 *
 * This does two things instead. It names the actual pixel values, and it draws
 * the screen as ASCII art. The art matters more than it sounds: there is no way
 * to look at this phone's screen, so a picture of it in the terminal is the
 * only way to tell a correct layout from a wrong one without sending a person
 * to go and look at the hardware.
 *
 * Read only. Maps the framebuffer PROT_READ and never writes.
 *
 * usage: fbpeek /dev/fb0            raw values at named points
 *        fbpeek /dev/fb0 --top      histogram of the commonest values
 *        fbpeek /dev/fb0 --map      ASCII art of the whole screen
 *        fbpeek /dev/fb0 --map W H  ASCII art at a chosen size
 *        fbpeek /dev/fb0 --map W H Y0 Y1   ASCII art of rows Y0..Y1 only
 */

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <linux/fb.h>

#define MAX_TOP 12

/* Luminance ramp, dark to light, with black and white at the two ends. */
static const char ramp[] = " .:-=+*#%@";

static unsigned px_at(const unsigned char *fb, size_t line_length,
                      int bpp, unsigned x, unsigned y)
{
    unsigned v;
    memcpy(&v, fb + (size_t)y * line_length + (size_t)x * bpp, bpp);
    return v;
}

int main(int argc, char **argv)
{
    const char *dev = argc > 1 ? argv[1] : "/dev/fb0";
    int want_top = (argc > 2 && strcmp(argv[2], "--top") == 0);
    int want_map = (argc > 2 && strcmp(argv[2], "--map") == 0);
    int map_w = 0, map_h = 0;
    /* Optional row range for --map. A 1080x1920 screen squeezed into 30 rows
     * averages 64 pixel rows into one character row, which is enough to see
     * that a keyboard is there and not enough to read a line of text. Naming
     * the rows is what makes the terminal half legible. */
    int row_lo = -1, row_hi = -1;
    if(want_map && argc > 4) {
        map_w = atoi(argv[3]);
        map_h = atoi(argv[4]);
    }
    if(want_map && argc > 6) {
        row_lo = atoi(argv[5]);
        row_hi = atoi(argv[6]);
    }

    int fd = open(dev, O_RDONLY);
    if(fd < 0) { perror(dev); return 1; }

    struct fb_var_screeninfo vinfo;
    struct fb_fix_screeninfo finfo;
    if(ioctl(fd, FBIOGET_VSCREENINFO, &vinfo) < 0) { perror("FBIOGET_VSCREENINFO"); return 1; }
    if(ioctl(fd, FBIOGET_FSCREENINFO, &finfo) < 0) { perror("FBIOGET_FSCREENINFO"); return 1; }

    size_t bytes = (size_t)finfo.line_length * vinfo.yres;
    unsigned char *fb = mmap(NULL, bytes, PROT_READ, MAP_SHARED, fd, 0);
    if(fb == MAP_FAILED) { perror("mmap"); return 1; }

    printf("%s: %ux%u  bpp %u  line_length %u  mapped %zu bytes\n",
           dev, vinfo.xres, vinfo.yres, vinfo.bits_per_pixel,
           (unsigned)finfo.line_length, bytes);
    if(finfo.smem_len > bytes)
        printf("  (smem_len %u is larger; %u more bytes are mapped by some programs)\n",
               finfo.smem_len, (unsigned)(finfo.smem_len - bytes));
    printf("\n");

    int bpp = vinfo.bits_per_pixel / 8;

    if(want_map) {
        /* Default size: about 2 characters wide to one line tall, which is
         * roughly the aspect ratio of a terminal character. */
        if(map_w <= 0) map_w = 54;
        if(map_h <= 0) map_h = 45;
        if(row_lo < 0) row_lo = 0;
        if(row_hi <= row_lo || row_hi > (int)vinfo.yres) row_hi = (int)vinfo.yres;
        int rows = row_hi - row_lo;

        printf("ASCII rendering, %d x %d cells over rows %d..%d, "
               "one cell per %dx%d pixels\n\n",
               map_w, map_h, row_lo, row_hi,
               (int)(vinfo.xres / map_w), rows / map_h);

        int rlen = (int)sizeof ramp - 2;

        for(int cy = 0; cy < map_h; cy++) {
            /* Each cell covers a block of source pixels. The block is averaged
             * rather than sampled, because a single pixel aliases badly on thin
             * strokes such as glyphs and key outlines. */
            int y0 = row_lo + (int)((long)cy * rows / map_h);
            int y1 = row_lo + (int)((long)(cy + 1) * rows / map_h);
            if(y1 <= y0) y1 = y0 + 1;

            for(int cx = 0; cx < map_w; cx++) {
                int x0 = (int)((long)cx * vinfo.xres / map_w);
                int x1 = (int)((long)(cx + 1) * vinfo.xres / map_w);
                if(x1 <= x0) x1 = x0 + 1;

                long lum = 0, n = 0;
                for(int y = y0; y < y1; y++) {
                    for(int x = x0; x < x1; x++) {
                        unsigned v = px_at(fb, finfo.line_length, bpp, x, y);
                        /* Rec. 601 luma. The top byte is ignored on purpose: one
                         * program writes 0x00 there and another 0xff for the same
                         * colour, and the difference is not what we are looking
                         * at. */
                        long r = v & 0xff, g = (v >> 8) & 0xff, b = (v >> 16) & 0xff;
                        lum += (r * 299 + g * 587 + b * 114) / 1000;
                        n++;
                    }
                }
                if(n == 0) n = 1;
                long avg = lum / n;
                int step = (int)(avg * rlen / 255);
                if(step < 0) step = 0;
                if(step > rlen) step = rlen;
                putchar(ramp[step]);
            }
            putchar('\n');
        }
        printf("\nlegend: ' ' = black, '%c' = white\n", ramp[rlen]);
        munmap(fb, bytes);
        close(fd);
        return 0;
    }

    if(want_top) {
        /* Histogram of the commonest values. Linear scan for a top-N is fine at
         * this size: a framebuffer is a couple of million pixels and this runs
         * a few times an hour. */
        struct { unsigned v; unsigned long n; } top[MAX_TOP];
        memset(top, 0, sizeof top);

        for(unsigned y = 0; y < vinfo.yres; y++) {
            for(unsigned x = 0; x < vinfo.xres; x++) {
                unsigned v = px_at(fb, finfo.line_length, bpp, x, y);
                int i;
                for(i = 0; i < MAX_TOP; i++) {
                    if(top[i].n && top[i].v == v) { top[i].n++; break; }
                    if(!top[i].n)          { top[i].v = v; top[i].n = 1; break; }
                }
                if(i == MAX_TOP) continue;   /* not in the top, ignore */
            }
        }

        for(int a = 0; a < MAX_TOP; a++)
            for(int b = a + 1; b < MAX_TOP; b++)
                if(top[b].n > top[a].n) {
                    unsigned tv = top[a].v; unsigned long tn = top[a].n;
                    top[a].v = top[b].v; top[a].n = top[b].n;
                    top[b].v = tv; top[b].n = tn;
                }

        unsigned long total = (unsigned long)vinfo.xres * vinfo.yres;
        printf("most common pixel values:\n");
        for(int i = 0; i < MAX_TOP; i++) {
            if(!top[i].n) break;
            printf("  0x%08x  r=%3u g=%3u b=%3u  %9lu  %5.1f%%\n",
                   top[i].v, top[i].v & 0xff, (top[i].v >> 8) & 0xff,
                   (top[i].v >> 16) & 0xff, top[i].n, 100.0 * top[i].n / total);
        }
        printf("\n");
    }

    /* Named points, which is what identifies a layout. */
    struct { const char *what; int x, y; } pts[] = {
        { "centre",        vinfo.xres / 2,  vinfo.yres / 2 },
        { "top-left",      0,               0 },
        { "top-right",     vinfo.xres - 1,  0 },
        { "bottom-left",   0,               vinfo.yres - 1 },
        { "bottom-centre", vinfo.xres / 2,  vinfo.yres - 1 },
        { "quarter-down",  vinfo.xres / 2,  vinfo.yres / 4 },
    };
    printf("sample pixels:\n");
    for(unsigned i = 0; i < sizeof pts / sizeof pts[0]; i++) {
        if((unsigned)pts[i].y >= vinfo.yres) continue;
        unsigned v = px_at(fb, finfo.line_length, bpp, pts[i].x, pts[i].y);
        printf("  %-15s (%4d,%4d)  0x%08x  r=%3u g=%3u b=%3u\n",
               pts[i].what, pts[i].x, pts[i].y, v,
               v & 0xff, (v >> 8) & 0xff, (v >> 16) & 0xff);
    }

    munmap(fb, bytes);
    close(fd);
    return 0;
}
