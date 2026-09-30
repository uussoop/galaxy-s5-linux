# S5 SSH launcher draft

The launcher is intended to be copied to the project `outputs/` directory after native SSH and the host key are verified. From there, double-click `Connect S5.command` or run it with an optional host argument (default: `galaxy-s5.local`). It always connects as `user` using the dedicated key in `work/galaxy-s5/ssh/s5_ed25519`.

Before use, pin the verified phone SSH host key in `work/galaxy-s5/ssh/known_hosts` under the name `codex-galaxy-s5`. The launcher requires that pin and will stop if it or the identity file is missing. It uses strict host-key checking and never falls back to a password or another SSH identity. The draft's relative project-root lookup is designed for its final location in `outputs/`.
