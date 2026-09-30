// s5probe - read the touchscreen's real axis geometry and the framebuffer's
// mode, straight from the kernel. Read-only: it opens devices, queries them
// with ioctl, prints, and exits. It never writes to /dev/fb0 or /dev/uinput.
#include <stdio.h>
#include <fcntl.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/fb.h>
#include <linux/input.h>

static const char *ev_name(int t) {
    switch (t) {
    case EV_SYN: return "SYN";
    case EV_KEY: return "KEY";
    case EV_REL: return "REL";
    case EV_ABS: return "ABS";
    case EV_MSC: return "MSC";
    case EV_SW:  return "SW";
    case EV_LED: return "LED";
    case EV_SND: return "SND";
    case EV_REP: return "REP";
    case EV_FF:  return "FF";
    }
    return "?";
}

int main(void) {
    /* ---- input devices ---- */
    int n = 0;
    FILE *f = fopen("/proc/bus/input/devices", "r");
    if (f) {
        char line[512];
        while (fgets(line, sizeof line, f)) {
            if (strncmp(line, "N:", 2) == 0 || strncmp(line, "H:", 2) == 0) {
                printf("  %s", line);
                n++;
            }
        }
        fclose(f);
    }
    printf("  (devices listed: %d)\n", n);

    /* ---- the touchscreen, in detail ---- */
    int fd = open("/dev/input/event2", O_RDONLY | O_NONBLOCK);
    if (fd < 0) { printf("event2 open failed: %s\n", strerror(errno)); return 1; }

    char nm[256] = {0};
    if (ioctl(fd, EVIOCGNAME(sizeof nm), nm) >= 0)
        printf("TOUCH NAME: %s\n", nm);

    struct input_id id;
    memset(&id, 0, sizeof id);
    ioctl(fd, EVIOCGID, &id);
    printf("BUS 0x%04x VENDOR 0x%04x PRODUCT 0x%04x VERSION 0x%04x\n",
           id.bustype, id.vendor, id.product, id.version);

    unsigned long mask = 0;
    int bits = 0;
    if (ioctl(fd, EVIOCGBIT(0, sizeof mask), &mask) >= 0)
        for (int b = 0; b < (int)(sizeof mask * 8); b++)
            if (mask & (1UL << b)) { printf("  type %2d = %s\n", b, ev_name(b)); bits++; }
    printf("  event types: %d\n", bits);

    /* Which of the button codes does it actually assert? We need one of these
       to know when a finger goes down and when it lifts. */
    unsigned long kbits = 0;
    if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof kbits), &kbits) >= 0 && kbits) {
        printf("KEY bits: 0x%08lx\n", kbits);
        for (int k = 0; k < 32; k++)
            if (kbits & (1UL << k))
                printf("  code 0x%03x = %s\n", k,
                       k == 0x110 ? "BTN_LEFT" :
                       k == 0x111 ? "BTN_RIGHT" : "BTN_MIDDLE");
    }

    unsigned long sbits = 0;
    if (ioctl(fd, EVIOCGBIT(EV_SW, sizeof sbits), &sbits) >= 0 && sbits) {
        printf("SW bits: 0x%08lx\n", sbits);
        for (int k = 0; k < 32; k++)
            if (sbits & (1UL << k))
                printf("  code 0x%03x\n", k);
    }

    unsigned long abits = 0;
    if (ioctl(fd, EVIOCGBIT(EV_ABS, sizeof abits), &abits) >= 0) {
        printf("ABS axes:\n");
        for (int a = 0; a < 64; a++) {
            if (!(abits & (1UL << a))) continue;
            struct input_absinfo ai;
            memset(&ai, 0, sizeof ai);
            if (ioctl(fd, EVIOCGABS(a), &ai) >= 0)
                printf("  ABS_%-18d min=%-7d max=%-7d res=%-5d fuzz=%d flat=%d\n",
                       a, ai.minimum, ai.maximum, ai.resolution, ai.fuzz, ai.flat);
        }
    }
    close(fd);

    /* ---- the framebuffer ---- */
    int fb = open("/dev/fb0", O_RDWR);
    if (fb < 0) { printf("fb0 open failed: %s\n", strerror(errno)); return 1; }
    struct fb_var_screeninfo v;
    struct fb_fix_screeninfo fi;
    memset(&v, 0, sizeof v);
    memset(&fi, 0, sizeof fi);
    if (ioctl(fb, FBIOGET_VSCREENINFO, &v) >= 0) {
        printf("FB: %ux%u virtual %ux%u  bpp=%u  nonstd=%d\n",
               v.xres, v.yres, v.xres_virtual, v.yres_virtual,
               v.bits_per_pixel, v.nonstd);
    } else printf("FBIOGET_VSCREENINFO failed: %s\n", strerror(errno));
    if (ioctl(fb, FBIOGET_FSCREENINFO, &fi) >= 0)
        printf("FB id=\"%.16s\" smem_len=%u  type=%u (0=PACKED_PIXELS)\n",
               fi.id, fi.smem_len, fi.type);
    /* Channel layout, so we know the byte order to write pixels in. */
    if (ioctl(fb, FBIOGET_VSCREENINFO, &v) >= 0)
        printf("FB pixel: R off=%u len=%u  G off=%u len=%u  B off=%u len=%u  T off=%u len=%u\n",
               v.red.offset, v.red.length, v.green.offset, v.green.length,
               v.blue.offset, v.blue.length, v.transp.offset, v.transp.length);
    printf("FB bytes per pixel: %.1f (smem_len/pixels = %.1f)\n",
           v.bits_per_pixel / 8.0,
           (double)fi.smem_len / ((double)v.xres * v.yres));
    close(fb);

    /* ---- uinput availability ---- */
    int ui = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (ui >= 0) {
        printf("UINPUT: open OK (writable) - keystroke injection is possible\n");
        close(ui);
    } else {
        printf("UINPUT: open failed: %s\n", strerror(errno));
    }
    return 0;
}
