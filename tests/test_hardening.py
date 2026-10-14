"""Adversarial regressions for the v2.1 audit (no application hooks invoked)."""
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
import weakref

import memwatch as mw
import _memwatch_core as core

try:
    import numpy as np
except ImportError:
    np = None


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        for key in list(os.environ):
            if key.startswith("MEMWATCH_"):
                del os.environ[key]
        os.environ["MEMWATCH_AUTO"] = "0"
        mw.uninstall()
        mw.install(trace=False, on_signal=False, write_report=False, brief=False,
                   roots_scope="tracked", count_objects=False, max_seconds=10,
                   include_threads=False, include_asyncio=False)

    def tearDown(self):
        mw.uninstall()
        self.env.stop()

    def scan(self, obj, **options):
        cfg = dict(mw.DEFAULT, max_seconds=10, top=100)
        cfg.update(options)
        return core._scan_roots([("tracked:target", obj, "tracked")], cfg)

    def assert_payload(self, result, payload, typename="builtins.bytearray"):
        rows = {row["type"]: row for row in result["types"]}
        self.assertIn(typename, rows)
        self.assertEqual(rows[typename]["bytes"], sys.getsizeof(payload))
        self.assertEqual(rows[typename]["count"], 1)
        self.assertTrue(result["coverage"]["graph"]["complete"])

    def test_nested_function_closure_without_global_namespace(self):
        payload = bytearray(1_000_000)
        def make_callback(data):
            return lambda: data
        # A function retains its globals as well; scanning those would pull in a
        # sentinel of a distinct type and almost the entire interpreter graph.
        fn = make_callback(payload)
        result = self.scan({"callback": fn})
        self.assert_payload(result, payload)
        self.assertNotIn("builtins.module", [r["type"] for r in result["types"]])
        self.assertLess(result["totals"]["objects"], 40)

    def test_function_defaults_kwdefaults_and_attribute_cache_deduplicate(self):
        payload = bytearray(1234)
        def fn(arg=payload, *, option=payload):
            return arg, option
        fn.cache = payload
        result = self.scan([fn])
        self.assert_payload(result, payload)

    def test_module_function_attribute_cache_is_a_root(self):
        payload = bytearray(4567)
        def fn():
            pass
        fn.cache = payload
        cfg = dict(mw.DEFAULT, max_seconds=10, top=100)
        result = core._scan_roots(list(core._root_members("module.fn", fn, "module")), cfg)
        self.assert_payload(result, payload)

    def test_class_descriptors_retain_defaults_without_calling_them(self):
        payload = bytearray(8765)
        class C:
            @staticmethod
            def action(value=payload):
                raise AssertionError("must not run action")
            @classmethod
            def action2(cls, value=payload):
                raise AssertionError("must not run classmethod")
            @property
            def lazy(self, value=payload):
                raise AssertionError("must not evaluate property")
        cfg = dict(mw.DEFAULT, max_seconds=10, top=100)
        result = core._scan_roots(list(core._root_members("C", C, "module")), cfg)
        self.assert_payload(result, payload)

    def test_class_reference_in_container_exposes_data_not_module(self):
        payload = bytearray(7654)
        class C:
            cache = payload
        self.assert_payload(self.scan([C]), payload)

    def test_function_attributes_are_not_materialized_by_observation(self):
        def fn():
            pass
        # __dict__ and __annotations__ are lazily allocated; do not access them.
        dictionaries = {id(x) for x in gc.get_referents(fn) if type(x) is dict}
        self.scan([fn])
        self.assertEqual(dictionaries, {id(x) for x in gc.get_referents(fn) if type(x) is dict})

    def test_nested_function_graph_respects_object_budget(self):
        payload = bytearray(9876)
        fn = lambda: payload
        result = self.scan([fn], max_objects=2)
        self.assertEqual(result["totals"]["objects"], 2)
        self.assertFalse(result["coverage"]["graph"]["complete"])
        self.assertIsNone(result["totals"]["exclusive_bytes"])

    def test_nested_function_report_does_not_retain_payload(self):
        class Payload:
            pass
        value = Payload()
        ref = weakref.ref(value)
        fn = (lambda item: lambda: item)(value)
        result = self.scan([fn])
        del value, fn
        gc.collect()
        self.assertIsNone(ref())
        json.dumps(result)

    @unittest.skipUnless(np is not None, "NumPy not installed")
    def test_numpy_subclass_attributes_are_measured(self):
        class Tagged(np.ndarray):
            pass
        owner = np.zeros(5, dtype=np.uint8)
        value = owner.view(Tagged)
        value.payload = bytearray(1_000_000)
        result = self.scan([owner, value])
        self.assert_payload(result, value.payload)
        self.assertEqual(result["totals"]["native_bytes"], owner.nbytes)

    @unittest.skipUnless(np is not None, "NumPy not installed")
    def test_numpy_subclass_slots_and_object_elements_count_once(self):
        class Tagged(np.ndarray):
            __slots__ = ("payload",)
        owner = np.empty(1, dtype=object)
        value = owner.view(Tagged)
        value.payload = bytearray(12345)
        owner[0] = value.payload
        result = self.scan(value)
        self.assert_payload(result, value.payload)
        self.assertEqual(result["totals"]["native_bytes"], owner.nbytes)

    def test_proxy_resolution_error_invalidates_all_coverage(self):
        class Broken:
            def _get_current_object(self):
                raise ValueError("backend failure")
        result = self.scan(Broken())
        self.assertFalse(result["coverage"]["graph"]["complete"])
        self.assertIn("proxy_resolution_error", result["coverage"]["graph"]["reasons"])
        self.assertIn("proxy_resolution_error", result["census_meta"]["reasons"])
        self.assertFalse(result["census_meta"]["complete_for_scope"])
        self.assertTrue(result["proxy_totals"]["truncated"])
        self.assertIsNone(result["totals"]["exclusive_bytes"])

    def test_foreign_proxy_alias_is_partial_even_with_current_target(self):
        class Proxy:
            def _get_current_object(self):
                return bytearray(10)
        proxy = Proxy()
        cfg = dict(mw.DEFAULT, max_seconds=10)
        result = core._scan_roots([("current", [proxy], "frame"), ("foreign", [proxy], "frame")],
                                 cfg, diag={"root_contexts": {"current": True, "foreign": False}})
        self.assertFalse(result["coverage"]["graph"]["complete"])
        self.assertIn("proxy_context_unavailable", result["coverage"]["graph"]["reasons"])
        self.assertIsNone(result["totals"]["exclusive_bytes"])

    def test_sizeof_failure_invalidates_graph_and_records_reason(self):
        class Broken:
            def __sizeof__(self):
                raise ValueError("cannot size")
        result = self.scan(Broken(), allow_custom_sizeof=True)
        self.assertFalse(result["coverage"]["graph"]["complete"])
        self.assertIn("measurement_error", result["coverage"]["graph"]["reasons"])
        self.assertIn("sizeof_error:ValueError", result["coverage"]["native"]["notes"])
        self.assertIsNone(result["totals"]["exclusive_bytes"])

    def test_gc_only_sizeof_failure_invalidates_census_not_graph(self):
        class Broken:
            def __sizeof__(self):
                raise ValueError("cannot size")
        cfg = dict(mw.DEFAULT, max_seconds=10, allow_custom_sizeof=True)
        result = core._scan_roots([], cfg, gc_pool=[Broken()])
        self.assertTrue(result["coverage"]["graph"]["complete"])
        self.assertFalse(result["census_meta"]["complete_for_scope"])
        self.assertIn("measurement_error", result["census_meta"]["reasons"])

    def test_cancelled_census_is_not_reported_as_timeout(self):
        stop = threading.Event()
        stop.set()
        cfg = dict(mw.DEFAULT, max_seconds=10, _stop=stop)
        result = core._scan_roots([], cfg, gc_pool=[[]])
        self.assertIn("cancelled", result["census_meta"]["reasons"])
        self.assertNotIn("max_seconds", result["census_meta"]["reasons"])

    def test_metaclass_lookup_hooks_are_not_executed(self):
        calls = []
        class Meta(type):
            def __getattribute__(cls, name):
                if name in ("__mro__", "__flags__", "__dict__"):
                    calls.append(name)
                    raise AssertionError("metaclass hook")
                return super().__getattribute__(name)
        class Safe(metaclass=Meta):
            pass
        result = self.scan(Safe())
        self.assertTrue(result["coverage"]["graph"]["complete"])
        self.assertEqual(calls, [])

    def test_metaclass_properties_are_bypassed_by_base_descriptors(self):
        calls = []
        class Meta(type):
            @property
            def __dict__(cls):
                calls.append("dict-property")
                raise AssertionError("metaclass descriptor")
            @property
            def __mro__(cls):
                calls.append("mro-property")
                raise AssertionError("metaclass descriptor")
            @property
            def __module__(cls):
                calls.append("module-property")
                raise AssertionError("metaclass descriptor")
        class Safe(metaclass=Meta):
            pass
        result = self.scan(Safe())
        self.assertTrue(result["coverage"]["graph"]["complete"])
        self.assertEqual(calls, [])

    def test_module_dict_property_is_not_evaluated_by_root_collection(self):
        calls = []
        class Module(types.ModuleType):
            @property
            def __dict__(self):
                calls.append("module-property")
                raise AssertionError("module descriptor")
        module = Module("safe_module")
        module.__file__ = os.path.join(os.getcwd(), "safe_module.py")
        module.payload = bytearray(1357)
        cfg = dict(mw.DEFAULT, root_paths=[os.getcwd()], max_seconds=10, top=100)
        with mock.patch.dict(sys.modules, {"safe_module": module}):
            rows = [row for row in core._roots_from_modules(cfg) if row[0].startswith("safe_module.")]
        result = core._scan_roots(rows, cfg)
        self.assert_payload(result, module.payload)
        self.assertEqual(calls, [])

    def test_preview_false_redacts_dictionary_keys_and_values_in_paths(self):
        class Proxy:
            def _get_current_object(self):
                return memoryview(b"SECRET_VALUE_789")
        result = self.scan({"SECRET_KEY_123": Proxy()}, preview=False)
        encoded = json.dumps(result)
        self.assertNotIn("SECRET_KEY_123", encoded)
        self.assertNotIn("SECRET_VALUE_789", encoded)
        self.assertIn("entry#0", encoded)

    def test_preview_true_preserves_useful_dictionary_key_paths(self):
        result = self.scan({"useful_key": memoryview(b"x")}, preview=True)
        self.assertIn("useful_key", json.dumps(result["buffers"]))

    def test_report_directory_ignores_non_reports_and_invalid_timestamps(self):
        with tempfile.TemporaryDirectory() as td:
            for i, data in enumerate(([], None, "text", {"ts": "2026"}, {"ts": False},
                                      {"ts": float("nan")}, {"ts": 2**2048})):
                Path(td, "%d.json" % i).write_text(json.dumps(data))
            Path(td, "valid.json").write_text(json.dumps({"label": "valid", "seq": 4, "ts": 42}))
            self.assertEqual(mw.report(td)["label"], "valid")
            self.assertEqual(mw.report(td, seq=4)["label"], "valid")

    def test_capture_reuses_inferred_paths_without_module_rediscovery(self):
        expected = list(mw._STATE["config"]["root_paths"])
        with mock.patch.object(mw, "_auto_root_paths", side_effect=AssertionError("rediscovery")):
            result = mw.snapshot("cached-paths", profile="counters", fail_silent=False)
        self.assertEqual(result["root_paths"], expected)
        self.assertEqual(result["errors"], [])

    def test_explicit_none_refreshes_paths_for_only_this_capture(self):
        original = list(mw._STATE["config"]["root_paths"])
        path = os.path.realpath("/explicit-refresh-test")
        with mock.patch.object(mw, "_auto_root_paths", return_value=[path]) as infer:
            result = mw.snapshot("refresh-paths", profile="counters", root_paths=None)
        infer.assert_called_once_with()
        self.assertEqual(result["root_paths"], [path])
        self.assertEqual(mw._STATE["config"]["root_paths"], original)

    def test_reinstall_refreshes_auto_path_configuration(self):
        path = os.path.realpath("/reinstall-refresh-test")
        with mock.patch.object(mw, "_auto_root_paths", return_value=[path]) as infer:
            config = mw.install()
        infer.assert_called_once_with()
        self.assertEqual(config["root_paths"], [path])

    def test_isolated_benchmark_smoke(self):
        root = Path(mw.__file__).parent
        with tempfile.TemporaryDirectory() as td:
            output = Path(td, "benchmark.json")
            proc = subprocess.run([sys.executable, str(root / "benchmarks/checkpoint_overhead.py"),
                                   "--objects", "3", "--trials", "1", "--profile", "counters",
                                   "--output", str(output)], capture_output=True, text=True, timeout=30,
                                  env=dict(os.environ, MEMWATCH_AUTO="0"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(output.read_text())
            self.assertEqual(len(data["samples"]), 1)
            self.assertEqual(data["samples"][0]["objects_requested"], 3)
            self.assertEqual(data["samples"][0]["errors"], [])

    def test_sites_run_before_graph_can_exhaust_budget(self):
        import tracemalloc
        mw.install(trace=True)
        mw.track("budget-payload", bytearray(100))
        order = []
        real_scan = mw._scan_roots
        def scan(*args, **kwargs):
            order.append("graph")
            # Consume the remaining graph budget without timing sleeps.
            args[1]["_budget"].deadline = 0
            return real_scan(*args, **kwargs)
        def sites(*args, **kwargs):
            order.append("sites")
            return {"status": "complete", "rows": []}
        with mock.patch.object(mw, "_phase_sites", side_effect=sites), \
             mock.patch.object(mw, "_scan_roots", side_effect=scan):
            result = mw.mark_all("sites-first", roots_scope="tracked")
        self.assertTrue(tracemalloc.is_tracing())
        self.assertEqual(order, ["sites", "graph"])
        self.assertEqual(result["sites"]["status"], "complete")
        self.assertTrue(result["holders_truncated"])


@unittest.skipUnless(hasattr(os, "fork"), "POSIX fork only")
class ForkTests(unittest.TestCase):
    def run_child(self, body):
        path = str(Path(mw.__file__).parent)
        code = "import sys; sys.path.insert(0, %r)\n" % path + body
        env = dict(os.environ, MEMWATCH_AUTO="0")
        proc = subprocess.run([sys.executable, "-S", "-c", code], cwd=path,
                              env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_fork_resets_locked_probe_and_preserves_parent_and_tracking(self):
        self.run_child('''
import os, signal, threading
import memwatch as m
m.install(trace=False, on_signal=False, profile="counters", count_objects=False,
          write_report=False, brief=False, fail_silent=False)
x = []
m.track("payload", x)
parent = m.snapshot("parent")
ready, release = threading.Event(), threading.Event()
def holder():
    with m._STATE["lock"], m._STATE["lifecycle"]:
        ready.set()
        release.wait(15)
t = threading.Thread(target=holder)
t.start(); assert ready.wait(5)
pid = os.fork()
if pid == 0:
    signal.alarm(10)
    assert not m.is_installed()
    assert not m._STATE["workers"]
    assert m._STATE["prev_snapshot"] is None
    child = m.snapshot("child")
    assert child["seq"] == 1 and child["pid"] == os.getpid(), child
    assert child["session"] != parent["session"]
    assert child["forked_from_pid"] == os.getppid()
    assert m._STATE["tracked"]["payload"] is x
    m.uninstall()
    os._exit(0)
_, status = os.waitpid(pid, 0)
release.set(); t.join(5)
assert status == 0, status
assert m.is_installed() and m._STATE["tracked"]["payload"] is x
assert m.snapshot("parent-again")["session"] == parent["session"]
m.uninstall()
''')

    def test_fork_inside_sink_does_not_release_replaced_lock(self):
        self.run_child('''
import os, signal
import memwatch as m
m.install(trace=False, on_signal=False, profile="counters", count_objects=False,
          write_report=False, brief=False, fail_silent=False)
child_pid = []
def sink(rep):
    child_pid.append(os.fork())
result = m.snapshot("fork-in-sink", sink=sink)
if child_pid[0] == 0:
    signal.alarm(10)
    assert result.get("skipped") == "cancelled", result
    followup = m.snapshot("child-followup")
    assert followup.get("schema_version") == 2, followup
    m.uninstall(); os._exit(0)
_, status = os.waitpid(child_pid[0], 0)
assert status == 0, status
assert result.get("schema_version") == 2, result
m.uninstall()
''')

    def test_fork_restores_only_monitor_owned_signal_and_profile_hooks(self):
        self.run_child('''
import os, signal, sys
import memwatch as m
original = signal.getsignal(signal.SIGUSR1)
m.install(trace=False, on_signal=True, at="never_called_business_function",
          count_objects=False, write_report=False)
assert m._STATE["signal_installed"] and m._STATE["at_hook"] is not None
pid = os.fork()
if pid == 0:
    signal.alarm(10)
    assert not m.is_installed()
    assert not m._STATE["signal_installed"] and m._STATE["at_hook"] is None
    assert signal.getsignal(signal.SIGUSR1) == original
    assert sys.getprofile() is None
    os._exit(0)
_, status = os.waitpid(pid, 0)
assert status == 0, status
assert m._STATE["signal_installed"] and m._STATE["at_hook"] is not None
m.uninstall()
''')


if __name__ == "__main__":
    unittest.main()
