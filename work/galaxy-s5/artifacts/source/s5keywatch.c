/* s5keywatch - report which input device claims a boot-failing key is held.
 *
 * iskey() aggregates libevdev state over every /dev/input/event*, so when it
 * says a key is down it cannot say which device said so. On this hardware two
 * devices advertise KEY_VOLUMEUP (gpio_keys, the real key, and "Headset" from
 * arizona-extcon, which synthesises remote keys), and that distinction is the
 * whole question: a person holding volume up, or a driver stuck asserting it.
 *
 * This polls EVIOCGKEY on each event node directly and prints only transitions,
 * so an hour of polling stays readable. Nothing is written and nothing is
 * modified; it is a pure observer.
 *
 * Build: zig cc -target arm-linux-musleabihf -static -O2 -o s5keywatch s5keywatch.c
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

/* input-event-codes.h in this 3.10 tree has no KEY_HEADSETHOOK; the arizona
 * headset remote reports KEY_PLAYPAUSE there. Alias it so the name printed
 * still says what the boot-failing key check cares about. */
#define KEY_HEADSETHOOK_ALIAS KEY_PLAYPAUSE

#define MAX_NODES 32

struct node {
	int fd;
	char path[64];
	char name[128];
	unsigned long long held; /* bitmap of keys currently down, bit per keycode */
};

static const char *keyname(unsigned int code)
{
	switch (code) {
	case KEY_LEFTSHIFT: return "KEY_LEFTSHIFT";
	case KEY_RIGHTSHIFT: return "KEY_RIGHTSHIFT";
	case KEY_VOLUMEUP: return "KEY_VOLUMEUP";
	case KEY_VOLUMEDOWN: return "KEY_VOLUMEDOWN";
	case KEY_POWER: return "KEY_POWER";
	case KEY_HOME: return "KEY_HOME";
	case KEY_MENU: return "KEY_MENU";
	case KEY_BACK: return "KEY_BACK";
	case KEY_HEADSETHOOK_ALIAS: return "KEY_HEADSETHOOK";
	default: return NULL;
	}
}

/* Only the keys that can plausibly affect the boot decision are named. */
static const unsigned int WATCH[] = {
	KEY_LEFTSHIFT, KEY_RIGHTSHIFT, KEY_VOLUMEUP, KEY_VOLUMEDOWN,
	KEY_POWER, KEY_HOME, KEY_MENU, KEY_BACK, KEY_HEADSETHOOK_ALIAS,
};
#define NWATCH (int)(sizeof(WATCH) / sizeof(WATCH[0]))

static int is_watched(unsigned int code)
{
	for (int i = 0; i < NWATCH; i++)
		if (WATCH[i] == code)
			return 1;
	return 0;
}

static int bitset(const unsigned char *bm, unsigned int code)
{
	return (bm[code / 8] >> (code % 8)) & 1;
}

static void describe(char *out, size_t n, const unsigned char *state)
{
	size_t used = 0;
	out[0] = '\0';
	for (int i = 0; i < NWATCH; i++) {
		unsigned int code = WATCH[i];
		if (!bitset(state, code))
			continue;
		const char *nm = keyname(code);
		int w = snprintf(out + used, n - used, "%s%s",
				 used ? "," : "", nm ? nm : "?");
		if (w < 0 || (size_t)w >= n - used)
			break;
		used += (size_t)w;
	}
}

int main(int argc, char **argv)
{
	int seconds = argc > 1 ? atoi(argv[1]) : 60;
	struct node nodes[MAX_NODES];
	int count = 0;

	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("s5keywatch: polling %d s\n", seconds);

	/* Enumerate /dev/input/event* in a fixed order for stable output. */
	for (int i = 0; i < MAX_NODES && count < MAX_NODES; i++) {
		struct node *n = &nodes[count];
		int len = snprintf(n->path, sizeof(n->path), "/dev/input/event%d", i);
		if (len < 0 || (size_t)len >= sizeof(n->path))
			continue;
		n->fd = open(n->path, O_RDONLY | O_NONBLOCK);
		if (n->fd < 0)
			continue;
		struct input_id id;
		memset(&id, 0, sizeof(id));
		if (ioctl(n->fd, EVIOCGID, &id) < 0) {
			close(n->fd);
			continue;
		}
		n->name[0] = '\0';
		if (ioctl(n->fd, EVIOCGNAME(sizeof(n->name) - 1), n->name) < 0)
			snprintf(n->name, sizeof(n->name), "(unnamed)");
		n->name[sizeof(n->name) - 1] = '\0';

		/* Only nodes that can report a watched key are worth polling. */
		unsigned char ev_bits[((EV_MAX + 1) + 7) / 8];
		unsigned char key_bits[(KEY_MAX + 1 + 7) / 8];
		memset(ev_bits, 0, sizeof(ev_bits));
		memset(key_bits, 0, sizeof(key_bits));
		if (ioctl(n->fd, EVIOCGBIT(0, sizeof(ev_bits)), ev_bits) < 0 ||
		    !bitset(ev_bits, EV_KEY)) {
			close(n->fd);
			continue;
		}
		if (ioctl(n->fd, EVIOCGBIT(EV_KEY, sizeof(key_bits)), key_bits) < 0) {
			close(n->fd);
			continue;
		}
		int useful = 0;
		for (int k = 0; k < NWATCH; k++)
			if (bitset(key_bits, WATCH[k]))
				useful = 1;
		if (!useful) {
			close(n->fd);
			continue;
		}
		n->held = 0;
		count++;
		printf("  watching %-20s name=%-24s can report:", n->path, n->name);
		for (int k = 0; k < NWATCH; k++)
			if (bitset(key_bits, WATCH[k]))
				printf(" %s", keyname(WATCH[k]) ? keyname(WATCH[k]) : "?");
		printf("\n");
	}
	if (count == 0) {
		printf("s5keywatch: no event node can report a watched key\n");
		return 1;
	}

	/* Poll, printing only transitions, until the deadline. */
	time_t deadline = time(NULL) + seconds;
	unsigned long long polls = 0;
	while (time(NULL) < deadline) {
		for (int i = 0; i < count; i++) {
			struct node *n = &nodes[i];
			unsigned char state[(KEY_MAX + 1 + 7) / 8];
			memset(state, 0, sizeof(state));
			if (ioctl(n->fd, EVIOCGKEY(sizeof(state)), state) < 0)
				continue;
			unsigned long long now = 0;
			for (int k = 0; k < NWATCH; k++)
				if (is_watched(WATCH[k]) && bitset(state, WATCH[k]))
					now |= 1ULL << k;
			if (now == n->held)
				continue;
			char text[256];
			describe(text, sizeof(text), state);
			printf("[+%3d] %-20s %-24s %s\n",
			       (int)(seconds - (deadline - time(NULL))),
			       n->path, n->name,
			       now ? text : "(nothing held)");
			n->held = now;
		}
		polls++;
		usleep(200 * 1000);
	}

	printf("s5keywatch: %d s elapsed, %llu polls\n", seconds, polls);
	for (int i = 0; i < count; i++)
		close(nodes[i].fd);
	return 0;
}
