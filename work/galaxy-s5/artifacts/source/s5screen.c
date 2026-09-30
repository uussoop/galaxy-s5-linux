/*
 * s5screen - paint a boot status straight into the Exynos framebuffer.
 *
 * Why this exists
 * ---------------
 * The k3gxx kernel has CONFIG_VT_CONSOLE=y but leaves CONFIG_FRAMEBUFFER_CONSOLE
 * unset, so there is no text console on this panel: everything the first stage
 * initramfs prints goes to the serial/debug channels and to the log files, and
 * the screen stays black. The stage-1 CPIO also contains no plymouth at all.
 * That leaves a boot that fails with a completely featureless screen, which is
 * why earlier attempts looked identical whether they progressed or died.
 *
 * So paint the state ourselves. The framebuffer is the only output available,
 * and on this device it must be written through mmap: the fbdev accepts a
 * plain write(2) but consumes nothing (returns 0), and msync(2) is rejected
 * with EINVAL, because there is no backing store behind the panel memory. The
 * panel is refreshed by re-asserting the current pan offset with
 * FBIOPAN_DISPLAY, the same way msm-fb-refresher drives it.
 *
 * Contract
 * --------
 * This is a diagnostic aid and must never be able to interfere with booting:
 * every failure path returns 0, nothing is printed, and an alarm(5) backstop
 * kills the process even if a driver call were to block unexpectedly.
 */

#include <stdint.h>
#include <string.h>

#ifndef S5_HOST
/* The host harness below only needs the pixel code, none of the fbdev ioctls. */
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <linux/fb.h>
#endif

#ifndef FB_PATH
#define FB_PATH "/dev/fb0"
#endif

#define ADVANCE (S5_GLYPH_W + 1)

#include "s5_font.h"

struct s5_state {
	const char *name;
	uint32_t accent;	/* bar and rule colour */
	const char *title;	/* the big word */
	const char *detail;	/* the small line under it */
};

static const struct s5_state s5_states[] = {
	{ "booting",   0x00e08a00, "STARTING",  "LOOKING FOR THE ROOT FILESYSTEM" },
	{ "dmok",      0x0000c020, "DM OK",     "SUBPARTITIONS MAPPED" },
	{ "dmno",      0x00e08a00, "DM SKIPPED", "FALLING BACK TO LOOP" },
	{ "loopfail",  0x00c00020, "NO ROOT",   "SUBPARTITIONS FAILED" },
	{ "keys",      0x00c00020, "KEY HELD",  "BOOT HALTED ON REQUEST" },
	{ "booted",    0x0000c020, "ROOT MOUNTED", "HANDING OVER" },
	{ NULL,        0,          NULL,        NULL },
};

static const struct s5_state *s5_lookup(const char *name)
{
	for (const struct s5_state *s = s5_states; s->name; s++)
		if (!strcmp(s->name, name))
			return s;
	return s5_states;	/* "booting": never paint an unknown state */
}

static int s5_text_width(const char *s, int scale)
{
	int n = 0;

	while (s[n] && s[n] != '\n')
		n++;
	return n ? (n * ADVANCE - 1) * scale : 0;
}

static void s5_fill(uint32_t *fb, int stride_px, int fbw, int fbh,
		    int x0, int y0, int w, int h, uint32_t colour)
{
	int y, x;

	if (x0 < 0)
		x0 = 0;
	if (y0 < 0)
		y0 = 0;
	if (x0 + w > fbw)
		w = fbw - x0;
	if (y0 + h > fbh)
		h = fbh - y0;
	for (y = 0; y < h; y++) {
		uint32_t *row = fb + (size_t)(y0 + y) * stride_px + x0;
		for (x = 0; x < w; x++)
			row[x] = colour;
	}
}

static void s5_text(uint32_t *fb, int stride_px, int fbw, int fbh, int x, int y,
		    const char *s, int scale, uint32_t colour)
{
	for (; *s; s++) {
		int code = (unsigned char)*s;
		int col, row;

		if (code == ' ') {
			x += ADVANCE * scale;
			continue;
		}
		if (code < S5_FONT_FIRST || code > S5_FONT_LAST)
			code = '?';
		code -= S5_FONT_FIRST;
		for (col = 0; col < S5_GLYPH_W; col++) {
			unsigned bits = s5_font[code * S5_GLYPH_W + col];

			for (row = 0; row < S5_GLYPH_H; row++)
				if (bits & (1u << row))
					s5_fill(fb, stride_px, fbw, fbh,
						x + col * scale, y + row * scale,
						scale, scale, colour);
		}
		x += ADVANCE * scale;
	}
}

/* Pick the largest scale <= cap that still fits the text in fbw. */
static int s5_fit(const char *s, int fbw, int cap, int margin)
{
	int avail = fbw - 2 * margin;
	int scale = cap;

	if (avail <= 0)
		return 1;
	while (scale > 1 && s5_text_width(s, scale) > avail)
		scale--;
	return scale < 1 ? 1 : scale;
}

/*
 * Paint one state into a fbw x fbh window. Pure pixel work, no syscalls, so
 * the host test harness can render the exact same image off-device.
 */
static void s5_paint(uint32_t *fb, int stride_px, int fbw, int fbh,
		     const struct s5_state *s)
{
	int margin = fbw / 12;
	int bar_h, scale, width, x, y;

	if (fbw < 8 || fbh < 8)
		return;

	/* background */
	s5_fill(fb, stride_px, fbw, fbh, 0, 0, fbw, fbh, 0x00101014);

	/* accent bar across the top */
	bar_h = fbh / 9;
	s5_fill(fb, stride_px, fbw, fbh, 0, 0, fbw, bar_h, s->accent);

	/* brand line, centred in the bar */
	scale = s5_fit("POSTMARKETOS", fbw, 6, margin);
	width = s5_text_width("POSTMARKETOS", scale);
	s5_text(fb, stride_px, fbw, fbh, (fbw - width) / 2,
		(bar_h - S5_GLYPH_H * scale) / 2, "POSTMARKETOS", scale,
		0x00ffffff);

	/* the big word */
	scale = s5_fit(s->title, fbw, 18, margin);
	width = s5_text_width(s->title, scale);
	y = bar_h + (fbh - bar_h) * 2 / 5;
	x = (fbw - width) / 2;
	s5_text(fb, stride_px, fbw, fbh, x, y, s->title, scale, 0x00f0f0f0);

	/* accent rule under it */
	y += S5_GLYPH_H * scale + fbh / 24;
	s5_fill(fb, stride_px, fbw, fbh, x, y, width, fbh / 160 + 2, s->accent);

	/* detail line */
	scale = s5_fit(s->detail, fbw, 6, margin);
	width = s5_text_width(s->detail, scale);
	y = fbh * 3 / 4;
	s5_text(fb, stride_px, fbw, fbh, (fbw - width) / 2, y, s->detail,
		scale, 0x0090a0b0);

	/* state name, small, bottom left, so a photo of the screen is
	 * self-describing even if the words are ambiguous */
	scale = 4;
	width = s5_text_width(s->name, scale);
	s5_text(fb, stride_px, fbw, fbh, margin, fbh - S5_GLYPH_H * scale - margin,
		s->name, scale, 0x00606070);
}

#ifndef S5_HOST
int main(int argc, char **argv)
{
	struct fb_fix_screeninfo fix;
	struct fb_var_screeninfo var;
	const struct s5_state *state;
	uint32_t *fb;
	int fd, rows, stride_px, bpp;
	int yres, yres_virtual, i;

	/* Backstop: this must never be able to wedge a boot. */
	alarm(5);

	if (argc < 2)
		return 0;
	state = s5_lookup(argv[1]);

	fd = open(FB_PATH, O_RDWR);
	if (fd < 0)
		return 0;
	if (ioctl(fd, FBIOGET_FSCREENINFO, &fix) < 0 ||
	    ioctl(fd, FBIOGET_VSCREENINFO, &var) < 0) {
		close(fd);
		return 0;
	}
	bpp = var.bits_per_pixel;
	if (bpp != 16 && bpp != 32) {
		close(fd);
		return 0;
	}
	if (!fix.smem_len || !fix.line_length) {
		close(fd);
		return 0;
	}
	fb = mmap(NULL, fix.smem_len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (fb == MAP_FAILED) {
		close(fd);
		return 0;
	}

	stride_px = fix.line_length / (bpp / 8);
	rows = fix.smem_len / fix.line_length;
	if (rows > var.yres_virtual)
		rows = var.yres_virtual;
	yres = var.yres;
	yres_virtual = var.yres_virtual;
	if (yres <= 0 || yres > yres_virtual)
		yres = yres_virtual;
	if (yres <= 0 || rows < yres) {
		munmap(fb, fix.smem_len);
		close(fd);
		return 0;
	}

	/*
	 * The panel holds yres_virtual / yres stacked buffers and the kernel
	 * pans between them. Paint every complete buffer rather than working out
	 * which one is on screen: msm-fb-refresher is already flipping between
	 * them, and writing all of them means the status shows up no matter
	 * where the pan currently sits.
	 */
	for (i = 0; i + yres <= rows; i += yres)
		s5_paint(fb + (size_t)i * stride_px, stride_px, var.xres, yres,
			 state);

	/*
	 * No msync(2): this fbdev rejects it with EINVAL because the panel
	 * memory has no backing store. Re-asserting the current pan offset is
	 * what actually makes the controller pick the buffer up, exactly as
	 * msm-fb-refresher does it.
	 */
	if (ioctl(fd, FBIOPAN_DISPLAY, &var) < 0)
		(void)ioctl(fd, FBIOPUT_VSCREENINFO, &var);

	munmap(fb, fix.smem_len);
	close(fd);
	return 0;
}
#else
/* Host harness: render every state to a P6 PPM and report the geometry. */
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
	int fbw = argc > 1 ? atoi(argv[1]) : 1080;
	int fbh = argc > 2 ? atoi(argv[2]) : 1920;
	int geom_only = argc > 3 && !strcmp(argv[3], "geom");
	int fb = fbw * fbh * 4;
	uint32_t *buf = geom_only ? NULL : calloc(1, fb);
	char name[128];
	int bad = 0;

	for (const struct s5_state *s = s5_states; s->name; s++) {
		const char *lines[3] = { "POSTMARKETOS", s->title, s->detail };
		int caps[3] = { 6, 18, 6 };
		int li;

		if (argc > 3 && strcmp(argv[3], "only") == 0 && strcmp(argv[4], s->name))
			continue;

		printf("%-9s ", s->name);
		for (li = 0; li < 3; li++) {
			int scale = s5_fit(lines[li], fbw, caps[li], fbw / 12);
			int w = s5_text_width(lines[li], scale);
			int x = (fbw - w) / 2;
			int ok = x >= 0 && x + w <= fbw && w > 0;

			if (!ok)
				bad = 1;
			printf("| %-24s scale=%2d w=%4d x=%4d %s ", lines[li],
			       scale, w, x, ok ? "ok" : "OVERFLOW");
		}
		printf("\n");

		if (buf) {
			FILE *f;

			memset(buf, 0, fb);
			s5_paint(buf, fbw, fbw, fbh, s);
			snprintf(name, sizeof(name), "state-%s.ppm", s->name);
			f = fopen(name, "wb");
			if (!f)
				continue;
			fprintf(f, "P6\n%d %d\n255\n", fbw, fbh);
			for (int i = 0; i < fbw * fbh; i++) {
				uint32_t px = buf[i];
				unsigned char rgb[3] = {
					(unsigned char)(px >> 16),
					(unsigned char)(px >> 8),
					(unsigned char)px
				};
				fwrite(rgb, 1, 3, f);
			}
			fclose(f);
		}
	}
	if (bad) {
		printf("\nFAIL: at least one line does not fit\n");
		return 1;
	}
	printf("\nall text fits inside %dx%d\n", fbw, fbh);
	return 0;
}
#endif
