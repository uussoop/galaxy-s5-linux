import contextlib
import io
import argparse
import subprocess
import unittest
from unittest import mock

import wifi_migrate as wifi


class MigrationTests(unittest.TestCase):
    def test_quoted_ssid_and_psk_are_canonicalized(self):
        fake_secret = "p" * 10
        raw = ('network={\nssid="caf\\xc3\\xa9#net"\npsk="' + fake_secret +
               '"\nkey_mgmt=WPA-PSK\nscan_ssid=1\n}\n').encode()
        profile = wifi.parse_networks(raw)[0]
        converted = wifi.convert_profile(profile)
        self.assertIn(b"ssid=636166c3a9236e6574", converted)
        self.assertIn(b"scan_ssid=1", converted)
        self.assertNotIn(b"caf\\x", converted)

    def test_hex_ssid_and_raw_psk(self):
        raw = ("network={\nssid=74657374\npsk=" + "a" * 64 + "\n}\n").encode()
        converted = wifi.convert_profile(wifi.parse_networks(raw)[0])
        self.assertIn(b"ssid=74657374", converted)
        self.assertIn(b"psk=" + b"a" * 64, converted)

    def test_unsupported_enterprise_and_wep_are_rejected(self):
        for fields in ("key_mgmt=WPA-EAP\nidentity=example\n",
                       "key_mgmt=NONE\nwep_key0=example\n"):
            profile = wifi.parse_networks(("network={\nssid=74657374\n" + fields + "}\n").encode())[0]
            with self.assertRaises(wifi.MigrationError):
                wifi.convert_profile(profile)

    def test_inspect_never_discloses_psk(self):
        fake_secret = "q" * 12
        profile = wifi.parse_networks(("network={\nssid=74657374\npsk=\"" + fake_secret +
                                       "\"\n}\n").encode())[0]
        out = io.StringIO()
        with mock.patch.object(wifi, "saved_profiles", return_value=[profile]):
            with contextlib.redirect_stdout(out):
                wifi.command_inspect(None)
        self.assertIn("test", out.getvalue())
        self.assertNotIn(fake_secret, out.getvalue())

    def test_mounted_stage_must_be_ram(self):
        wifi.check_ram_stage([("tmpfs", "/tmp", "tmpfs")])
        with self.assertRaises(wifi.MigrationError):
            wifi.check_ram_stage([("/dev/block/x", "/tmp", "ext4")])

    def test_secret_uses_raw_stdin_never_argv_or_pty(self):
        fake_secret = b"synthetic-credential-bytes"
        with mock.patch.object(wifi.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
            wifi.adb_with_input(fake_secret, "cat > /tmp/codex-s5-wifi/config")
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:4], ("adb", "-s", wifi.SERIAL, "exec-in"))
        self.assertNotIn("shell", arguments)
        self.assertNotIn("-T", arguments)
        self.assertNotIn(fake_secret.decode(), " ".join(arguments))
        self.assertEqual(run.call_args.kwargs["input"], fake_secret)

    def test_ram_readback_mismatch_is_rejected_without_data(self):
        with mock.patch.object(wifi, "adb_capture", return_value=b"different"):
            with self.assertRaisesRegex(wifi.MigrationError, "did not match") as exc:
                wifi.verify_ram_file(b"synthetic", wifi.STAGE_FILE)
        self.assertNotIn("synthetic", str(exc.exception))

    def test_completion_marker_waits_for_this_operation(self):
        with mock.patch.object(wifi, "adb_capture", side_effect=[b"old", b"new"]), \
                mock.patch.object(wifi.time, "sleep"):
            wifi.wait_for_marker(wifi.STAGE_MARKER, "new", seconds=1)

    def test_guard_command_aborts_after_failed_check(self):
        result = subprocess.run(["sh", "-c", "set -eu; false; printf guard-not-respected"],
                                capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_commit_keeps_ram_stage_if_installed_copy_differs(self):
        args = argparse.Namespace(mountpoint="/mnt/native", expected_device="/dev/block/test")
        calls = []

        def fake_adb(*parts):
            calls.append(parts)
            if parts == ("exec-out", "cat", "/mnt/native/etc/os-release"):
                return b"ID=postmarketos\n"
            if parts == ("exec-out", "cat", "/mnt/native/etc/init.d/s5-wifi"):
                return b"#!/sbin/openrc-run\n"
            if parts == ("exec-out", "cat", wifi.STAGE_FILE):
                return b"synthetic-profile\n"
            if parts == ("exec-out", "cat", wifi.COMMIT_MARKER):
                return b"nonce"
            if parts == ("exec-out", "cat", "/mnt/native" + wifi.NATIVE_CONFIG):
                return b"different\n"
            raise AssertionError(f"Unexpected ADB call: {parts}")

        with mock.patch.object(wifi, "mounted_filesystems", return_value=[
                ("/dev/block/test", "/mnt/native", "ext4")]), \
                mock.patch.object(wifi, "adb_capture", side_effect=fake_adb), \
                mock.patch.object(wifi, "adb_with_input"), \
                mock.patch.object(wifi.secrets, "token_hex", return_value="nonce"):
            with self.assertRaisesRegex(wifi.MigrationError, "did not match"):
                wifi.command_commit(args)
        self.assertFalse(any("rm -f" in " ".join(call) for call in calls))


if __name__ == "__main__":
    unittest.main()
