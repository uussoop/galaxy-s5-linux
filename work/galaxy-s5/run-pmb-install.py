"""Run pmbootstrap install with an in-memory random password via a PTY."""

import errno
import os
import pty
import secrets
import sys


args = [sys.executable, "/src/pmbootstrap/pmbootstrap.py", "--details-to-stdout"]
args += sys.argv[2:]
log_path = sys.argv[1]
pid, fd = pty.fork()
if pid == 0:
    os.execv(sys.executable, args)

password = secrets.token_urlsafe(32)
seen = b""
sent_first = sent_second = False
with open(log_path, "wb", buffering=0) as log:
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        log.write(chunk)
        seen = (seen + chunk)[-1024:]
        if not sent_first and b"Choose a password" in seen:
            os.write(fd, password.encode() + b"\n")
            sent_first = True
            seen = b""
        elif sent_first and not sent_second and b"Confirm password" in seen:
            os.write(fd, password.encode() + b"\n")
            sent_second = True
            seen = b""

_, status = os.waitpid(pid, 0)
exit_code = os.waitstatus_to_exitcode(status)
print("password prompts answered:", sent_first, sent_second)
print("pmbootstrap exit status:", exit_code)
sys.exit(exit_code)
