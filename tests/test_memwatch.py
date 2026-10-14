"""Deterministic accounting, budget, lifecycle and public API regression tests.

Run with: MEMWATCH_AUTO=0 python -m unittest discover -s tests -v
Werkzeug/NumPy cases are real integration tests, skipped only when not installed.
"""
import asyncio
import contextlib
import gc
import io
from importlib.metadata import version
import json
import mmap
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import types
import unittest
from unittest import mock
import weakref

import memwatch as mw

try:
    import numpy as np
except ImportError:
    np = None
try:
    from werkzeug.local import LocalProxy
    WERKZEUG_MODERN = tuple(int(x) for x in version("werkzeug").split(".")[:2]) >= (2, 2)
except ImportError:
    LocalProxy = None
    WERKZEUG_MODERN = False


def context_proxy(variable):
    # Direct ContextVar/LocalStack constructors were added in Werkzeug 2.2.
    # Exercise the documented callable backend on 2.0, not an invalid proxy.
    if WERKZEUG_MODERN:
        return LocalProxy(variable)
    def lookup():
        try:
            return variable.get()
        except LookupError:
            raise RuntimeError("unbound ContextVar") from None
    return LocalProxy(lookup)


class Proxy:
    def __init__(self, target):
        self.target = target
        self.calls = 0

    def _get_current_object(self):
        self.calls += 1
        return self.target


class Unbound:
    def _get_current_object(self):
        raise RuntimeError("no context")


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        for key in list(os.environ):
            if key.startswith("MEMWATCH_"):
                del os.environ[key]
        os.environ["MEMWATCH_AUTO"] = "0"
        mw.uninstall()
        self.temp = tempfile.TemporaryDirectory()
        mw.install(trace=False, on_signal=False, include_threads=False, include_asyncio=False,
                   roots_scope="tracked", write_report=False, count_objects=False,
                   brief=False, max_seconds=10, out_dir=self.temp.name)

    def tearDown(self):
        mw.uninstall()
        self.temp.cleanup()
        self.env.stop()

    def scan(self, roots, **opts):
        cfg = dict(mw.DEFAULT, max_seconds=10, top=100)
        cfg.update(opts)
        return mw._scan_roots([(name, obj, "tracked") for name, obj in roots], cfg)

    def capture(self, **opts):
        return mw.snapshot("test", **opts)


class AccountingTests(Base):
    def test_exact_builtin_sizes_and_dict_keys(self):
        data = {"distinct_key_" + str(i): bytearray(i + 10) for i in range(10)}
        result = self.scan([("x", data)])
        expected = sys.getsizeof(data) + sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in data.items())
        self.assertEqual(result["totals"]["accounted_bytes"], expected)
        self.assertEqual(result["totals"]["objects"], 21)
        self.assertFalse(result["holders_truncated"])

    def test_aliases_preserved_and_order_independent(self):
        x = [bytearray(1000)]
        a = self.scan([("z", x), ("a", x)])
        b = self.scan([("a", x), ("z", x)])
        self.assertEqual(a["totals"], b["totals"])
        self.assertEqual(a["holders"], b["holders"])
        self.assertEqual(len(a["holders"]), 1)
        self.assertEqual(a["holders"][0]["aliases"], ["a", "z"])

    def test_shared_subgraph_is_not_greedily_assigned(self):
        child = bytearray(2000)
        a, b = [child], {"data": child}
        r = self.scan([("a", a), ("b", b)])
        reverse = self.scan([("b", b), ("a", a)])
        self.assertEqual(r["totals"], reverse["totals"])
        self.assertEqual(r["holders"], reverse["holders"])
        self.assertEqual(r["totals"]["shared_bytes"], sys.getsizeof(child))
        self.assertGreater(sum(h["bytes"] for h in r["holders"]), r["totals"]["accounted_bytes"])
        self.assertEqual(sum(t["bytes"] for t in r["types"]), r["totals"]["accounted_bytes"])

    def test_cycles_count_once(self):
        x = []
        x.append(x)
        r = self.scan([("x", x)])
        self.assertEqual(r["totals"]["objects"], 1)
        self.assertEqual(r["totals"]["accounted_bytes"], sys.getsizeof(x))

    def test_top_only_limits_display(self):
        roots = [(str(i), bytearray(i + 20)) for i in range(10)]
        r = self.scan(roots, top=1)
        self.assertEqual(len(r["holders"]), 1)
        self.assertEqual(r["holders_total"], 10)
        self.assertEqual(r["holders_omitted"], 9)
        self.assertEqual(r["totals"]["accounted_bytes"], sum(sys.getsizeof(x) for _, x in roots))

    def test_custom_hooks_are_not_called_by_default(self):
        calls = []
        class Hostile:
            @property
            def __class__(self):
                calls.append("class")
                raise AssertionError("forwarded class")
            def __repr__(self):
                calls.append("repr")
                raise AssertionError("repr")
            def __len__(self):
                calls.append("len")
                raise AssertionError("len")
            def __sizeof__(self):
                calls.append("sizeof")
                return 10_000
        obj = Hostile()
        r = self.scan([("x", obj)])
        self.assertTrue(r["holders"])
        self.assertEqual(calls, [])
        self.assertGreater(mw._sizeof(obj, allow_custom=True), 9999)
        self.assertEqual(calls, ["sizeof"])

    def test_non_gc_tracked_bytes_appear_in_types(self):
        x = {"payload": bytearray(1024)}
        r = self.scan([("x", x)])
        rows = {r["type"]: r for r in r["types"]}
        self.assertFalse(gc.is_tracked(x))
        self.assertEqual(rows["builtins.bytearray"]["bytes"], sys.getsizeof(x["payload"]))
        self.assertEqual(r["census_meta"]["scope"], "reachable_graph")

    def test_census_union_does_not_recount_reachable_objects(self):
        x, y = [], bytearray(10)
        cfg = dict(mw.DEFAULT, max_seconds=10, top=100)
        r = mw._scan_roots([("x", x, "tracked")], cfg, gc_pool=[x, y])
        self.assertEqual(r["census_meta"]["bytes"], sys.getsizeof(x) + sys.getsizeof(y))
        self.assertEqual(r["census_meta"]["objects"], 2)

    def test_imported_aliases_and_private_module_are_kept(self):
        payload = bytearray(1000)
        mod1, mod2 = types.ModuleType("_mw_original"), types.ModuleType("mw_importer")
        for mod in (mod1, mod2):
            mod.__file__ = os.path.join(self.temp.name, mod.__name__ + ".py")
        mod1.payload, mod2.alias = payload, payload
        cfg = dict(mw._STATE["config"], root_paths=[self.temp.name], auto_widen=False, roots_scope="all")
        with mock.patch.dict(sys.modules, {mod1.__name__: mod1, mod2.__name__: mod2}):
            roots = mw._roots_from_modules(cfg)
        r = mw._scan_roots(roots, cfg)
        self.assertEqual(r["holders"][0]["aliases"], ["_mw_original.payload", "mw_importer.alias"])
        self.assertEqual(r["totals"]["accounted_bytes"], sys.getsizeof(payload))

    def test_class_cache_and_function_closure_roots(self):
        data = bytearray(1000)
        class C:
            cache = data
        def fn():
            return data
        class_rows = list(mw._root_members("C", C, "module"))
        closure_rows = list(mw._root_members("fn", fn, "module"))
        self.assertTrue(any(label == "C.cache" for label, _, _ in class_rows))
        self.assertTrue(any(label == "fn.closure" for label, _, _ in closure_rows))

    def test_report_does_not_retain_target(self):
        class Blob:
            pass
        x = Blob()
        x.data = bytearray(1024)
        ref = weakref.ref(x)
        mw.track("x", x, weak=True)
        result = self.capture()
        del x
        gc.collect()
        self.assertIsNone(ref())
        json.dumps(result, allow_nan=False)
        self.assertIsNone(mw._STATE["tracked"]["x"]())

    def test_strong_tracking_is_explicit_and_flush_controls_it(self):
        class Blob:
            pass
        x = Blob()
        ref = weakref.ref(x)
        mw.track("x", x)
        del x
        mw.flush(clear_tracked=False)
        self.assertIsNotNone(ref())
        mw.flush()
        gc.collect()
        self.assertIsNone(ref())

    def test_capture_and_flush_do_not_force_gc(self):
        with mock.patch.object(gc, "collect", side_effect=AssertionError("forced GC")):
            mw.track("x", [bytearray(20)])
            r = self.capture()
            mw.flush()
        self.assertEqual(r["errors"], [])


class ProxyTests(Base):
    def test_shared_target_and_aliases(self):
        x = [bytearray(1000)]
        a, b = Proxy(x), Proxy(x)
        r = self.scan([("a", a), ("alias_a", a), ("b", b)])
        self.assertEqual((a.calls, b.calls), (1, 1))
        self.assertEqual(len(r["proxies"]), 1)
        p = r["proxies"][0]
        self.assertEqual(p["proxy_count"], 2)
        self.assertEqual(p["alias_count"], 3)
        self.assertTrue(p["shared"])
        expected = sys.getsizeof(x) + sys.getsizeof(x[0])
        self.assertEqual(p["target_bytes"], expected)
        self.assertEqual(r["proxy_totals"]["targets_union_bytes"], expected)
        self.assertEqual(r["totals"]["accounted_bytes"], expected + mw._sizeof(a) + mw._sizeof(b))

    def test_distinct_targets_with_overlapping_children_union_once(self):
        shared = bytearray(1000)
        x, y = [shared], [shared]
        a, b = Proxy(x), Proxy(y)
        r = self.scan([("a", a), ("b", b)])
        self.assertEqual(len(r["proxies"]), 2)
        self.assertEqual(r["proxy_totals"]["targets_union_bytes"],
                         sys.getsizeof(x) + sys.getsizeof(y) + sys.getsizeof(shared))

    def test_none_is_bound_not_unbound(self):
        r = self.scan([("a", Proxy(None)), ("b", Proxy(None))])
        self.assertEqual(r["proxies"][0]["state"], "bound")
        self.assertTrue(r["proxies"][0]["shared"])
        self.assertEqual(r["proxies"][0]["target_bytes"], sys.getsizeof(None))

    def test_unbound_proxies_do_not_form_false_shared_group(self):
        r = self.scan([("a", Unbound()), ("b", Unbound())])
        self.assertEqual(len(r["proxies"]), 2)
        self.assertTrue(all(not p["shared"] and p["target_bytes"] is None for p in r["proxies"]))

    def test_resolve_false_never_calls_getter(self):
        p = Proxy(bytearray(1024))
        r = self.scan([("x", p)], resolve_proxies=False)
        self.assertEqual(p.calls, 0)
        self.assertEqual(r["proxies"][0]["state"], "disabled")
        self.assertEqual(r["totals"]["accounted_bytes"], mw._sizeof(p))

    def test_proxy_cycles_terminate(self):
        a, b = Proxy(None), Proxy(None)
        a.target, b.target = b, a
        r = self.scan([("a", a)])
        self.assertEqual(r["totals"]["objects"], 2)
        self.assertEqual((a.calls, b.calls), (1, 1))
        a.target = a
        r = self.scan([("a", a)])
        self.assertEqual(r["proxies"][0]["state"], "cycle")

    def test_foreign_context_not_resolved(self):
        p = Proxy(bytearray(1000))
        cfg = dict(mw.DEFAULT, max_seconds=10)
        r = mw._scan_roots([("foreign", p, "frame")], cfg,
                           diag={"root_contexts": {"foreign": False}})
        self.assertEqual(p.calls, 0)
        self.assertEqual(r["proxies"][0]["state"], "context_unavailable")

    def test_foreign_root_cannot_claim_current_context_proxy_payload(self):
        p = Proxy(bytearray(1000))
        foreign, current = [p], [p]
        cfg = dict(mw.DEFAULT, max_seconds=10)
        r = mw._scan_roots([("foreign", foreign, "frame"), ("current", current, "frame")], cfg,
                           diag={"root_contexts": {"foreign": False, "current": True}})
        holders = {h["root"]: h for h in r["holders"]}
        self.assertEqual(holders["foreign"]["bytes"], sys.getsizeof(foreign) + mw._sizeof(p))
        self.assertGreater(holders["current"]["bytes"], holders["foreign"]["bytes"])

    def test_nested_references_to_same_proxy_have_paths(self):
        p = Proxy(bytearray(100))
        result = self.scan([("container", {"left": p, "right": p})])
        row = result["proxies"][0]
        self.assertEqual(row["proxy_count"], 1)
        self.assertEqual(row["alias_count"], 2)
        self.assertEqual(row["observed_reference_edges"], 2)
        self.assertTrue(any("left" in path for path in row["proxies"]))
        self.assertTrue(any("right" in path for path in row["proxies"]))
        self.assertEqual(p.calls, 1)

    def test_proxy_group_budget_is_explicit(self):
        roots = [(str(i), Proxy(bytearray(20))) for i in range(5)]
        r = self.scan(roots, max_proxy_groups=1)
        self.assertEqual(len(r["proxies"]), 1)
        self.assertEqual(r["proxy_totals"]["distinct_proxies"], 5)
        self.assertTrue(r["proxy_totals"]["truncated"])

    @unittest.skipUnless(LocalProxy is not None, "Werkzeug not installed")
    def test_real_werkzeug_bound_none_shared_and_unbound(self):
        from contextvars import ContextVar
        value = ContextVar("test_value")
        a, b = context_proxy(value), context_proxy(value)
        r = self.scan([("a", a), ("b", b)])
        self.assertEqual(len(r["proxies"]), 2)
        token = value.set(bytearray(1000))
        try:
            r = self.scan([("a", a), ("alias", a), ("b", b)])
            self.assertTrue(r["proxies"][0]["shared"])
            self.assertEqual(r["proxies"][0]["target_bytes"], sys.getsizeof(value.get()))
            value.set(None)
            r = self.scan([("a", a)])
            self.assertEqual(r["proxies"][0]["state"], "bound")
        finally:
            value.reset(token)


@unittest.skipUnless(np is not None, "NumPy not installed")
class BufferTests(Base):
    def test_numpy_owner_plus_multiple_views_no_double_count(self):
        owner = np.arange(1000, dtype="float64")
        a, b = owner[::2], owner[1::2]
        r = self.scan([("owner", owner), ("a", a), ("b", b)])
        self.assertEqual(r["totals"]["native_bytes"], owner.nbytes)
        self.assertEqual(r["totals"]["accounted_bytes"], sum(sys.getsizeof(x) for x in (owner, a, b)))
        self.assertEqual(r["totals"]["objects"], 3)

    def test_numpy_view_only_follows_owner(self):
        owner = np.zeros(1000)
        view = owner[3:5]
        r = self.scan([("view", view)])
        self.assertEqual(r["totals"]["native_bytes"], owner.nbytes)
        self.assertEqual(r["totals"]["accounted_bytes"], sys.getsizeof(owner) + sys.getsizeof(view))

    def test_numpy_external_bytearray_buffer(self):
        backing = bytearray(1024)
        array = np.ndarray((128,), dtype="float64", buffer=backing)
        r = self.scan([("array", array), ("backing", backing)])
        self.assertEqual(r["totals"]["native_bytes"], 0)
        self.assertEqual(r["totals"]["accounted_bytes"], sys.getsizeof(backing) + sys.getsizeof(array))

    def test_memoryview_and_slices_share_exporter(self):
        data = bytearray(1000)
        a = memoryview(data)
        b = a[::2]
        try:
            r = self.scan([("a", a), ("b", b)])
            self.assertEqual(r["totals"]["objects"], 3)
            self.assertEqual(r["totals"]["accounted_bytes"], sum(sys.getsizeof(x) for x in (data, a, b)))
        finally:
            b.release()
            a.release()

    def test_object_array_python_payloads(self):
        x = bytearray(1000)
        array = np.empty(2, dtype=object)
        array[:] = [x, x]
        r = self.scan([("array", array)])
        self.assertEqual(r["totals"]["objects"], 2)
        self.assertEqual(r["totals"]["native_bytes"], array.nbytes)
        self.assertEqual(r["totals"]["accounted_bytes"], sys.getsizeof(array) + sys.getsizeof(x))

    def test_structured_object_array_does_not_count_observer_views(self):
        x = bytearray(1000)
        array = np.empty(2, dtype=[("id", "i4"), ("data", object)])
        array["data"] = [x, x]
        r = self.scan([("array", array)])
        self.assertEqual(r["totals"]["objects"], 2)
        self.assertEqual(r["totals"]["accounted_bytes"], sys.getsizeof(array) + sys.getsizeof(x))

    def test_mmap_is_mapping_length_not_file_size_or_rss(self):
        path = Path(self.temp.name) / "mapping.bin"
        path.write_bytes(b"\0" * 16384)
        with path.open("r+b") as fh, mmap.mmap(fh.fileno(), 4096) as mapping:
            view = np.frombuffer(mapping, dtype="u1")
            r = self.scan([("map", mapping), ("array", view)])
            self.assertEqual(r["totals"]["mapped_bytes"], 4096)
            self.assertEqual(r["totals"]["native_bytes"], 0)
            self.assertLess(r["totals"]["accounted_bytes"], 4096)
            del view


class BudgetTests(Base):
    def test_object_limit_exact_no_dropped_partial_row(self):
        data = [bytearray(10) for _ in range(20)]
        r = self.scan([("data", data)], max_objects=4)
        self.assertEqual(r["holders_scanned"], 4)
        self.assertTrue(r["holders_truncated"])
        self.assertGreater(r["holders"][0]["bytes"], 0)
        self.assertIsNone(r["holders"][0]["exclusive_bytes"])
        self.assertIn("max_objects", r["coverage"]["graph"]["reasons"])

    def test_zero_time_budget_does_not_claim_complete(self):
        r = self.scan([("data", [bytearray(20)])], max_seconds=0)
        self.assertEqual(r["holders_scanned"], 0)
        self.assertTrue(r["holders_truncated"])
        self.assertIn("max_seconds", r["coverage"]["graph"]["reasons"])

    def test_edge_budget_on_repeated_references(self):
        x = bytearray(10)
        data = [x] * 100000
        r = self.scan([("data", data)], max_edges=10)
        self.assertEqual(r["coverage"]["graph"]["edges_examined"], 10)
        self.assertTrue(r["holders_truncated"])
        self.assertLessEqual(r["holders_scanned"], 2)

    def test_proxy_target_truncation_propagates(self):
        p = Proxy([bytearray(100) for _ in range(20)])
        r = self.scan([("proxy", p)], max_objects=4)
        self.assertTrue(r["proxies"][0]["target_truncated"])
        self.assertTrue(r["proxy_totals"]["truncated"])
        self.assertEqual(p.calls, 1)

    def test_attribution_budget_retains_physical_totals(self):
        x = [bytearray(100)]
        complete = self.scan([("x", x)])
        for opts in ({"max_propagations": 0}, {"max_root_links": 0}):
            partial = self.scan([("x", x)], **opts)
            self.assertEqual(partial["totals"]["accounted_bytes"], complete["totals"]["accounted_bytes"])
            self.assertTrue(partial["holders_truncated"])
            self.assertIsNone(partial["totals"]["exclusive_bytes"])

    def test_census_budget_status(self):
        cfg = dict(mw.DEFAULT, max_census_objects=1, max_seconds=10)
        r = mw._scan_roots([], cfg, gc_pool=[[], {}, bytearray(20)])
        self.assertTrue(r["census_meta"]["truncated"])
        self.assertEqual(r["census_meta"]["gc_objects_examined"], 1)

    def test_root_limit_and_zero_limits(self):
        mw.track("a", bytearray(100))
        mw.track("b", bytearray(100))
        r = self.capture(max_roots=1)
        self.assertTrue(r["holders_truncated"])
        r = self.capture(max_roots=0, max_objects=0)
        self.assertEqual(r["totals"]["objects"], 0)
        self.assertTrue(r["holders_truncated"])


class APITests(Base):
    def test_mark_respects_installed_profile_and_mark_all_defaults_deep(self):
        mw.install(profile="counters")
        r = mw.mark("configured", brief=False)
        self.assertEqual(r["profile"], "counters")
        with mock.patch.object(gc, "get_objects", return_value=[]):
            r = mw.mark_all("all", roots_scope="tracked", brief=False)
        self.assertEqual(r["profile"], "deep")
        self.assertIn("census", r)
        self.assertEqual(r["sites"]["status"], "unavailable")
        r = mw.mark_all("override", profile="standard", roots_scope="tracked", brief=False)
        self.assertEqual(r["profile"], "standard")
        self.assertNotIn("sites", r)

    def test_standard_includes_type_summary(self):
        mw.track("data", bytearray(100))
        r = self.capture()
        self.assertTrue(r["types"])
        self.assertTrue(r["census"])
        self.assertEqual(r["census_meta"]["scope"], "reachable_graph")

    def test_per_call_output_override_and_persisted_report(self):
        other = Path(self.temp.name) / "other"
        seen = []
        r = self.capture(profile="counters", write_report=True, out_dir=other, sink=seen.append)
        self.assertEqual(len(seen), 1)
        path = Path(r["report_paths"]["json"])
        self.assertEqual(path.parent, other)
        self.assertEqual(json.loads(path.read_text()), r)
        mw.install(write_report=True)
        r = self.capture(profile="counters", write_report=False)
        self.assertNotIn("report_file", r)
        self.assertFalse(list(Path(self.temp.name).glob("*.json")))
        self.assertEqual(self.capture(enabled=False)["skipped"], "disabled")

    def test_output_failure_is_reported(self):
        file = Path(self.temp.name) / "not_a_dir"
        file.write_text("x")
        r = self.capture(profile="counters", write_report=True, out_dir=file)
        self.assertTrue(r["report_write_failed"])
        self.assertTrue(r["errors"])
        with self.assertRaises(OSError):
            self.capture(profile="counters", write_report=True, out_dir=file, fail_silent=False)

    def test_prefix_cannot_escape_output_directory(self):
        r = self.capture(profile="counters", write_report=True, label_prefix="../../outside/")
        self.assertEqual(Path(r["report_paths"]["json"]).parent, Path(self.temp.name))
        self.assertTrue(Path(r["report_paths"]["json"]).is_file())

    def test_sink_reentry_is_busy_without_consuming_label_quota(self):
        inner = []
        def sink(rep):
            inner.append(mw.snapshot("recursive"))
        r = self.capture(profile="counters", sink=sink)
        self.assertEqual(r["errors"], [])
        self.assertEqual(inner[0]["skipped"], "busy")
        self.assertNotIn("recursive", mw._STATE["label_counts"])

    def test_concurrent_capture_is_busy(self):
        entered, leave = threading.Event(), threading.Event()
        results = []
        def sink(rep):
            entered.set()
            leave.wait(5)
        t = threading.Thread(target=lambda: results.append(self.capture(profile="counters", sink=sink)))
        t.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(mw.snapshot("racing")["skipped"], "busy")
            self.assertNotIn("racing", mw._STATE["label_counts"])
        finally:
            leave.set()
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertFalse(results[0].get("skipped"))

    def test_label_limits_are_bounded(self):
        mw.install(max_marks_per_label=1, max_labels=1)
        self.assertNotIn("skipped", self.capture(profile="counters"))
        self.assertEqual(self.capture(profile="counters")["skipped"], "label_limit")
        self.assertEqual(mw.snapshot("other")["skipped"], "label_capacity")

    def test_unknown_and_invalid_config(self):
        r = self.capture(tops=4)
        self.assertIn("tops", " ".join(r["warnings"]))
        for key, value in (("max_seconds", float("nan")), ("max_objects", -1), ("top", 0), ("preview", object())):
            with self.assertRaises((ValueError, TypeError)):
                mw.install(**{key: value})
        self.assertEqual(self.capture(max_seconds=-1)["status"], "error")

    def test_phase_error_preserves_other_results(self):
        mw.track("x", bytearray(100))
        with mock.patch.object(mw, "_phase_counters", side_effect=RuntimeError("broken")):
            r = self.capture()
        self.assertTrue(r["holders"])
        self.assertEqual(r["phases"]["counters"]["status"], "error")
        with mock.patch.object(mw, "_phase_counters", side_effect=RuntimeError("broken")):
            with self.assertRaises(RuntimeError):
                self.capture(fail_silent=False)

    def test_root_phase_error_marks_all_dependent_results_partial(self):
        with mock.patch.object(mw, "collect_roots", side_effect=RuntimeError("broken")):
            r = self.capture()
        self.assertTrue(r["holders_truncated"])
        self.assertIsNone(r["totals"]["exclusive_bytes"])
        self.assertFalse(r["coverage"]["attribution"]["complete"])
        self.assertTrue(r["census_meta"]["truncated"])

    def test_skipped_deep_census_does_not_advance_growth_baseline(self):
        r = self.capture(profile="deep", max_seconds=0)
        self.assertTrue(r["census_meta"]["truncated"])
        self.assertIsNone(mw._STATE["growth_base"])

    def test_growth_uses_type_net_diffs_not_ids(self):
        a = bytearray(100)
        mw.track("a", a)
        with mock.patch.object(gc, "get_objects", return_value=[]):
            one = self.capture(profile="deep")
            a.extend(b"a" * 100)
            two = mw.snapshot("next", profile="deep")
        self.assertEqual(one["growth"], [])
        delta = next(row for row in two["growth"] if row["type"] == "builtins.bytearray")
        self.assertEqual(delta["count_diff"], 0)
        self.assertGreater(delta["bytes_diff"], 0)

    def test_tracemalloc_sites_and_external_trace_ownership(self):
        tracemalloc.start(1)
        try:
            mw.install(trace=False)
            x = bytearray(3000)
            mw.track("x", x)
            with mock.patch.object(gc, "get_objects", return_value=[]):
                r = self.capture(profile="deep")
            self.assertEqual(r["sites"]["status"], "complete")
            self.assertEqual(r["sites"]["mode"], "absolute")
            self.assertIsNotNone(r["sites"]["take_snapshot_ms"])
            mw.uninstall()
            self.assertTrue(tracemalloc.is_tracing())
        finally:
            tracemalloc.stop()

    def test_node_delta_saved_before_sink_and_file(self):
        seen = []
        with mw.node("node", profile="counters", write_report=True, sink=seen.append):
            pass
        exit_report = seen[-1]
        self.assertIn("node_delta_mb", exit_report)
        self.assertIn("node_delta_mb", json.loads(Path(exit_report["report_paths"]["json"]).read_text()))

    def test_async_watch_captures_around_awaited_work(self):
        order = []
        def sink(rep):
            order.append(rep["label"])
        @mw.watch("work", profile="counters", sink=sink)
        async def work():
            order.append("before")
            await asyncio.sleep(0)
            order.append("after")
            return 7
        self.assertEqual(asyncio.run(work()), 7)
        self.assertEqual(order, ["work:enter", "before", "after", "work:exit"])

    def test_watch_preserves_business_exception_and_generator_lifetime(self):
        order = []
        @mw.watch("gen", profile="counters", sink=lambda rep: order.append(rep["label"]))
        def generate():
            yield 1
            raise ValueError("business")
        gen = generate()
        self.assertEqual(order, [])
        self.assertEqual(next(gen), 1)
        self.assertEqual(order, ["gen:enter"])
        with self.assertRaisesRegex(ValueError, "business"):
            next(gen)
        self.assertEqual(order, ["gen:enter", "gen:exit"])

    def test_report_selection_uses_timestamp_and_exact_seq(self):
        root = Path(self.temp.name)
        (root / "z_older.json").write_text(json.dumps({"ts": 1, "seq": 9, "pid": 123}))
        (root / "a_newer.json").write_text(json.dumps({"ts": 2, "seq": 3, "pid": 456}))
        (root / "corrupt.json").write_text("{")
        self.assertEqual(mw.report(root)["seq"], 3)
        self.assertEqual(mw.report(root, seq=9)["pid"], 123)
        with self.assertRaises(FileNotFoundError):
            mw.report(root, seq=999)

    def test_install_is_idempotent_and_background_keeps_tracks(self):
        mw.track("x", bytearray(100))
        event = threading.Event()
        mw.install(profile="counters", interval=0.1, sink=lambda rep: event.set())
        worker = mw._STATE["workers"]["watchdog"]["thread"]
        mw.install(interval=0.1)
        self.assertIs(worker, mw._STATE["workers"]["watchdog"]["thread"])
        self.assertTrue(event.wait(3))
        self.assertIn("x", mw._STATE["tracked"])
        generation = mw._STATE["generation"]
        mw.uninstall()
        self.assertFalse(worker.is_alive())
        self.assertEqual(mw._do_snapshot("stale", background_generation=generation)["skipped"], "cancelled")
        self.assertFalse(mw.is_installed())
        mw.install(trace=False, on_signal=False, interval=0.1, profile="counters", write_report=False)
        self.assertFalse(mw._STATE["stop"].is_set())
        self.assertIsNot(worker, mw._STATE["workers"]["watchdog"]["thread"])

    def test_sink_can_uninstall_without_reviving_probe(self):
        r = self.capture(profile="counters", sink=lambda rep: mw.uninstall())
        self.assertEqual(r["skipped"], "cancelled")
        self.assertFalse(mw.is_installed())
        self.assertIsNone(mw._STATE["prev_snapshot"])

    def test_hooks_restore_none_and_preserve_existing_profiler(self):
        original = sys.getprofile()
        original_thread = getattr(threading, "getprofile", lambda: None)()
        if original is not None or original_thread is not None:
            self.skipTest("external profiler already installed")
        mw.install(at="never_called_memwatch_test")
        self.assertIsNotNone(sys.getprofile())
        mw.uninstall()
        self.assertIsNone(sys.getprofile())
        if hasattr(threading, "getprofile"):
            self.assertIsNone(threading.getprofile())
        def profiler(frame, event, arg):
            return None
        sys.setprofile(profiler)
        try:
            mw.install(trace=False, on_signal=False, at="another_never_called")
            self.assertIs(sys.getprofile(), profiler)
            mw.uninstall()
            self.assertIs(sys.getprofile(), profiler)
        finally:
            sys.setprofile(original)

    def test_existing_signal_handler_is_not_replaced(self):
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("POSIX signal unavailable")
        original = signal.getsignal(signal.SIGUSR1)
        def handler(signum, frame):
            pass
        signal.signal(signal.SIGUSR1, handler)
        try:
            mw.install(on_signal=True)
            self.assertIs(signal.getsignal(signal.SIGUSR1), handler)
            mw.uninstall()
            self.assertIs(signal.getsignal(signal.SIGUSR1), handler)
        finally:
            signal.signal(signal.SIGUSR1, original)

    def test_peak_units_and_uss_api(self):
        if sys.platform == "win32":
            self.skipTest("resource unavailable")
        fake_resource = types.SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda _: types.SimpleNamespace(ru_maxrss=2048))
        fake_proc = types.SimpleNamespace(
            memory_info=lambda: types.SimpleNamespace(rss=4096, vms=8192),
            memory_full_info=lambda: types.SimpleNamespace(uss=2048, pss=3072))
        fake_psutil = types.SimpleNamespace(Process=lambda: fake_proc)
        with mock.patch.dict(sys.modules, {"resource": fake_resource, "psutil": fake_psutil}):
            with mock.patch.object(sys, "platform", "linux"):
                linux = mw._phase_counters(mw.DEFAULT)
            with mock.patch.object(sys, "platform", "darwin"):
                darwin = mw._phase_counters(mw.DEFAULT)
        self.assertEqual(linux["peak_rss_bytes"], 2048 * 1024)
        self.assertEqual(darwin["peak_rss_bytes"], 2048)
        self.assertEqual(linux["uss_bytes"], 2048)
        self.assertEqual(linux["rss_bytes"], 4096)


class SubprocessTests(Base):
    def run_script(self, body, extra_env=None, cli=True):
        script = Path(self.temp.name) / "app.py"
        script.write_text(body)
        output = Path(self.temp.name) / "reports"
        env = dict(os.environ, MEMWATCH_OUT=str(output), MEMWATCH_AUTO="0",
                   PYTHONPATH=str(Path(mw.__file__).parent))
        env.update(extra_env or {})
        cmd = [sys.executable, "-m", "memwatch", "run", str(script)] if cli else [sys.executable, str(script)]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        rows = [json.loads(p.read_text()) for p in output.glob("*.json")]
        return proc, rows

    def test_cli_single_state_retains_shutdown_globals(self):
        proc, rows = self.run_script('import memwatch\npayload = bytearray(200000)\nmemwatch.mark("inside", brief=False)\n')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(r["seq"] for r in rows), [1, 2, 3])
        self.assertEqual(len({r["session"] for r in rows}), 1)
        shutdown = next(r for r in rows if r["label"] == "shutdown")
        self.assertTrue(any("payload" in h["root"] and h["bytes"] >= 200000 for h in shutdown["holders"]))

    def test_cli_at_call_and_line_restore_hooks(self):
        body = 'import sys\ndef checkpoint_target(payload):\n    return len(payload)\ndata = bytearray(10000)\ncheckpoint_target(data)\n'
        for target in ("checkpoint_target", "5"):
            env = {"MEMWATCH_AT": str(Path(self.temp.name) / "app.py") + ":" + target}
            proc, rows = self.run_script(body, env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            hits = [r for r in rows if r["label"].startswith("at:")]
            self.assertTrue(hits, proc.stderr)
            self.assertEqual(hits[-1]["profile"], "deep")
            self.assertIn("sites", hits[-1])
            # Remove reports between separate processes to keep assertions scoped.
            for path in (Path(self.temp.name) / "reports").glob("*"):
                path.unlink()

    def test_cli_system_exit_code_preserved(self):
        proc, rows = self.run_script('raise SystemExit(7)\n')
        self.assertEqual(proc.returncode, 7)
        self.assertTrue(any(r["label"] == "shutdown" for r in rows))


class ExtendedIntegrationTests(SubprocessTests):
    # Reuse the subprocess harness, not its test methods.
    test_cli_single_state_retains_shutdown_globals = None
    test_cli_at_call_and_line_restore_hooks = None
    test_cli_system_exit_code_preserved = None

    def test_sitecustomize_requires_opt_in(self):
        code = 'import sys\nprint("LOADED", "memwatch" in sys.modules)\n'
        script = Path(self.temp.name) / "plain.py"
        script.write_text(code)
        env = dict(os.environ, PYTHONPATH=str(Path(mw.__file__).parent))
        env.pop("MEMWATCH_AUTO", None)
        for key in list(env):
            if key.startswith("MEMWATCH_"):
                env.pop(key)
        proc = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LOADED False", proc.stdout)

    def test_preinstalled_probe_and_cli_share_single_session(self):
        script = Path(self.temp.name) / "preinstalled.py"
        script.write_text('import memwatch\nmemwatch.mark("inside", brief=False, profile="counters")\n')
        output = Path(self.temp.name) / "reports"
        env = dict(os.environ, MEMWATCH_AUTO="0", MEMWATCH_OUT=str(output),
                   PYTHONPATH=str(Path(mw.__file__).parent))
        bootstrap = "import runpy, sys, memwatch; memwatch.install(); sys.argv=['memwatch', 'run', %r]; runpy.run_module('memwatch', run_name='__main__')" % str(script)
        proc = subprocess.run([sys.executable, "-c", bootstrap], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(p.read_text()) for p in output.glob("*.json")]
        self.assertEqual(sorted(r["seq"] for r in rows), [1, 2, 3])
        self.assertEqual(len({r["session"] for r in rows}), 1)

    def test_actual_pythonpath_startup_injection(self):
        env = dict(os.environ, MEMWATCH_AUTO="1", MEMWATCH_OUT=self.temp.name,
                   PYTHONPATH=str(Path(mw.__file__).parent))
        source = "import sys,json; h=sys.modules.get('sitecustomize'); m=sys.modules.get('memwatch'); print(json.dumps({'hook':getattr(h,'__file__',None),'installed':bool(m and m.is_installed())}))"
        proc = subprocess.run([sys.executable, "-c", source], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        info = json.loads(proc.stdout)
        expected = Path(mw.__file__).with_name("sitecustomize.py").resolve()
        if not info["hook"] or Path(info["hook"]).resolve() != expected:
            self.skipTest("runtime startup hook shadows PYTHONPATH: " + str(info["hook"]))
        self.assertTrue(info["installed"])

    def test_sitecustomize_chain_first_hook_even_when_probe_disabled(self):
        first, second = Path(self.temp.name) / "first", Path(self.temp.name) / "second"
        first.mkdir(); second.mkdir()
        for folder, value in ((first, "first"), (second, "second")):
            (folder / "sitecustomize.py").write_text('import os\nos.environ["CHAIN_MARK"] = "%s"\n' % value)
        env = dict(os.environ, MEMWATCH_AUTO="0",
                   PYTHONPATH=os.pathsep.join((str(Path(mw.__file__).parent), str(first), str(second))))
        bootstrap = "import runpy, os; runpy.run_path(%r); print(os.environ.get('CHAIN_MARK'))" % str(Path(mw.__file__).with_name("sitecustomize.py"))
        proc = subprocess.run([sys.executable, "-c", bootstrap],
                              env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "first")

    def test_stdlib_only_fallback(self):
        body = '''import sys
class Blocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'psutil', 'numpy', 'werkzeug'}:
            raise ImportError('blocked optional dependency')
sys.meta_path.insert(0, Blocker())
import memwatch
class Proxy:
    def _get_current_object(self):
        return payload
payload = bytearray(2 * 1024 * 1024)
proxy = Proxy()
memwatch.install(trace=False, on_signal=False, roots_scope='tracked', count_objects=False)
memwatch.track('proxy', proxy)
r = memwatch.snapshot('fallback')
assert not r['errors'], r['errors']
assert r['proxies'][0]['target_bytes'] > 2000000
assert r['counters']['rss_is_peak']
print('OK')
'''
        proc, rows = self.run_script(body, cli=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)
        self.assertTrue(rows)

    def test_asyncio_suspended_task_payload_root(self):
        body = '''import asyncio, memwatch
memwatch.install(trace=False, on_signal=False, write_report=False, max_seconds=10)
async def worker():
    payload = [bytearray(262144) for _ in range(8)]
    await asyncio.sleep(3600)
async def main():
    task = asyncio.create_task(worker())
    await asyncio.sleep(0)
    r = memwatch.mark('async', roots_scope='all', brief=False)
    matches = [h for h in r['holders'] if any('payload' in a for a in h['aliases'])]
    assert matches and matches[0]['bytes'] > 2000000, r
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
asyncio.run(main())
print('OK')
'''
        proc, _ = self.run_script(body, cli=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("OK", proc.stdout)

    def test_auto_root_inference_and_widening(self):
        body = '''import memwatch
memwatch.install(trace=False, on_signal=False, write_report=False, root_paths=['/not-a-real-project'])
def work():
    data = bytearray(1000)
    r = memwatch.mark(brief=False)
    assert r['auto_widened'], r
    assert any(h['root'].endswith(':data') for h in r['holders']), r
    d = memwatch.diagnose(write=False)
    assert d['would_be_accepted_if_widened'] > d['accepted_frames'], d
work()
print('OK')
'''
        proc, _ = self.run_script(body, cli=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("OK", proc.stdout)

    def test_external_file_and_signal_triggers(self):
        for mode in ("file", "signal"):
            if mode == "signal" and not hasattr(signal, "SIGUSR1"):
                continue
            directory = Path(self.temp.name) / mode
            directory.mkdir()
            trigger = directory / "NOW"
            script = directory / "target.py"
            script.write_text('''import sys, memwatch
memwatch.install()
payload = bytearray(1000000)
memwatch.track('payload', payload)
print('READY', flush=True)
sys.stdin.readline()
memwatch.uninstall()
''')
            env = dict(os.environ, MEMWATCH_AUTO="1", MEMWATCH_OUT=str(directory),
                       MEMWATCH_PROFILE="standard", PYTHONPATH=str(Path(mw.__file__).parent))
            if mode == "file":
                env["MEMWATCH_WATCH_FILE"] = str(trigger)
            proc = subprocess.Popen([sys.executable, str(script)], env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "READY")
                if mode == "file":
                    trigger.touch()
                else:
                    proc.send_signal(signal.SIGUSR1)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and not list(directory.glob("*.json")):
                    time.sleep(0.05)
                paths = list(directory.glob("*.json"))
                self.assertTrue(paths, mode)
                result = json.loads(paths[0].read_text())
                self.assertEqual(result["label"], "signal" if mode == "signal" else "file:NOW")
                self.assertTrue(any("tracked:payload" in h["aliases"] for h in result["holders"]))
            finally:
                if proc.poll() is None:
                    proc.stdin.write("stop\n"); proc.stdin.flush()
                stdout, stderr = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, stderr)

    def test_memprobe_compatibility_uses_deduplicated_engine(self):
        import memprobe as mp
        payload = bytearray(1000)
        a, b = Proxy(payload), Proxy(payload)
        roots = [("a", a), ("b", b)]
        rows = mp.retained_attribution(roots)
        self.assertEqual(len(rows), 2)
        probe = mp.Probe(trace=False)
        text = "\n".join(probe.proxies("shared", roots))
        self.assertIn("deduplicated union: %s B" % sys.getsizeof(payload), text)
        self.assertIn("wrappers=2", text)
        self.assertIn("NOT additive", "\n".join(probe.holders("holders", roots)))
        probe.close()

    @unittest.skipUnless(LocalProxy is not None, "Werkzeug not installed")
    def test_real_werkzeug_local_and_stack_backends(self):
        from werkzeug.local import Local, LocalStack
        local, stack = Local(), LocalStack()
        data = bytearray(1000)
        local.value = data
        stack.push(data)
        a, b, c = LocalProxy(local, "value"), (LocalProxy(stack) if WERKZEUG_MODERN else stack()), LocalProxy(lambda: local.value)
        result = self.scan([("a", a), ("b", b), ("c", c)])
        self.assertEqual(result["proxies"][0]["proxy_count"], 3)
        self.assertEqual(result["proxy_totals"]["targets_union_bytes"], sys.getsizeof(data))
        stack.pop()
        del local.value
        # The repository demo must also use a construction valid in both series.
        import b as demo_backend
        demo_backend.install(data)
        try:
            for proxy in (demo_backend.payload_proxy, demo_backend.stack_proxy, demo_backend.lazy_proxy):
                self.assertIs(object.__getattribute__(proxy, "_get_current_object")(), data)
        finally:
            demo_backend.uninstall()

    def test_closed_mmap_and_released_memoryview_are_explicit(self):
        mm = mmap.mmap(-1, 4096)
        mm.close()
        mv = memoryview(bytearray(10))
        mv.release()
        result = self.scan([("mm", mm), ("mv", mv)])
        self.assertIn("closed_or_released_buffer", result["coverage"]["native"]["notes"])
        self.assertTrue(result["holders_truncated"])


if __name__ == "__main__":
    unittest.main()
