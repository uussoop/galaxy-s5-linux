/*
 * absprobe - report the absolute axis capabilities of an evdev node, correctly.
 *
 * This exists because two of our own tools got this wrong, in the same way, and
 * that mistake cost a round of debugging on the touch screen.
 *
 * EVIOCGBIT(EV_ABS, n) writes ceil(n/8) bytes. Every ABS_MT_* code is 47 or
 * higher, so a caller that asks for sizeof(uint32_t) bytes learns about event
 * codes 0..31 and nothing else. A probe that then reports "this device has no
 * multitouch" is not wrong about the device, it is wrong about what it asked.
 *
 * So this asks for a bitmap wide enough to hold every code the kernel can
 * report, and prints both the axes and their ranges. It is read only: it opens
 * the node O_RDONLY and does nothing but ioctl.
 *
 * usage: absprobe /dev/input/event2
 */

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <linux/input.h>

/* Wide enough for ABS_CNT codes. EVIOCGBIT takes the byte count, so this asks
 * for the full bitmap rather than a truncated prefix of it. */
#define BITS_WORDS ((ABS_CNT + 31) / 32)

static int bit_test(const unsigned long *bits, int code)
{
    return (bits[code / 32] >> (code % 32)) & 1u;
}

int main(int argc, char **argv)
{
    const char *dev = argc > 1 ? argv[1] : "/dev/input/event0";

    int fd = open(dev, O_RDONLY | O_NONBLOCK);
    if(fd < 0) { perror(dev); return 1; }

    char name[128] = "?", phys[128] = "?";
    if(ioctl(fd, EVIOCGNAME(sizeof name - 1), name) < 0)  snprintf(name, sizeof name, "?");
    if(ioctl(fd, EVIOCGPHYS(sizeof phys - 1), phys) < 0) snprintf(phys, sizeof phys, "?");
    printf("device  %s\n", dev);
    printf("name    %s\n", name);
    printf("phys    %s\n\n", phys);

    /* Which event types exist at all. */
    unsigned long evbits[(EV_MAX + 31) / 32] = {0};
    if(ioctl(fd, EVIOCGBIT(0, sizeof evbits), evbits) == 0) {
        printf("event types:");
        for(int t = 0; t <= EV_MAX; t++)
            if(bit_test(evbits, t)) printf(" %d", t);
        printf("\n");
    }

    /* The absolute axes, in full. */
    unsigned long absbits[BITS_WORDS] = {0};
    if(ioctl(fd, EVIOCGBIT(EV_ABS, sizeof absbits), absbits) < 0) {
        perror("EVIOCGBIT(EV_ABS)");
        close(fd);
        return 1;
    }

    printf("\nABS axes present (%d bytes of bitmap requested):\n",
           (int)sizeof absbits);
    int n = 0;
    for(int code = 0; code < ABS_CNT; code++) {
        if(!bit_test(absbits, code)) continue;
        char label[64] = "";
        const char *known = NULL;
        switch(code) {
        case ABS_X:  known = "ABS_X"; break;
        case ABS_Y:  known = "ABS_Y"; break;
        case ABS_MT_SLOT: known = "ABS_MT_SLOT"; break;
        case ABS_MT_TOUCH_MAJOR: known = "ABS_MT_TOUCH_MAJOR"; break;
        case ABS_MT_TOUCH_MINOR: known = "ABS_MT_TOUCH_MINOR"; break;
        case ABS_MT_POSITION_X: known = "ABS_MT_POSITION_X"; break;
        case ABS_MT_POSITION_Y: known = "ABS_MT_POSITION_Y"; break;
        case ABS_MT_TRACKING_ID: known = "ABS_MT_TRACKING_ID"; break;
        case ABS_PRESSURE: known = "ABS_PRESSURE"; break;
        case ABS_MT_PRESSURE: known = "ABS_MT_PRESSURE"; break;
        default: break;
        }
        if(known) snprintf(label, sizeof label, "%-18s", known);

        struct input_absinfo ai;
        if(ioctl(fd, EVIOCGABS(code), &ai) == 0)
            printf("  %3d  %-18s min %6d max %6d  res %5d  flat %d\n",
                   code, label, ai.minimum, ai.maximum, ai.resolution, ai.flat);
        else
            printf("  %3d  %-18s (no range: %s)\n", code, label, strerror(errno));
        n++;
    }
    if(!n) printf("  none\n");

    /* The two things a caller most often gets wrong, stated plainly. */
    int has_xy   = bit_test(absbits, ABS_X) && bit_test(absbits, ABS_Y);
    int has_mtxy = bit_test(absbits, ABS_MT_POSITION_X) &&
                   bit_test(absbits, ABS_MT_POSITION_Y);
    printf("\nABS_X/ABS_Y pair:            %s\n", has_xy ? "present" : "ABSENT");
    printf("ABS_MT_POSITION_X/Y pair:    %s\n", has_mtxy ? "present" : "ABSENT");
    printf("\nA 32 bit capability mask would test bits 0 and 1, i.e. %s.\n",
           has_xy ? "find the pair, and auto-detect a pointer by luck"
                  : "find nothing, and reject the touchscreen outright");
    printf("A mask wide enough for all %d ABS codes sees the pair: %s.\n",
           ABS_CNT, has_mtxy ? "yes" : "no");

    close(fd);
    return 0;
}
