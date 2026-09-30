/* s5rootmount - bind a byte range of a block device to a loop device and mount
 * it, read-only. Built for this recovery task and nothing else.
 *
 * Why this exists: the installed postmarketOS root is not a partition of its
 * own. It is a nested MBR table inside /dev/block/mmcblk0p21, which the initramfs
 * exposes through device-mapper. Linux does not scan partitions inside
 * partitions, so TWRP cannot mount the root directly, and:
 *
 *   - toybox losetup -f answers "/dev/loop0: No such device or address",
 *     because this kernel registers loop devices dynamically and does not have
 *     the static 7:0 node;
 *   - there is no dmsetup, no fdisk, no /sbin/sh and no busybox in TWRP.
 *
 * The only way to get at the filesystem from a recovery with those gaps is to do
 * the loop ioctls directly, which is what this does. The offsets it is given
 * were read off the nested MBR rather than guessed.
 *
 * Safety: the mount is always MS_RDONLY, the loop device is opened O_RDONLY, and
 * nothing here ever writes to the underlying device. The helper is a tool for
 * reading a filesystem that finally boots; it must not be the thing that damages
 * it. "detach" exists so the mapping can be undone afterwards.
 *
 * Usage:
 *   s5rootmount mount  <blockdev> <offset_bytes> <mountpoint> [fstype]
 *   s5rootmount detach <loopdev> <mountpoint>
 *
 * Exit status is 0 only if the filesystem was mounted and is readable; every
 * failure prints why to stderr.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

/* From include/uapi/linux/loop.h. Spelled out rather than included because the
 * static musl target for arm-linux does not always ship that header, and the
 * values are ABI-frozen kernel constants. */
#define LOOP_CTL_GET_FREE 3
#define LOOP_SET_FD 6
#define LOOP_SET_OFFSET 7
#define LOOP_GET_STATUS64 9
#define LOOP_CLR_FD 11

struct loop_info64 {
	uint64_t lo_device;
	uint64_t lo_inode;
	uint64_t lo_rdevice;
	uint64_t lo_offset;
	uint64_t lo_sizelimit;
	uint32_t lo_number;
	uint32_t lo_encrypt_type;
	uint32_t lo_encrypt_key_size;
	uint32_t lo_flags;
	uint8_t lo_file_name[64];
	uint8_t lo_crypt_name[64];
	uint8_t lo_encrypt_key[32];
	uint64_t lo_init[2];
};

static int fail(const char *what)
{
	fprintf(stderr, "%s: %s\n", what, strerror(errno));
	return 1;
}

static int do_mount(const char *src, long long offset, const char *mnt,
		    const char *fstype)
{
	char loopname[32];
	struct loop_info64 info;
	struct stat st;
	int ctl, loop, srcfd;
	int idx;

	ctl = open("/dev/loop-control", O_RDWR);
	if (ctl < 0)
		return fail("open /dev/loop-control");
	idx = ioctl(ctl, LOOP_CTL_GET_FREE);
	close(ctl);
	if (idx < 0)
		return fail("LOOP_CTL_GET_FREE");

	/* The kernel registered the index just now, but recovery has no udev, so
	 * the node still has to be created by hand. */
	snprintf(loopname, sizeof(loopname), "/dev/loop%d", idx);
	unlink(loopname);
	if (mknod(loopname, S_IFBLK | 0660, makedev(7, idx)) < 0 && errno != EEXIST)
		return fail("mknod loop device");

	srcfd = open(src, O_RDONLY);
	if (srcfd < 0)
		return fail("open source device");
	loop = open(loopname, O_RDONLY);
	if (loop < 0)
		return fail("open loop device");

	if (ioctl(loop, LOOP_SET_FD, srcfd) < 0)
		return fail("LOOP_SET_FD");
	/* LOOP_SET_OFFSET counts 512-byte units, which is what the nested MBR
	 * entries were expressed in. */
	if (ioctl(loop, LOOP_SET_OFFSET, offset / 512) < 0)
		return fail("LOOP_SET_OFFSET");

	/* Read the offset back rather than trusting the ioctl: a silently wrong
	 * offset would mount the wrong bytes and every answer drawn from the
	 * result would be confidently wrong. */
	memset(&info, 0, sizeof info);
	if (ioctl(loop, LOOP_GET_STATUS64, &info) < 0)
		return fail("LOOP_GET_STATUS64");
	printf("loop: /dev/loop%d bound to %s at byte offset %llu\n", idx, src,
	       (unsigned long long)info.lo_offset);
	if ((long long)info.lo_offset != offset) {
		fprintf(stderr,
			"offset did not take: asked %lld, got %llu; refusing to mount\n",
			offset, (unsigned long long)info.lo_offset);
		return 1;
	}

	if (mount(loopname, mnt, fstype, MS_RDONLY, NULL) < 0)
		return fail("mount (read-only)");

	/* Prove the mount is really there and really readable, rather than
	 * reporting success on the strength of an ioctl. */
	if (stat(mnt, &st) < 0)
		return fail("stat mountpoint");
	close(loop);
	close(srcfd);
	printf("mounted: %s on %s type %s read-only\n", loopname, mnt, fstype);
	return 0;
}

static int do_detach(const char *loopdev, const char *mnt)
{
	int loop, rc = 0;

	if (umount2(mnt, MNT_DETACH) < 0 && errno != EINVAL) {
		rc = fail("umount2");
		fprintf(stderr, "  continuing: the loop device is being dropped anyway\n");
	}
	loop = open(loopdev, O_RDONLY);
	if (loop < 0) {
		if (!rc)
			return fail("open loop device");
		return rc;
	}
	if (ioctl(loop, LOOP_CLR_FD, 0) < 0 && !rc)
		rc = fail("LOOP_CLR_FD");
	close(loop);
	unlink(loopdev);
	if (!rc)
		printf("detached: %s unmounted from %s and released\n", loopdev, mnt);
	return rc;
}

int main(int argc, char **argv)
{
	if (argc >= 2 && strcmp(argv[1], "mount") == 0) {
		if (argc < 5) {
			fprintf(stderr,
				"usage: s5rootmount mount <blockdev> <offset_bytes> <mountpoint> [fstype]\n");
			return 2;
		}
		return do_mount(argv[2], atoll(argv[3]), argv[4],
				argc > 5 ? argv[5] : "ext4");
	}
	if (argc >= 2 && strcmp(argv[1], "detach") == 0) {
		if (argc < 4) {
			fprintf(stderr,
				"usage: s5rootmount detach <loopdev> <mountpoint>\n");
			return 2;
		}
		return do_detach(argv[2], argv[3]);
	}
	fprintf(stderr,
		"usage: s5rootmount mount|detach ...\n");
	return 2;
}
