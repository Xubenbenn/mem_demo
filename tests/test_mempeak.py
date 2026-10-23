"""Contract tests for mempeak (whole-run peak memory monitor)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import mempeak

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINUX = sys.platform.startswith("linux") and os.path.exists("/proc/self/statm")
MiB = 1024 * 1024


def _touch(n_mib):
    buf = bytearray(n_mib * MiB)
    buf[::4096] = b"x" * len(buf[::4096])  # make the pages resident
    return buf


@unittest.skipUnless(LINUX, "needs /proc")
class InProcessTests(unittest.TestCase):
    def tearDown(self):
        if mempeak.active() is not None:
            mempeak.stop()

    def test_spike_between_samples_is_caught_by_kernel_hwm(self):
        with mempeak.monitor(interval=5, print_summary=False) as m:
            base = mempeak.rss_bytes()
            buf = _touch(64)
            del buf
        rep = m.report
        self.assertEqual(rep["status"], "finished")
        self.assertGreaterEqual(rep.peak_bytes - base, 60 * MiB)
        self.assertGreaterEqual(rep["peak"]["hwm_bytes"], rep["peak"]["sampled_bytes"])

    def test_phase_is_noop_without_monitor(self):
        os.environ.pop("MEMPEAK_PHASE_FILE", None)
        with mempeak.phase("nothing"):
            pass

        @mempeak.phase("decorated")
        def f():
            return 7
        self.assertEqual(f(), 7)
        self.assertIsNone(mempeak.active())

    def test_exact_phases_attribute_spike_to_its_own_phase(self):
        with mempeak.monitor(interval=5, exact_phases=True, print_summary=False) as m:
            with mempeak.phase("big"):
                buf = _touch(64)
                del buf
            with mempeak.phase("small"):
                time.sleep(0.01)
        phases = {p["name"]: p for p in m.report["phases"]}
        self.assertGreaterEqual(phases["big"]["peak_bytes"] - phases["small"]["peak_bytes"], 50 * MiB)
        self.assertTrue(phases["small"].get("exact"))
        # the global peak survives the per-phase VmHWM resets
        self.assertGreaterEqual(m.report.peak_bytes, phases["big"]["peak_bytes"])

    def test_python_heap_peak(self):
        with mempeak.monitor(interval=5, python_heap=True, print_summary=False) as m:
            with mempeak.phase("heap"):
                data = [bytes(1024) for _ in range(20000)]
                del data
        self.assertGreaterEqual(m.report["peak"]["python_heap_bytes"], 15 * MiB)
        self.assertGreaterEqual(m.report["phases"][0]["python_heap_peak_bytes"], 15 * MiB)

    def test_report_is_written_while_running_and_on_stop(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "peak.json")
            mempeak.start(interval=0.01, flush_every=0.02, out=out, print_summary=False)
            deadline = time.time() + 5
            while not os.path.exists(out) and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(mempeak.load(out)["status"], "running")
            rep = mempeak.stop()
            self.assertEqual(mempeak.load(out)["status"], "finished")
            self.assertEqual(json.loads(json.dumps(rep))["tool"], "mempeak")

    def test_profile_decorator_and_double_start(self):
        @mempeak.profile(interval=0.01, print_summary=False)
        def job():
            self.assertIsNotNone(mempeak.active())
            with self.assertRaises(RuntimeError):
                mempeak.start()
            return 1
        self.assertEqual(job(), 1)
        self.assertIsNone(mempeak.active())

    def test_exception_is_propagated_and_recorded(self):
        with self.assertRaises(KeyError):
            with mempeak.monitor(interval=0.01, print_summary=False) as m:
                raise KeyError("x")
        self.assertEqual(m.report["status"], "error: KeyError")


@unittest.skipUnless(LINUX, "needs /proc")
class ExternalTests(unittest.TestCase):
    def test_run_reports_child_peak_phases_and_exit_code(self):
        code = ("import sys; sys.path.insert(0, %r); import mempeak, time\n"
                "with mempeak.phase('alloc'):\n"
                "    b = bytearray(48 << 20); b[::4096] = b'x' * len(b[::4096]); time.sleep(0.15)\n"
                "sys.exit(5)\n" % ROOT)
        rep, rc = mempeak.run_command([sys.executable, "-c", code], interval=0.01)
        self.assertEqual(rc, 5)
        self.assertEqual(rep["exit"]["code"], 5)
        self.assertGreaterEqual(rep.peak_bytes, 48 * MiB)
        self.assertEqual([p["name"] for p in rep["phases"]], ["alloc"])
        self.assertGreaterEqual(rep["phases"][0]["peak_bytes"], 40 * MiB)

    def test_kill_above_stops_child_and_flags_it(self):
        code = "import time\nb = bytearray(200 << 20); b[::4096] = b'x' * len(b[::4096]); time.sleep(5)\n"
        rep, rc = mempeak.run_command([sys.executable, "-c", code], interval=0.01, kill_above=64 * MiB)
        self.assertEqual(rc, -9)
        self.assertTrue(rep["exit"]["killed_by_mempeak"])
        self.assertFalse(rep["exit"]["likely_oom"])

    def test_child_processes_are_summed(self):
        code = ("import subprocess, sys\n"
                "kid = 'b = bytearray(40 << 20); b[::4096] = b\"x\" * len(b[::4096]); import time; time.sleep(0.4)'\n"
                "ps = [subprocess.Popen([sys.executable, '-c', kid]) for _ in range(2)]\n"
                "[p.wait() for p in ps]\n")
        rep, rc = mempeak.run_command([sys.executable, "-c", code], interval=0.01)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(rep["peak"]["max_processes"], 3)
        self.assertGreaterEqual(rep["peak"]["sampled_bytes"], 80 * MiB)

    def test_cli_run_compare_and_gates(self):
        script = os.path.join(ROOT, "examples", "peak_demo.py")
        with tempfile.TemporaryDirectory() as d:
            before, after = os.path.join(d, "b.json"), os.path.join(d, "a.json")
            cli = [sys.executable, "-m", "mempeak"]
            for out, mode in ((before, "before"), (after, "after")):
                r = subprocess.run(cli + ["run", "-q", "--out", out, "--", sys.executable, script,
                                          "--mode", mode, "--mb", "40"], cwd=ROOT, capture_output=True)
                self.assertEqual(r.returncode, 0, r.stderr)
            text, rows = mempeak.compare(before, after)
            self.assertLess(rows[0]["delta"], -40 * MiB)
            self.assertIn("phase transform", text)
            r = subprocess.run(cli + ["compare", after, before, "--fail-if-higher", "5"], cwd=ROOT,
                               capture_output=True)
            self.assertEqual(r.returncode, 3)
            r = subprocess.run(cli + ["run", "-q", "--fail-above", "10M", "--", sys.executable, script,
                                      "--mb", "40"], cwd=ROOT, capture_output=True)
            self.assertEqual(r.returncode, 3)


class HelperTests(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(mempeak.parse_size("512M"), 512 * MiB)
        self.assertEqual(mempeak.parse_size("1.5GiB"), int(1.5 * 1024 * MiB))
        self.assertEqual(mempeak.parse_size("4096"), 4096)
        with self.assertRaises(ValueError):
            mempeak.parse_size("lots")


if __name__ == "__main__":
    unittest.main()
