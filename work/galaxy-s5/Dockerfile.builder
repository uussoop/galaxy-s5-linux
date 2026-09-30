FROM alpine:3.22
RUN apk add --no-cache python3 openssl git sudo bash coreutils util-linux multipath-tools procps tar curl ca-certificates e2fsprogs e2fsprogs-extra parted xz file patch make
RUN adduser -D -u 501 -h /home/pmos pmos && printf 'pmos ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/pmos && chmod 0440 /etc/sudoers.d/pmos
USER pmos
WORKDIR /home/pmos
CMD ["sleep", "infinity"]
