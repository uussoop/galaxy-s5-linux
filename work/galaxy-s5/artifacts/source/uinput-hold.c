/* uinput-hold - hold KEY_VOLUMEUP on two virtual input devices.
 *
 * Proves the s5iskey exclusion logic end to end without a human pressing
 * anything. It creates two uinput devices that both advertise KEY_VOLUMEUP:
 *
 *   "Headset"         the name arizona-extcon uses on this hardware, so this
 *                     stands in for the driver that synthesises headset keys
 *   "gpio_keys.test"  the name shape gpio_keys uses, so this stands in for
 *                     the real volume key
 *
 * and presses volume-up on both at once. That is precisely the ambiguity that
 * halted boot 6, so:
 *
 *   s5iskey --verbose KEY_VOLUMEUP   must report only gpio_keys.test, exit 0
 *   s5iskey --all     KEY_VOLUMEUP   must report both,               exit 0
 *
 * and once released, both must go back to exit 1.
 *
 * Build: zig cc -target arm-linux-musleabihf -static -O2 -o uinput-hold uinput-hold.c
 * Usage: uinput-hold <seconds-to-hold>
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

struct vdev {
	int fd;
	const char *name;
};

/* musl's linux/input.h leaves the time member out of struct input_event, so the
 * kernel ABI is spelled out here instead. On 32-bit ARM musl, time_t and
 * suseconds_t are both 4 bytes, giving 8 + 2 + 2 + 4 = 16 bytes, which is what
 * uinput expects. */
struct uinput_event {
	struct timeval time;
	unsigned short type;
	unsigned short code;
	int value;
};

/* Create a virtual keyboard-ish device that advertises the volume keys. */
static int make_dev(const char *name)
{
	int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
	struct uinput_setup setup;
	struct uinput_user_dev user;
	struct input_id id;

	if (fd < 0) {
		fprintf(stderr, "uinput-hold: open /dev/uinput: %s\n", strerror(errno));
		return -1;
	}
	if (ioctl(fd, UI_SET_EVBIT, EV_SYN) < 0 ||
	    ioctl(fd, UI_SET_EVBIT, EV_KEY) < 0 ||
	    ioctl(fd, UI_SET_KEYBIT, KEY_VOLUMEUP) < 0 ||
	    ioctl(fd, UI_SET_KEYBIT, KEY_VOLUMEDOWN) < 0) {
		fprintf(stderr, "uinput-hold: %s: %s\n", name, strerror(errno));
		close(fd);
		return -1;
	}
	memset(&id, 0, sizeof(id));
	id.bustype = BUS_USB;
	id.vendor = 0x1234;
	id.product = 0x5678;
	id.version = 1;

	/* UI_DEV_SETUP arrived in Linux 4.5. This recovery kernel is older and
	 * answers EINVAL, so fall back to the original ABI: write a
	 * uinput_user_dev, then create. Either path yields the same node. */
	memset(&setup, 0, sizeof(setup));
	setup.id = id;
	snprintf(setup.name, sizeof(setup.name), "%s", name);
	if (ioctl(fd, UI_DEV_SETUP, &setup) == 0) {
		if (ioctl(fd, UI_DEV_CREATE) < 0) {
			fprintf(stderr, "uinput-hold: %s: UI_DEV_CREATE: %s\n",
				name, strerror(errno));
			close(fd);
			return -1;
		}
		return fd;
	}
	fprintf(stderr, "uinput-hold: %s: UI_DEV_SETUP unsupported (%s), "
			"using the legacy ABI\n", name, strerror(errno));

	memset(&user, 0, sizeof(user));
	snprintf(user.name, sizeof(user.name), "%s", name);
	user.id = id;
	if (write(fd, &user, sizeof(user)) != (ssize_t)sizeof(user)) {
		fprintf(stderr, "uinput-hold: %s: write user_dev: %s\n", name, strerror(errno));
		close(fd);
		return -1;
	}
	if (ioctl(fd, UI_DEV_CREATE) < 0) {
		fprintf(stderr, "uinput-hold: %s: UI_DEV_CREATE: %s\n", name, strerror(errno));
		close(fd);
		return -1;
	}
	return fd;
}

static void emit(int fd, int type, int code, int value)
{
	/* Written from a flat 16-byte buffer rather than a struct, so the size
	 * handed to write(2) cannot be affected by struct padding. The uinput ABI
	 * on 32-bit is two 32-bit time words, then u16 type, u16 code, s32 value. */
	unsigned char raw[16];
	struct timeval tv;
	ssize_t n;

	gettimeofday(&tv, NULL);
	{
		unsigned short t = (unsigned short)type, c = (unsigned short)code;
		int v = value;
		memcpy(raw + 0, &tv.tv_sec, 4);
		memcpy(raw + 4, &tv.tv_usec, 4);
		memcpy(raw + 8, &t, 2);
		memcpy(raw + 10, &c, 2);
		memcpy(raw + 12, &v, 4);
	}
	n = write(fd, raw, sizeof(raw));
	if (n != (ssize_t)sizeof(raw))
		fprintf(stderr, "uinput-hold: write(type=%d code=%d value=%d) = %d, errno=%d (%s)\n",
			type, code, value, (int)n, errno, strerror(errno));
}

static void hold(struct vdev *d, int v)
{
	/* Only the EV_KEY event. uinput generates SYN_REPORT itself and rejects a
	 * userspace EV_SYN with EINVAL, so sending one loses the whole update. */
	emit(d->fd, EV_KEY, KEY_VOLUMEUP, v);
}

int main(int argc, char **argv)
{
	int seconds = argc > 1 ? atoi(argv[1]) : 20;
	const char *probe = argc > 2 ? argv[2] : NULL;
	struct vdev devs[2];
	int n = 0;

	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("uinput-hold: creating two devices that both advertise KEY_VOLUMEUP\n");

	devs[n].name = "Headset";
	devs[n].fd = make_dev(devs[n].name);
	if (devs[n].fd < 0)
		return 1;
	n++;

	devs[n].name = "gpio_keys.test";
	devs[n].fd = make_dev(devs[n].name);
	if (devs[n].fd < 0)
		return 1;
	n++;

	/* Give udev and any device scan a moment to see the new nodes. */
	sleep(3);
	printf("uinput-hold: holding KEY_VOLUMEUP on both\n");
	for (int i = 0; i < n; i++)
		hold(&devs[i], 1);
	sleep(1);
	printf("uinput-hold: held\n");

	/* The probe runs from in here rather than from a second adb shell, so the
	 * keys are provably still held while it runs. A detached background
	 * process is exactly the thing that silently died in earlier attempts. */
	if (probe) {
		/* fork + execvp rather than system(), because this recovery has no
		 * /bin/sh for system() to use. The child inherits stdout, so the
		 * probe's output lands in this log in order. */
		char *args[16];
		int nargs = 0;
		char *copy = strdup(probe);
		char *tok;
		pid_t pid;
		int status = -1;

		for (tok = strtok(copy, " "); tok && nargs < 15; tok = strtok(NULL, " "))
			args[nargs++] = tok;
		args[nargs] = NULL;

		printf("uinput-hold: running probe: %s\n", probe);
		fflush(stdout);
		pid = fork();
		if (pid == 0) {
			execvp(args[0], args);
			fprintf(stderr, "uinput-hold: exec %s: %s\n", args[0], strerror(errno));
			_exit(127);
		}
		if (pid > 0)
			waitpid(pid, &status, 0);
		free(copy);
		if (pid > 0 && WIFEXITED(status))
			printf("uinput-hold: probe exit status %d\n", WEXITSTATUS(status));
		else
			printf("uinput-hold: probe did not exit normally (raw %d)\n", status);
		fflush(stdout);
	}

	sleep(seconds);

	printf("uinput-hold: releasing\n");
	for (int i = 0; i < n; i++)
		hold(&devs[i], 0);
	sleep(1);
	for (int i = 0; i < n; i++) {
		ioctl(devs[i].fd, UI_DEV_DESTROY);
		close(devs[i].fd);
	}
	printf("uinput-hold: done\n");
	return 0;
}
