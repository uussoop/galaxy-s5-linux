#!/usr/bin/env python3
"""Unit tests for matching a cu.usbmodem node to the phone's USB serial.

This exists because the match got it wrong twice, in two different ways, and
both times it was wrong in a way that looked like the phone was missing.

The first error was matching on the whole node name. macOS names the node
cu.usbmodem<serial><n>, where the trailing n only exists to separate two
identical devices. On this phone the real node is cu.usbmodem01f44ecab7141 --
the serial 01f44ecab714 is followed by a 1. So an equality test against the
serial matches nothing, forever, and the autocapture sat through an entire live
console window reporting that the phone had no node.

The second error was not filtering at all. The descriptor watcher's acm_nodes
listed every cu.usbmodem* on the machine, so TWRP's own node cu.usbmodem11102
appeared in the log attributed to the phone. The log then claimed the phone was
present while the phone was sitting in TWRP, which is a claim that cannot be
trusted even when it happens to come out right.

The rules under test, stated once so that a failure names itself:

  1. the serial is matched as a SUBSTRING, so the trailing disambiguator is
     tolerated;
  2. a node belonging to something else is excluded, even though it has the
     identical shape;
  3. no serial means no claim is being made, and returns nothing -- never
     everything.

The tests import the real functions from the real scripts, not copies. A test
against a copy of the logic is a test of the copy, and the copy is exactly what
has been wrong before.

Two of these are controls rather than assertions about correct behaviour: they
pin down the specific wrong implementations that were shipped, so that
reintroducing either one fails a named test instead of passing quietly.
"""
import importlib.util
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SERIAL = "01f44ecab714"          # the phone's USB descriptor serial number
TWRP_NODE = "cu.usbmodem11102"   # the recovery's node, seen on this Mac
LIVE_NODE = f"cu.usbmodem{SERIAL}1"  # the real node while pmOS runs


def _load(name):
    """Import a script by path without importing its neighbours."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class NodeMatching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.watch = _load("usb-descriptor-watch")
        cls.cap = _load("serial-autocapture")

    # ---- the live name, and why it is awkward -------------------------

    def test_live_node_carries_the_serial_as_a_substring(self):
        self.assertNotEqual(LIVE_NODE, SERIAL,
                            "premise: the live node name is not the serial")
        self.assertNotEqual(SERIAL, LIVE_NODE,
                            "premise: neither string is a suffix of the other")
        self.assertIn(SERIAL, LIVE_NODE)

    def test_control_endswith_matches_nothing(self):
        """The bug that made the autocapture miss a live console."""
        self.assertEqual([n for n in [LIVE_NODE] if n.endswith(SERIAL)], [],
                         "premise: endswith is the wrong matcher for this name")
        self.assertEqual([n for n in [LIVE_NODE] if n == SERIAL], [],
                         "premise: equality is also the wrong matcher")

    # ---- the watcher --------------------------------------------------

    def test_watcher_finds_the_live_node(self):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, LIVE_NODE).touch()
            self.assertEqual(self.watch._acm_nodes_in(d, serial=SERIAL),
                             [LIVE_NODE])

    def test_watcher_excludes_a_foreign_node(self):
        """TWRP's node has the identical shape and a different serial."""
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, TWRP_NODE).touch()
            pathlib.Path(d, LIVE_NODE).touch()
            got = self.watch._acm_nodes_in(d, serial=SERIAL)
            self.assertNotIn(TWRP_NODE, got,
                             "a recovery node must never be read as the phone")
            self.assertEqual(got, [LIVE_NODE])

    def test_watcher_claims_nothing_without_a_serial(self):
        """No serial must yield nothing, never everything."""
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, TWRP_NODE).touch()
            pathlib.Path(d, LIVE_NODE).touch()
            for serial in (None, "", 0):
                self.assertEqual(self.watch._acm_nodes_in(d, serial=serial),
                                 [], f"serial={serial!r} must match nothing")

    def test_watcher_returns_sorted_names(self):
        """Two identical devices get 1 and 2, and both may be present."""
        with tempfile.TemporaryDirectory() as d:
            for suffix in ("10", "2", "1"):
                pathlib.Path(d, f"cu.usbmodem{SERIAL}{suffix}").touch()
            got = self.watch._acm_nodes_in(d, serial=SERIAL)
            self.assertEqual(got, sorted(got))
            self.assertEqual(len(got), 3)

    def test_watcher_acm_nodes_delegates_to_the_tested_helper(self):
        """The public function must not have its own private filtering.

        An earlier version of acm_nodes ignored its serial argument entirely
        and globbed everything. This asserts the public entry point is a thin
        delegation, so the behaviour cannot diverge from what is tested here.
        """
        seen = {}

        def spy(directory="/dev", serial=None):
            seen["directory"] = directory
            seen["serial"] = serial
            return ["sentinel"]

        saved = self.watch._acm_nodes_in
        try:
            self.watch._acm_nodes_in = spy
            self.assertEqual(self.watch.acm_nodes(SERIAL), ["sentinel"])
        finally:
            self.watch._acm_nodes_in = saved
        self.assertEqual(seen, {"directory": "/dev", "serial": SERIAL})

    def test_control_unfiltered_glob_would_latch_onto_twrp(self):
        """Pin down the defect that was actually shipped and then fixed."""
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, TWRP_NODE).touch()
            unfiltered = sorted(p.name for p in pathlib.Path(d).glob("cu.usbmodem*"))
            self.assertIn(TWRP_NODE, unfiltered,
                          "premise: an unfiltered glob does include TWRP")
            self.assertEqual(self.watch._acm_nodes_in(d, serial=SERIAL), [],
                             "and the fixed filter correctly reports no phone")

    def test_missing_directory_is_not_an_error(self):
        """A glob over a directory that is not there yields nothing."""
        self.assertEqual(
            self.watch._acm_nodes_in("/nonexistent-s5-test-dir", serial=SERIAL),
            [])

    # ---- the autocapture ----------------------------------------------

    def test_autocapture_finds_the_live_node(self):
        self.assertEqual(
            self.cap._first_matching_node([f"/dev/{LIVE_NODE}"], SERIAL),
            f"/dev/{LIVE_NODE}")

    def test_autocapture_excludes_a_foreign_node(self):
        self.assertIsNone(
            self.cap._first_matching_node([f"/dev/{TWRP_NODE}"], SERIAL),
            "TWRP's node must not be mistaken for the phone's")

    def test_autocapture_prefers_the_phone_when_both_are_present(self):
        """Two nodes, one of them ours. Returning TWRP's would be a wrong
        answer that still type-checks and still returns a string."""
        nodes = [f"/dev/{TWRP_NODE}", f"/dev/{LIVE_NODE}"]
        self.assertEqual(self.cap._first_matching_node(nodes, SERIAL),
                         f"/dev/{LIVE_NODE}")

    def test_autocapture_claims_nothing_without_a_serial(self):
        for serial in (None, ""):
            self.assertIsNone(
                self.cap._first_matching_node([f"/dev/{LIVE_NODE}"], serial),
                f"serial={serial!r} must not match anything")

    def test_autocapture_returns_none_on_an_empty_list(self):
        self.assertIsNone(self.cap._first_matching_node([], SERIAL))


if __name__ == "__main__":
    unittest.main(verbosity=2)
