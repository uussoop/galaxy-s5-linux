/*
 * fbwatch - time every change to the panel.
 *
 * Samples a spread of rows across the framebuffer on a fixed interval and
 * prints a line whenever the contents differ from the previous sample. This
 * turns "it's flickering" into a number: how many repaints, and at what
 * interval. Idle flicker and touch-triggered flicker look completely different
 * in the output, and guessing between them wastes more time than measuring.
 *
 * Read-only: opens /dev/fb0 O_RDONLY and touches nothing.
 */
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define W 1080
#define H 1920

/* Compare the whole screen against the previous sample and describe what
 * moved: how many pixels, the rows and columns involved, and the colours on
 * either side. That turns "it's flickering" into a location, which is the
 * difference between knowing which code to read and guessing. */
int main(int argc, char **argv)
{
    int secs = argc > 1 ? atoi(argv[1]) : 15;
    int ms   = argc > 2 ? atoi(argv[2]) : 50;

    int fd = open("/dev/fb0", O_RDONLY);
    if (fd < 0) { perror("open /dev/fb0"); return 1; }
    uint32_t *p = mmap(NULL, (size_t)W * H * 4, PROT_READ, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }

    uint32_t *prev = malloc((size_t)W * H * 4);
    if (!prev) return 1;

    size_t total = (size_t)W * H;
    memcpy(prev, p, total * 4);

    struct timespec t0;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    int changes = 0;
    for (int step = 0; step * ms < secs * 1000; step++) {
        struct timespec req = { ms / 1000, (long)(ms % 1000) * 1000000L };
        nanosleep(&req, NULL);

        int ndiff = 0, y0 = H, y1 = -1, x0 = W, x1 = -1;
        uint32_t was = 0, now = 0;
        for (size_t i = 0; i < total; i++) {
            if (p[i] == prev[i]) continue;
            ndiff++;
            int y = (int)(i / W), x = (int)(i % W);
            if (y < y0) y0 = y;
            if (y > y1) y1 = y;
            if (x < x0) x0 = x;
            if (x > x1) x1 = x;
            if (!was)  was  = prev[i];
            if (!now)  now  = p[i];
        }

        struct timespec t;
        clock_gettime(CLOCK_MONOTONIC, &t);
        double el = (t.tv_sec - t0.tv_sec) + (t.tv_nsec - t0.tv_nsec) / 1e9;

        if (ndiff) {
            changes++;
            printf("[%6.2fs] %6d px  y %4d..%-4d  x %4d..%-4d  0x%08x -> 0x%08x\n",
                   el, ndiff, y0, y1, x0, x1, was, now);
            fflush(stdout);
        }
        memcpy(prev, p, total * 4);
    }

    printf("--- %d changes in %d s at %d ms sampling ---\n", changes, secs, ms);
    free(prev);
    munmap(p, (size_t)W * H * 4);
    close(fd);
    return 0;
}
