/* s5iskey - like iskey(1), but names the device and can ignore synthesised keys.
 *
 * The initramfs iskey(1) aggregates libevdev state over every /dev/input/event*,
 * so when it reports a key as held it cannot say which device reported it. On
 * the Galaxy S5 two devices advertise KEY_VOLUMEUP:
 *
 *   gpio_keys.16  the real volume key, and
 *   Headset       arizona-extcon, which synthesises headset remote key events
 *                 while the headphone-detect pin is still settling at boot.
 *
 * Both of those can therefore make iskey claim volume-up is held. The boot
 * check "hold left shift and volume up to fail the boot" then fires on a key
 * nobody pressed, and the phone halts with a black screen. Nothing advertises
 * KEY_LEFTSHIFT, so volume-up is the only way that check can be triggered, and
 * the real volume key is always gpio_keys.
 *
 * This asks the same question as iskey while skipping devices whose name
 * matches an exclusion, and prints which device answered so a log can record
 * it. Default exclusion is the synthesising headset device; pass --all to
 * include every device (behaviour identical to iskey).
 *
 * Exit status: 0 if a listed key is held on a non-excluded device, 1 if not,
 * 2 on usage error. Purely observational: reads only, writes nothing.
 *
 * Build: zig cc -target arm-linux-musleabihf -static -O2 -o s5iskey s5iskey.c
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define MAX_NODES 32
#define KEYBITS_BYTES ((KEY_MAX + 1 + 7) / 8)

/* Substrings matched (case-sensitive) against EVIOCGNAME. "Headset" is the
 * arizona-extcon device; "gpio_keys" is deliberately NOT excluded, because that
 * is the physical key the documented shortcut is meant to use. */
static const char *DEFAULT_EXCLUDE[] = { "Headset", "headset", "arizona" };
#define NDEFAULT_EXCLUDE (int)(sizeof(DEFAULT_EXCLUDE) / sizeof(DEFAULT_EXCLUDE[0]))

struct kdef {
	const char *name;
	unsigned int code;
};

/* Only keys that can plausibly affect a boot decision are named; anything
 * else is reported by number so the tool still works if asked about it. */
static const struct kdef KEYS[] = {
	{ "KEY_LEFTSHIFT", KEY_LEFTSHIFT },
	{ "KEY_RIGHTSHIFT", KEY_RIGHTSHIFT },
	{ "KEY_LEFTCTRL", KEY_LEFTCTRL },
	{ "KEY_RIGHTCTRL", KEY_RIGHTCTRL },
	{ "KEY_VOLUMEUP", KEY_VOLUMEUP },
	{ "KEY_VOLUMEDOWN", KEY_VOLUMEDOWN },
	{ "KEY_POWER", KEY_POWER },
	{ "KEY_HOME", KEY_HOME },
	{ "KEY_MENU", KEY_MENU },
	{ "KEY_BACK", KEY_BACK },
	{ "KEY_ENTER", KEY_ENTER },
};
#define NKEYS (int)(sizeof(KEYS) / sizeof(KEYS[0]))

static int lookup(const char *want, unsigned int *code)
{
	for (int i = 0; i < NKEYS; i++) {
		if (strcmp(KEYS[i].name, want) == 0) {
			*code = KEYS[i].code;
			return 1;
		}
	}
	/* Accept "KEY_" + digits for anything not in the table, so the tool is
	 * not limited to the list above. */
	if (strncmp(want, "KEY_", 4) == 0) {
		const char *p = want + 4;
		if (*p >= '0' && *p <= '9') {
			char *end = NULL;
			unsigned long v = strtoul(p, &end, 10);
			if (end && *end == '\0' && v <= KEY_MAX) {
				*code = (unsigned int)v;
				return 1;
			}
		}
	}
	return 0;
}

static int bitset(const unsigned char *bm, unsigned int bit)
{
	return (bm[bit / 8] >> (bit % 8)) & 1;
}

static int excluded(const char *name, const char **list, int n)
{
	for (int i = 0; i < n; i++)
		if (strstr(name, list[i]))
			return 1;
	return 0;
}

int main(int argc, char **argv)
{
	int i, argi = 1, include_all = 0, verbose = 0;
	unsigned int codes[32];
	const char *names[32];
	int ncodes = 0;
	int held = 0;

	if (argi < argc && strcmp(argv[argi], "--all") == 0) {
		include_all = 1;
		argi++;
	}
	if (argi < argc && strcmp(argv[argi], "--verbose") == 0) {
		verbose = 1;
		argi++;
	}
	for (; argi < argc; argi++) {
		if (ncodes >= (int)(sizeof(codes) / sizeof(codes[0]))) {
			fprintf(stderr, "s5iskey: too many keys\n");
			return 2;
		}
		if (!lookup(argv[argi], &codes[ncodes])) {
			fprintf(stderr, "s5iskey: unknown key name '%s'\n", argv[argi]);
			return 2;
		}
		names[ncodes] = argv[argi];
		ncodes++;
	}
	if (ncodes == 0) {
		fprintf(stderr, "usage: s5iskey [--all] [--verbose] KEY [KEY...]\n");
		return 2;
	}

	for (i = 0; i < MAX_NODES; i++) {
		char path[64];
		int len = snprintf(path, sizeof(path), "/dev/input/event%d", i);
		int fd;
		char name[128];
		unsigned char ev_bits[((EV_MAX + 1) + 7) / 8];
		unsigned char key_bits[KEYBITS_BYTES];
		unsigned char state[KEYBITS_BYTES];
		int reported = 0;

		if (len < 0 || (size_t)len >= sizeof(path))
			continue;
		fd = open(path, O_RDONLY | O_NONBLOCK);
		if (fd < 0)
			continue;

		memset(name, 0, sizeof(name));
		if (ioctl(fd, EVIOCGNAME(sizeof(name) - 1), name) < 0)
			snprintf(name, sizeof(name), "(unnamed)");
		name[sizeof(name) - 1] = '\0';

		memset(ev_bits, 0, sizeof(ev_bits));
		memset(key_bits, 0, sizeof(key_bits));
		if (ioctl(fd, EVIOCGBIT(0, sizeof(ev_bits)), ev_bits) < 0 ||
		    !bitset(ev_bits, EV_KEY)) {
			close(fd);
			continue;
		}
		if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(key_bits)), key_bits) < 0) {
			close(fd);
			continue;
		}

		/* Skip devices that cannot report any key we were asked about. */
		int useful = 0;
		for (int k = 0; k < ncodes; k++)
			if (bitset(key_bits, codes[k]))
				useful = 1;
		if (!useful) {
			if (verbose)
				printf("INFO: s5iskey: %s (%s) cannot report those keys\n",
				       path, name);
			close(fd);
			continue;
		}

		if (!include_all && excluded(name, DEFAULT_EXCLUDE, NDEFAULT_EXCLUDE)) {
			if (verbose)
				printf("INFO: s5iskey: ignoring %s (%s), synthesised keys\n",
				       path, name);
			close(fd);
			continue;
		}

		memset(state, 0, sizeof(state));
		if (ioctl(fd, EVIOCGKEY(sizeof(state)), state) < 0) {
			close(fd);
			continue;
		}
		for (int k = 0; k < ncodes; k++) {
			if (!bitset(state, codes[k]))
				continue;
			if (!held)
				printf("INFO: s5iskey: %s (%s) reports %s held\n",
				       path, name, names[k]);
			else
				printf("INFO: s5iskey: %s (%s) also reports %s held\n",
				       path, name, names[k]);
			held = 1;
			reported = 1;
		}
		(void)reported;
		close(fd);
	}

	if (!held && verbose)
		printf("INFO: s5iskey: no non-excluded device reports a held key\n");
	return held ? 0 : 1;
}
