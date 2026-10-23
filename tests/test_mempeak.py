"""Contract tests for mempeak (whole-run peak memory monitor)."""
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import mempeak

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINUX = sys.platform.startswith("linux") and os.path.exists("/proc/self/statm")
MiB = 1024 * 1024
try:
    import psutil  # noqa: F401
    HAVE_PSUTIL = True
except ImportError:
    HAVE_PSUTIL = False


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


@unittest.skipUnless(LINUX or HAVE_PSUTIL, "needs /proc or psutil")
class CrossPlatformTests(unittest.TestCase):
    """Real processes, no mocks: runs on Linux, and on Windows/macOS when psutil is installed (CI)."""

    def test_in_process_peak(self):
        with mempeak.monitor(interval=0.01, print_summary=False) as m:
            base = mempeak.rss_bytes()
            buf = _touch(64)
            time.sleep(0.05)
            del buf
        self.assertIsNotNone(base)
        self.assertGreaterEqual(m.report.peak_bytes - base, 56 * MiB)
        if sys.platform == "win32":
            self.assertGreaterEqual(m.report["peak"]["commit_bytes"], 64 * MiB)

    def test_run_exit_code_and_peak(self):
        code = ("import sys, time\nb = bytearray(64 << 20)\nb[::4096] = b'x' * len(b[::4096])\n"
                "time.sleep(0.3)\nsys.exit(5)\n")
        rep, rc = mempeak.run_command([sys.executable, "-c", code], interval=0.01)
        self.assertEqual(rc, 5)
        self.assertEqual(rep["status"], "exited")
        self.assertGreaterEqual(rep.peak_bytes, 60 * MiB)

    def test_kill_above(self):
        code = "import time\nb = bytearray(300 << 20)\nb[::4096] = b'x' * len(b[::4096])\ntime.sleep(10)\n"
        t0 = time.monotonic()
        rep, rc = mempeak.run_command([sys.executable, "-c", code], interval=0.01, kill_above=64 * MiB)
        self.assertLess(time.monotonic() - t0, 8)
        self.assertNotEqual(rc, 0)
        self.assertTrue(rep["exit"]["killed_by_mempeak"])
        self.assertFalse(rep["exit"]["likely_oom"])
        self.assertEqual(rep["status"], "killed")


class VenvResolutionTests(unittest.TestCase):
    """`python -m mempeak run -- python ...` must run the child in mempeak's own venv, activated or not."""

    def test_bare_python_uses_the_interpreter_running_mempeak(self):
        with tempfile.TemporaryDirectory() as d:
            venv_dir = os.path.join(d, "venv")
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv_dir], check=True)
            bindir = "Scripts" if sys.platform == "win32" else "bin"
            venv_python = os.path.join(venv_dir, bindir, "python.exe" if sys.platform == "win32" else "python")
            env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")}
            child = "import sys; print('PREFIX=' + sys.prefix)"
            runs = {}
            for extra in ([], ["--no-resolve"]):
                r = subprocess.run([venv_python, os.path.join(ROOT, "mempeak.py"), "run", "-q"] + extra +
                                   ["--", "python", "-c", child], env=env, capture_output=True, text=True)
                line = [x for x in r.stdout.splitlines() if x.startswith("PREFIX=")]
                runs[bool(extra)] = (r.returncode, line[0][7:] if line else None, r.stderr)
            code, prefix, err = runs[False]
            self.assertEqual(code, 0, err)
            self.assertEqual(os.path.realpath(prefix), os.path.realpath(venv_dir))
            # literal mode keeps the old OS lookup, which does not land in the (unactivated) venv
            if runs[True][1] is not None:
                self.assertNotEqual(os.path.realpath(runs[True][1]), os.path.realpath(venv_dir))

    def test_explicit_paths_are_untouched(self):
        argv, note = mempeak.resolve_command([sys.executable, "-V"])
        self.assertEqual(argv, [sys.executable, "-V"])
        self.assertIsNone(note)
        argv, note = mempeak.resolve_command(["python3", "x.py"])
        self.assertEqual(argv, [sys.executable, "x.py"])
        self.assertIn("python3", note)


class WindowsCompatTests(unittest.TestCase):
    """Windows has no signal.SIGKILL, no OOM killer, and reads memory only through psutil."""

    @unittest.skipUnless(LINUX, "spawns a real child")
    def test_kill_above_without_sigkill(self):
        code = "import time\nb = bytearray(200 << 20); b[::4096] = b'x' * len(b[::4096]); time.sleep(5)\n"
        with mock.patch.object(mempeak, "_SIGKILL", None):
            rep, rc = mempeak.run_command([sys.executable, "-c", code], interval=0.01, kill_above=64 * MiB)
        self.assertEqual(rc, -signal.SIGTERM)          # the fallback TerminateProcess path
        self.assertTrue(rep["exit"]["killed_by_mempeak"])
        self.assertFalse(rep["exit"]["likely_oom"])
        self.assertEqual(rep["status"], "killed")

    def test_sigkill_attribute_is_optional(self):
        with open(mempeak.__file__, encoding="utf-8") as f:
            self.assertNotIn("signal.SIGKILL", f.read())

    def test_commit_peak_from_psutil_on_windows(self):
        info = types.SimpleNamespace(rss=10 * MiB, peak_wset=12 * MiB, peak_pagefile=30 * MiB)
        fake = types.SimpleNamespace(Process=lambda pid=None: types.SimpleNamespace(memory_info=lambda: info))
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(mempeak, "_LINUX", False), \
                mock.patch.object(mempeak, "_psutil", lambda: fake), mock.patch.dict(sys.modules, {"resource": None}):
            self.assertEqual(mempeak.commit_peak_bytes(), 30 * MiB)
            self.assertEqual(mempeak.rss_bytes(), 10 * MiB)
            with mempeak.monitor(interval=0.01, print_summary=False) as m:
                time.sleep(0.03)
        self.assertEqual(m.report["peak"]["commit_bytes"], 30 * MiB)
        self.assertEqual(m.report["peak"]["hwm_bytes"], 12 * MiB)
        self.assertIn("peak commit", m.report.summary())

    def test_missing_psutil_is_reported_not_silent(self):
        with mock.patch.object(mempeak, "_LINUX", False), mock.patch.object(mempeak, "_psutil", lambda: None), \
                mock.patch.object(sys, "platform", "win32"), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with mempeak.monitor(interval=0.01, print_summary=False) as m:
                pass
        self.assertIn("pip install psutil", err.getvalue())
        self.assertTrue(any("psutil" in n for n in m.report["notes"]))


class HelperTests(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(mempeak.parse_size("512M"), 512 * MiB)
        self.assertEqual(mempeak.parse_size("1.5GiB"), int(1.5 * 1024 * MiB))
        self.assertEqual(mempeak.parse_size("4096"), 4096)
        with self.assertRaises(ValueError):
            mempeak.parse_size("lots")


if __name__ == "__main__":
    unittest.main()
