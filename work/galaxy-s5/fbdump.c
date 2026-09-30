/*
 * fbdump - report what is actually on the panel right now.
 *
 * s5term owns /dev/fb0, so this mmaps the same memory read-only and counts
 * colours. That gives hard evidence about the screen without asking anyone to
 * squint at a phone in a dim room, and without disturbing the display: it
 * opens nothing for writing and changes nothing.
 */
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

#define W 1080
#define H 1920

int main(void)
{
    int fd = open("/dev/fb0", O_RDONLY);
    if (fd < 0) { perror("open /dev/fb0"); return 1; }

    uint32_t *p = mmap(NULL, (size_t)W * H * 4, PROT_READ, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }

    unsigned long black = 0, cyan = 0, white = 0, dim = 0, btn = 0, other = 0;
    int minx = W, maxx = -1, miny = H, maxy = -1;

    for (int y = 0; y < H; y++) {
        for (int x = 0; x < W; x++) {
            uint32_t v = p[(size_t)y * W + x];
            if (v == 0x00000000u) { black++; continue; }
            if (v == 0x00c0c000u) { cyan++; }        /* RGB(00,c0,c0) */
            else if (v == 0xdcdcdcu) { white++; }   /* RGB(dc,dc,dc) */
            else if (v == 0x505050u) { dim++; }
            else if (v == 0x141008u) { btn++; }
            else { other++; continue; }
            if (x < minx) minx = x;
            if (x > maxx) maxx = x;
            if (y < miny) miny = y;
            if (y > maxy) maxy = y;
        }
    }

    unsigned long total = (unsigned long)W * H;
    printf("panel 1080x1920  total=%lu\n", total);
    printf("  black  %8lu  %5.1f%%\n", black, 100.0 * black / total);
    printf("  cyan   %8lu  %5.1f%%   (wake button + its label)\n", cyan, 100.0 * cyan / total);
    printf("  white  %8lu  %5.1f%%   (terminal text)\n", white, 100.0 * white / total);
    printf("  dim    %8lu  %5.1f%%   (subtitle)\n", dim, 100.0 * dim / total);
    printf("  button %8lu  %5.1f%%   (wake button fill)\n", btn, 100.0 * btn / total);
    printf("  other  %8lu  %5.1f%%\n", other, 100.0 * other / total);
    if (maxx >= 0)
        printf("  lit area bbox: x %d..%d  y %d..%d\n", minx, maxx, miny, maxy);

    /* Anything at all? A fully black panel means the backlight is off or the
     * framebuffer is not the one being scanned out. */
    printf("  verdict: %s\n",
           (cyan + white + dim) == 0 ? "PANEL IS BLANK" : "panel has content");

    munmap(p, (size_t)W * H * 4);
    close(fd);
    return 0;
}
