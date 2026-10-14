"""Checkpoint memory diagnostics, without mandatory third-party dependencies.

    import memwatch
    memwatch.mark()                 # local roots + deduplicated type summary
    memwatch.mark_all()             # deep: also allocation sites and GC census

A reference is not an owner. Reachable bytes are NOT additive across roots and
are not dominator/retained sizes. Native buffers and mappings have distinct
accounting. Every bounded phase reports its coverage. Deploy this file WITH _memwatch_core.py.
See README_INTERFACE.md.
"""
from __future__ import annotations

import functools
import gc
import inspect
import json
import math
import mmap
import os
import signal
import sys
import sysconfig
import tempfile
import threading
import time
import tracemalloc
import types
import weakref
from collections import deque

__all__ = ["install", "mark", "mark_all", "snapshot", "node", "watch", "track",
           "untrack", "flush", "uninstall", "is_installed", "report", "diagnose", "DEFAULT"]

from _memwatch_core import (
    DEFAULT,
    __version__,
    _SELF_FILE,
    _OBSERVER_FILES,
    _INFRA_TYPES,
    _GC_HEAD,
    _STATE,
    _typename,
    _is_proxy,
    _resolve_proxy_result,
    _resolve_proxy,
    _proxy_state,
    _sizeof,
    _safe_preview,
    _describe,
    _size_hint,
    _is_infra,
    _numpy_type,
    _measure,
    _children,
    _numpy_object_elements,
    _Budget,
    _caller_frame,
    _frame_label,
    _frame_root,
    _is_virtual_filename,
    _site_prefixes,
    _under_lib,
    _under,
    _lib_prefixes,
    _module_path,
    _auto_root_paths,
    _path_accepted,
    _frame_verdict,
    _frame_is_first_party,
    _module_is_first_party,
    _root_members,
    _roots_from_modules,
    _stack_rows,
    _roots_from_stack,
    _roots_from_frames,
    _roots_from_asyncio,
    collect_roots,
    _Object,
    _add_type,
    _edge_iter,
    _bits,
    _scan_roots,
    _deep_size,
    _phase_holders,
    _phase_proxies,
    _phase_census,
    _phase_counters,
    _is_self_trace,
)

def _baseline_valid(cfg):
    return (cfg.get("_generation", _STATE["generation"]) == _STATE["generation"]
            and cfg.get("_baseline_epoch", _STATE.get("baseline_epoch", 0)) == _STATE.get("baseline_epoch", 0))


def _phase_sites(cfg, label=None, captured=None, deadline=None):
    if not tracemalloc.is_tracing():
        _STATE["prev_snapshot"] = _STATE["prev_label"] = None
        return {"status": "unavailable", "reason": "tracemalloc_disabled", "rows": [],
                "coverage": "only allocations recorded while tracemalloc is active"}
    start = time.monotonic()
    if captured is None:
        captured = (tracemalloc.take_snapshot(), (time.monotonic() - start) * 1000)
    snap, take_ms = captured
    out = {"take_snapshot_ms": round(take_ms, 3), "traces": len(snap.traces), "rows": [],
           "since": _STATE.get("prev_label"), "status": "complete",
           "coverage": "live traced allocations; not all native allocations; no pre-start history"}
    if deadline is not None and time.monotonic() >= deadline:
        out.update(status="skipped", reason="max_seconds_before_statistics")
        return out
    previous = _STATE.get("prev_snapshot")
    stats = snap.compare_to(previous, "lineno") if previous is not None else snap.statistics("lineno")
    out["mode"] = "diff" if previous is not None else "absolute"
    for st in stats:
        if _is_self_trace(st):
            continue
        if previous is None:
            row = {"bytes": st.size, "kb": round(st.size / 1024, 3),
                   "count": st.count, "where": str(st.traceback)}
        else:
            if st.size_diff == 0 and st.count_diff == 0:
                continue
            row = {"bytes_diff": st.size_diff, "kb_diff": round(st.size_diff / 1024, 3),
                   "count_diff": st.count_diff, "where": str(st.traceback)}
        out["rows"].append(row)
        if len(out["rows"]) >= cfg["top"]:
            break
    out["elapsed_ms"] = round((time.monotonic() - start) * 1000, 3)
    out["budget_overrun"] = deadline is not None and time.monotonic() > deadline
    if _baseline_valid(cfg):
        _STATE["prev_snapshot"], _STATE["prev_label"] = snap, label
    return out


def _normalise_cfg(base, overrides=None, refresh_roots=True):
    cfg = dict(base)
    overrides = overrides or {}
    unknown = [k for k in overrides if k not in DEFAULT]
    cfg.update({k: v for k, v in overrides.items() if k in DEFAULT})
    warnings = ["忽略未知参数: " + ", ".join(sorted(unknown))] if unknown else []
    for key in ("max_objects", "max_edges", "max_roots", "max_propagations", "max_root_links",
                "max_census_objects", "max_proxy_groups", "max_marks_per_label", "max_labels"):
        if type(cfg[key]) is not int or cfg[key] < 0:
            raise ValueError(key + " must be a non-negative integer")
    for key in ("top", "brief_top", "frames"):
        if type(cfg[key]) is not int or cfg[key] < 1:
            raise ValueError(key + " must be a positive integer")
    if cfg["frames"] > 65535:
        raise ValueError("frames must be <= 65535")
    for key in ("max_seconds", "interval", "rss_threshold_mb"):
        value = cfg[key]
        if value is None and key != "max_seconds":
            continue
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(key + " must be finite")
        if value < 0 or (key != "max_seconds" and value == 0):
            raise ValueError(key + " must be positive (max_seconds may be zero)")
    if cfg["roots_scope"] not in ("all", "locals", "tracked"):
        raise ValueError("roots_scope must be all, locals or tracked")
    for key in ("profile", "all_profile"):
        if cfg[key] not in ("counters", "standard", "deep"):
            raise ValueError(key + " must be counters, standard or deep")
    for key in ("enabled", "trace", "include_third_party", "include_threads", "include_asyncio",
                "resolve_proxies", "skip_infra", "count_objects", "preview", "auto_widen",
                "write_report", "print_report", "on_signal", "fail_silent", "allow_custom_sizeof"):
        if type(cfg[key]) is not bool:
            raise ValueError(key + " must be bool")
    if cfg["brief"] is not None and type(cfg["brief"]) is not bool:
        raise ValueError("brief must be bool or None")
    for key in ("label_prefix", "at"):
        if not isinstance(cfg[key], str) and not (key == "at" and cfg[key] is None):
            raise ValueError(key + " must be str" + (" or None" if key == "at" else ""))
    for key in ("out_dir", "watch_file"):
        if cfg[key] is not None:
            cfg[key] = os.fspath(cfg[key])
            if not isinstance(cfg[key], str):
                raise ValueError(key + " must be a text path")
    if cfg["sink"] is not None and not callable(cfg["sink"]):
        raise ValueError("sink must be callable or None")
    auto = cfg.get("_root_paths_auto", False)
    if "root_paths" in overrides:
        auto = overrides["root_paths"] is None
    if cfg["root_paths"] is None or (auto and (refresh_roots or "root_paths" in overrides)):
        # Import/path discovery is configuration work, not a per-checkpoint scan.
        # install() or an explicit root_paths=None refreshes inferred directories.
        cfg["root_paths"], auto = _auto_root_paths(), True
    elif isinstance(cfg["root_paths"], (str, os.PathLike)):
        cfg["root_paths"] = [cfg["root_paths"]]
    cfg["root_paths"] = [os.path.realpath(os.fspath(p)) for p in cfg["root_paths"]]
    cfg["_root_paths_auto"] = auto
    return cfg, warnings


def _snapshot_impl(label, profile=None, explicit=None, frame=None, effective=None):
    if effective is None:
        cfg, warnings = _normalise_cfg(_STATE.get("config") or DEFAULT, explicit)
    else:
        cfg, warnings = dict(effective), list(effective.get("_warnings", []))
    if profile is not None:
        if profile not in ("counters", "standard", "deep"):
            raise ValueError("invalid profile: " + str(profile))
        cfg["profile"] = profile
    profile = cfg["profile"]
    cfg["_stop"] = _STATE["stop"]
    cfg["_generation"] = _STATE["generation"]
    cfg["_baseline_epoch"] = _STATE.get("baseline_epoch", 0)
    start = time.monotonic()
    budget = _Budget(cfg)
    cfg["_budget"] = budget
    rep = {"schema_version": 2, "producer": "memwatch", "version": __version__,
           "seq": _STATE["seq"], "label": label, "profile": profile,
           "pid": os.getpid(), "ts": time.time(), "session": _STATE.get("session", ""),
           "forked_from_pid": _STATE.get("forked_from_pid"),
           "config": {k: cfg[k] for k in DEFAULT if k != "sink"},
           "errors": [], "warnings": warnings + list(_STATE["warnings"]), "phases": {},
           "callsite": _frame_label(frame), "roots_scope": cfg["roots_scope"],
           "root_paths": cfg["root_paths"],
           "measurement_contract": {
               "holders": "selected-root reachable bytes; non-additive; not allocation ownership or retained size",
               "types": "identity-deduplicated size estimates; recognized native buffers split out",
               "mappings": "virtual mapped extents, never added to accounted_bytes or equated to RSS",
               "budget": "cooperative; GC, extension descriptors, trace statistics and I/O cannot be preempted",
               "native_complete": False,
               "elapsed_ms": "capture phases only; excludes report writing and user sink",
               "proxy_resolution": "may execute application callable; disable with resolve_proxies=False",
               "snapshot_consistency": "live best effort; other threads and native code may mutate"}}

    def phase(name, fn, fallback=None):
        t = time.monotonic()
        try:
            result = fn()
            rep["phases"][name] = {"status": "completed", "elapsed_ms": round((time.monotonic() - t) * 1000, 3)}
            return result
        except Exception as exc:
            if not cfg["fail_silent"]:
                raise
            rep["errors"].append(name + ": " + type(exc).__name__)
            rep["phases"][name] = {"status": "error", "error": type(exc).__name__}
            return fallback

    rep["counters"] = phase("counters", lambda: _phase_counters(cfg), {})
    captured = None
    if profile == "deep":
        if tracemalloc.is_tracing() and budget.alive():
            t = time.monotonic()
            snap = phase("take_snapshot", tracemalloc.take_snapshot)
            if snap is not None:
                captured = (snap, (time.monotonic() - t) * 1000)
            else:
                rep["sites"] = {"status": "error", "reason": "take_snapshot_failed", "rows": []}
        elif not tracemalloc.is_tracing():
            rep["sites"] = phase("sites", lambda: _phase_sites(cfg, label), {"status": "error", "rows": []})
        else:
            rep["sites"] = {"status": "skipped", "reason": "max_seconds_before_snapshot", "rows": []}
    if captured is not None:
        rep["sites"] = phase("sites", lambda: _phase_sites(cfg, label, captured, budget.deadline),
                             {"status": "error", "reason": "statistics_failed", "rows": []})
        captured = None
    pool = None
    if (profile == "deep" or cfg["count_objects"]) and budget.alive():
        pool = phase("gc_objects", gc.get_objects)
        if pool is not None:
            rep["counters"]["objects_tracked"] = len(pool)
        if profile != "deep":
            pool = None
    if profile in ("standard", "deep"):
        diag = {}
        roots = phase("roots", lambda: collect_roots(cfg, frame, diag), [])
        analysis = phase("graph", lambda: _scan_roots(roots, cfg, diag=diag, gc_pool=pool))
        if analysis is not None:
            all_types = analysis.pop("_census_all")
            rep.update(analysis)
            if profile == "deep" and pool is None:
                rep["census_meta"].update(complete_for_scope=False, truncated=True)
                rep["census_meta"]["reasons"].append("gc_pool_unavailable_or_budget_exhausted")
            if "roots" in rep["phases"] and rep["phases"]["roots"]["status"] == "error":
                rep["holders_truncated"] = True
                rep["coverage"]["graph"]["complete"] = False
                rep["coverage"]["graph"]["reasons"].append("roots_error")
                rep["coverage"]["attribution"]["complete"] = False
                rep["totals"].update(exclusive_bytes=None, attribution_complete=False)
                rep["proxy_totals"]["truncated"] = True
                for holder in rep["holders"]:
                    holder.update(exclusive_bytes=None, truncated=True)
                rep["census_meta"].update(complete_for_scope=False, truncated=True)
            rep["auto_widened"] = bool(diag.get("auto_widened"))
            rep["root_diagnostics"] = {k: v for k, v in diag.items() if k != "root_contexts"}
            if rep["auto_widened"]:
                rep["warnings"].append("root_paths 未命中；已放宽到非库代码，请核对 root_paths")
            if not rep["holders"]:
                rep["diagnostics"] = ["没有可归因的变量；检查 root_paths / scope / budget，或使用 track()", str(rep["root_diagnostics"])]
            if rep["holders_truncated"]:
                rep["warnings"].append("对象图/归因不完整（预算、读取错误或上下文缺失）；bytes 仅为已观察估计，exclusive_bytes 不可用")
            if any(p["state"] == "context_unavailable" for p in rep["proxies"]):
                rep["warnings"].append("其他线程/挂起任务的 LocalProxy 不能在当前上下文安全解引用；请在目标上下文 mark()")
            if any(p["shared"] for p in rep["proxies"]):
                rep["warnings"].append("LocalProxy 共享目标已自动去重；使用 proxy_totals.targets_union_bytes，不要相加各行")
            if rep["proxy_totals"]["groups_truncated"]:
                rep["warnings"].append("代理分组达到 max_proxy_groups；proxy_totals 是下界")
            if profile == "deep":
                fingerprint = (rep["census_meta"]["scope"], tuple(cfg["root_paths"]), cfg["roots_scope"],
                               cfg["resolve_proxies"], cfg["include_third_party"], cfg["skip_infra"],
                               cfg["include_threads"], cfg["include_asyncio"], cfg["allow_custom_sizeof"])
                previous = _STATE.get("growth_base")
                rep["growth"] = []
                if rep["census_meta"]["complete_for_scope"]:
                    if previous is not None and previous[0] == fingerprint:
                        before = previous[1]
                        growth = []
                        for name in set(before) | set(all_types):
                            a, b = before.get(name, (0, 0)), all_types.get(name, (0, 0))
                            if a != b:
                                growth.append({"type": name, "bytes_diff": b[0] - a[0], "count_diff": b[1] - a[1]})
                        growth.sort(key=lambda r: -abs(r["bytes_diff"]))
                        rep["growth"] = growth[:cfg["top"]]
                        rep["growth_since"] = previous[2]
                    else:
                        rep["warnings"].append("首次完整 census 或统计口径变化：建立类型净变化基线")
                    if _baseline_valid(cfg):
                        _STATE["growth_base"] = (fingerprint, all_types, label)
                else:
                    rep["warnings"].append("census 不完整，不更新增长基线")
                rep["growth_semantics"] = "net type count/size change; not new-object identification; includes observer effects"
        else:
            rep.update(holders=[], holders_truncated=True,
                       coverage={"graph": {"complete": False, "reasons": ["graph_phase_error"]}})
            if profile == "deep":
                rep.update(census=[], census_meta={"complete_for_scope": False, "truncated": True, "reasons": ["graph_phase_error"]})
        del roots, pool
    rep["tracing"] = {"active": tracemalloc.is_tracing(), "owned": _STATE["tracemalloc_owned"],
                      "before_target_imports": _STATE["tracemalloc_early"],
                      "history": "no allocation history before tracing started"}
    if rep["tracing"]["active"] and not rep["tracing"]["before_target_imports"]:
        rep["warnings"].append("分配点只包含 tracemalloc 开启后的记录；install() 无法追溯更早的分配")
    rep["elapsed_ms"] = round((time.monotonic() - start) * 1000, 3)
    rep["budget_overrun"] = time.monotonic() > budget.deadline
    if rep["budget_overrun"]:
        rep["warnings"].append("超过协作式时间预算；不可中断的内建调用及汇总可能造成超时")
    return rep


def _out_dir(cfg):
    return os.fspath((cfg or {}).get("out_dir") or os.environ.get("MEMWATCH_OUT") or os.path.join(os.getcwd(), "memwatch-reports"))


def _safe_filename(text, limit=60):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(text))[:limit].strip("._") or "checkpoint"


def _report_name(rep, cfg):
    prefix = _safe_filename(cfg["label_prefix"], 32) + "_" if cfg["label_prefix"] else ""
    return "%s%d_%04d_%s_%s" % (prefix, rep["pid"], rep["seq"], rep["session"], _safe_filename(rep["label"]))


def _atomic_text(path, text):
    directory = os.path.dirname(path)
    fd, temp = tempfile.mkstemp(prefix=".memwatch-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _write_report(rep, cfg):
    directory = _out_dir(cfg)
    os.makedirs(directory, exist_ok=True)
    base = rep.get("report_file") or _report_name(rep, cfg)
    rep["report_file"] = base
    rep["report_paths"] = {ext: os.path.join(directory, base + "." + ext) for ext in ("json", "txt")}
    text = render_text(rep)
    encoded = json.dumps(rep, ensure_ascii=False, indent=2, allow_nan=False)
    _atomic_text(rep["report_paths"]["txt"], text)
    _atomic_text(rep["report_paths"]["json"], encoded)
    return base


def _do_snapshot(label, profile=None, explicit=None, frame=None, enrich=None, background_generation=None):
    base = _STATE.get("config") or DEFAULT
    fail_silent = (explicit or {}).get("fail_silent", base.get("fail_silent", True))
    acquired = False
    capture_lock = None
    try:
        if background_generation is not None:
            if not _STATE["installed"] or background_generation != _STATE["generation"]:
                return {"skipped": "cancelled", "label": label}
        elif not _STATE["installed"]:
            install()
        cfg, unknown = _normalise_cfg(_STATE["config"], explicit, refresh_roots=False)
        if not isinstance(label, str):
            raise TypeError("snapshot label must be str")
        if not cfg["enabled"]:
            return {"skipped": "disabled", "label": label}
        # Non-reentrant: a sink calling mark() must not recursively capture.
        capture_lock = _STATE["lock"]
        acquired = capture_lock.acquire(blocking=False)
        if not acquired:
            return {"skipped": "busy", "label": label}
        generation = _STATE["generation"]
        counts = _STATE["label_counts"]
        if label not in counts and cfg["max_labels"] and len(counts) >= cfg["max_labels"]:
            return {"skipped": "label_capacity", "label": label}
        if cfg["max_marks_per_label"] and counts.get(label, 0) >= cfg["max_marks_per_label"]:
            return {"skipped": "label_limit", "label": label}
        counts[label] = counts.get(label, 0) + 1
        _STATE["seq"] += 1
        cfg["_warnings"] = unknown
        rep = _snapshot_impl(label, profile, explicit, frame, effective=cfg)
        if generation != _STATE["generation"] or not _STATE["installed"]:
            return {"skipped": "cancelled", "label": label}
        if enrich is not None:
            enrich(rep)
        if cfg["write_report"]:
            rep["report_file"] = _report_name(rep, cfg)
        if callable(cfg["sink"]):
            try:
                cfg["sink"](rep)
            except Exception as exc:
                if not cfg["fail_silent"]:
                    raise
                rep["errors"].append("sink: " + type(exc).__name__)
        if generation != _STATE["generation"] or not _STATE["installed"]:
            return {"skipped": "cancelled", "label": label}
        if cfg["write_report"]:
            try:
                _write_report(rep, cfg)
            except Exception as exc:
                if not cfg["fail_silent"]:
                    raise
                rep["errors"].append("write_report: " + type(exc).__name__)
                rep["report_write_failed"] = True
        if cfg["print_report"]:
            try:
                print(render_text(rep), file=sys.stderr)
            except Exception as exc:
                if not cfg["fail_silent"]:
                    raise
                rep["errors"].append("print_report: " + type(exc).__name__)
        _STATE["prev_holders"] = {h["root"]: h["bytes"] for h in rep.get("holders", [])}
        _STATE["prev_rss"] = rep.get("counters", {}).get("rss_mb")
        return rep
    except Exception as exc:
        if not fail_silent:
            raise
        return {"label": label, "errors": ["snapshot failed: " + type(exc).__name__], "status": "error"}
    finally:
        if acquired:
            # A fork inside a user callback replaces the child's global lock.
            # Release the particular lock this invocation actually acquired.
            capture_lock.release()


def snapshot(label="checkpoint", profile=None, **overrides):
    frame = _caller_frame()
    try:
        return _do_snapshot(label, profile, overrides, frame)
    finally:
        del frame


def mark(label=None, profile=None, **opts):
    frame = _caller_frame()
    try:
        label = label or _frame_label(frame)
        opts.setdefault("roots_scope", "locals")
        before, rss = dict(_STATE["prev_holders"]), _STATE["prev_rss"]
        rep = _do_snapshot(label, profile, opts, frame)
        cfg = _STATE.get("config") or DEFAULT
        brief = opts.get("brief", cfg["brief"])
        if brief is not False and not rep.get("skipped"):
            try:
                print(_render_brief(rep, before, rss, opts.get("brief_top", cfg["brief_top"])), file=sys.stderr)
            except Exception:
                if not opts.get("fail_silent", cfg["fail_silent"]):
                    raise
        return rep
    finally:
        del frame


def mark_all(label=None, profile=None, **opts):
    if not _STATE["installed"]:
        install()
    opts.setdefault("roots_scope", "all")
    return mark(label, profile if profile is not None else _STATE["config"]["all_profile"], **opts)


class node:
    def __init__(self, label, profile=None, **opts):
        self.label, self.profile, self.opts, self.entry = label, profile, opts, None

    def __enter__(self):
        self.entry = snapshot(self.label + ":enter", self.profile, **self.opts)
        return self.entry

    def __exit__(self, exc_type, exc, tb):
        def enrich(rep):
            before = (self.entry or {}).get("counters", {}).get("traced_bytes")
            after = rep.get("counters", {}).get("traced_bytes")
            rep["node_delta_mb"] = round((after - before) / 1048576, 3) if before is not None and after is not None else None
        frame = _caller_frame()
        try:
            _do_snapshot(self.label + ":exit", self.profile, self.opts, frame, enrich)
        finally:
            del frame
        return False


def watch(label=None, profile=None, **opts):
    def decorate(func):
        name = label or func.__module__ + "." + func.__qualname__
        if inspect.isasyncgenfunction(func):
            raise TypeError("watch does not wrap async generators; place mark() in the generator")
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def wrapped(*args, **kwargs):
                with node(name, profile, **opts):
                    return await func(*args, **kwargs)
        elif inspect.isgeneratorfunction(func):
            @functools.wraps(func)
            def wrapped(*args, **kwargs):
                with node(name, profile, **opts):
                    return (yield from func(*args, **kwargs))
        else:
            @functools.wraps(func)
            def wrapped(*args, **kwargs):
                with node(name, profile, **opts):
                    return func(*args, **kwargs)
        return wrapped
    return decorate


def track(name, obj, weak=False):
    if not isinstance(name, str):
        raise TypeError("track name must be str")
    with _STATE["lifecycle"]:
        if weak:
            _STATE["tracked"][name] = weakref.ref(obj)
            _STATE["weak_names"].add(name)
        else:
            _STATE["tracked"][name] = obj
            _STATE["weak_names"].discard(name)
    return obj


def untrack(name):
    with _STATE["lifecycle"]:
        _STATE["tracked"].pop(name, None)
        _STATE["weak_names"].discard(name)


def flush(clear_tracked=True):
    """Release probe baselines; by compatibility default also clear track().

    No forced gc.collect(): a measurement must not run application finalizers.
    Background triggers never call flush and never erase user registrations.
    """
    with _STATE["lifecycle"]:
        _STATE["baseline_epoch"] = _STATE.get("baseline_epoch", 0) + 1
        _STATE["prev_snapshot"] = _STATE["prev_label"] = _STATE["growth_base"] = None
        _STATE["prev_holders"] = {}
        _STATE["prev_rss"] = None
        if clear_tracked:
            _STATE["tracked"].clear()
            _STATE["weak_names"].clear()


def is_installed():
    return _STATE["installed"]


def render_text(rep):
    c = rep.get("counters", {})
    lines = ["=" * 78, "memwatch 检查点 #%s %s profile=%s" % (rep.get("seq"), rep.get("label"), rep.get("profile")),
             "RSS %s MiB; USS %s MiB; peak RSS %s MiB; traced %s MiB; %s ms" %
             (c.get("rss_mb"), c.get("uss_mb"), c.get("peak_rss_mb"), c.get("traced_mb"), rep.get("elapsed_ms"))]
    if c.get("rss_is_peak"):
        lines.append("RSS 降级值是进程历史峰值，不是当前 RSS")
    totals = rep.get("totals", {})
    if totals:
        lines.append("去重已观察量: %s B = Python %s + native %s; 映射另列 %s B (非 RSS)" %
                     (totals["accounted_bytes"], totals["python_bytes"], totals["native_bytes"], totals["mapped_bytes"]))
    if "holders" in rep:
        lines.append("\n-- 变量维度持有量（可达下界；各行不可相加；不是释放收益/分配归属）")
        for h in rep["holders"]:
            lines.append("  %10.1f KiB  %s  %s%s" % (h["bytes"] / 1024, h["root"], h.get("preview", ""),
                                                          " [截断]" if h.get("truncated") else ""))
            if len(h.get("aliases", [])) > 1:
                lines.append("    aliases: " + ", ".join(h["aliases"]))
    if rep.get("buffers"):
        lines.append("\n-- 原生缓冲区 / 映射（logical 不是额外占用，mapped 不是 RSS）")
        for b in rep["buffers"]:
            lines.append("  native=%s B mapped=%s B logical=%s B %s %s" %
                         (b["native_bytes"], b["mapped_bytes"], b["logical_bytes"], b["source"], b["first_observed_path"]))
    if rep.get("proxies"):
        lines.append("\n-- LocalProxy（同目标自动去重；不同目标也可能共享子图）")
        for p in rep["proxies"]:
            lines.append("  target=%s B; wrappers=%d; own=%s B; state=%s; partial=%s" %
                         (p.get("target_bytes"), p["proxy_count"], p["own_bytes"], p["state"], p.get("target_truncated")))
            lines.append("    " + ", ".join(p["proxies"]))
    if "census" in rep:
        lines.append("\n-- 类型普查 (%s)" % rep.get("census_meta", {}).get("scope", "legacy GC-only"))
        for name, size, count in rep["census"]:
            lines.append("  %10.1f KiB %8d objects  %s" % (size / 1024, count, name))
        if rep.get("census_meta", {}).get("truncated"):
            lines.append("  [截断] 不是完整进程内存")
    if "sites" in rep:
        s = rep["sites"]
        lines.append("\n-- 分配点 (%s / %s): %s" % (s.get("mode", "n/a"), s.get("status", "legacy"), s.get("reason", "")))
        for r in s.get("rows", []):
            lines.append("  %s KiB  %s" % (r.get("kb", r.get("kb_diff")), r.get("where")))
    if "growth" in rep:
        lines.append("\n-- 类型净变化（不是按 id 推断新增对象）")
        for g in rep["growth"]:
            lines.append("  %s bytes; %s objects; %s" % (g.get("bytes_diff", g.get("own_bytes")),
                                                       g.get("count_diff", g.get("count")), g["type"]))
    lines.extend("!! " + str(x) for x in rep.get("warnings", []) + rep.get("errors", []))
    return "\n".join(lines)


def _render_brief(rep, prev_holders=None, prev_rss=None, top=5):
    c = rep.get("counters", {})
    lines = ["[memwatch] %s  %s ms  RSS %s MiB  profile=%s" %
             (rep.get("label"), rep.get("elapsed_ms"), c.get("rss_mb"), rep.get("profile"))]
    if prev_rss is not None and c.get("rss_mb") is not None:
        lines.append("  RSS delta %+.3f MiB" % (c["rss_mb"] - prev_rss))
    for h in rep.get("holders", [])[:top]:
        previous = (prev_holders or {}).get(h["root"])
        delta = " (%+.3f MiB)" % ((h["bytes"] - previous) / 1048576) if previous is not None else ""
        lines.append("  %.3f MiB%s  %s  %s" % (h["bytes"] / 1048576, delta, h["root"], h.get("preview", "")))
    lines.extend("  ! " + str(w) for w in rep.get("warnings", []) + rep.get("errors", []))
    if rep.get("report_paths") and not rep.get("report_write_failed"):
        lines.append("  完整报告: " + rep["report_paths"]["txt"])
    return "\n".join(lines)


def report(path=None, seq=None, pid=None):
    target = os.fspath(path) if path is not None else _out_dir(_STATE.get("config"))
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as fh:
            return json.load(fh)
    candidates = []
    for name in os.listdir(target):
        if not name.endswith(".json"):
            continue
        try:
            filename = os.path.join(target, name)
            with open(filename, encoding="utf-8") as fh:
                row = json.load(fh)
            # A directory may contain arbitrary JSON, not only our reports.
            if not isinstance(row, dict):
                continue
            timestamp = row.get("ts", 0)
            if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
                continue
            if seq is not None and row.get("seq") != int(seq):
                continue
            if pid is not None and row.get("pid") != int(pid):
                continue
            candidates.append((timestamp, os.stat(filename).st_mtime_ns, row))
        except (OSError, ValueError, TypeError, OverflowError):
            continue
    if not candidates:
        raise FileNotFoundError("no report matches the requested path/seq/pid")
    return max(candidates, key=lambda item: item[:2])[2]


def diagnose(frame=None, write=True):
    if not _STATE["installed"]:
        install()
    cfg = _STATE["config"]
    frame = frame if frame is not None else _caller_frame()
    stack, cur = [], frame
    try:
        for _ in range(64):
            if cur is None:
                break
            ok, why = _frame_verdict(cur, cfg["root_paths"], cfg["include_third_party"])
            wide, _ = _frame_verdict(cur, cfg["root_paths"], cfg["include_third_party"], True)
            stack.append({"file": cur.f_code.co_filename, "func": cur.f_code.co_name,
                          "line": cur.f_lineno, "accepted": ok, "reason": why,
                          "accepted_when_widened": wide})
            cur = cur.f_back
        out = {"root_paths": cfg["root_paths"], "roots_scope": cfg["roots_scope"], "stack": stack,
               "accepted_frames": sum(s["accepted"] for s in stack),
               "would_be_accepted_if_widened": sum(s["accepted_when_widened"] for s in stack),
               "tracemalloc": tracemalloc.is_tracing(), "warnings": list(_STATE["warnings"])}
        if write:
            print(json.dumps(out, ensure_ascii=False, indent=2), file=sys.stderr)
        return out
    finally:
        del frame, cur


def _warn(message):
    if message not in _STATE["warnings"] and len(_STATE["warnings"]) < 32:
        _STATE["warnings"].append(message)


def _parse_at(spec):
    path, sep, target = spec.rpartition(":")
    if not sep:
        path, target = "", spec
    if target.isdigit():
        return {"kind": "line", "path": path, "filename": os.path.basename(path), "lineno": int(target)}
    return {"kind": "call", "path": path, "filename": os.path.basename(path), "name": target}


def _remove_at():
    state = _STATE.get("at_hook")
    if state is None:
        return
    state["done"] = True
    channel, hook = state["channel"], state["hook"]
    get = sys.gettrace if channel == "trace" else sys.getprofile
    set_hook = sys.settrace if channel == "trace" else sys.setprofile
    if get() is hook:
        set_hook(None)
    get_thread = getattr(threading, "get" + channel, None)
    if get_thread and get_thread() is hook:
        getattr(threading, "set" + channel)(None)
    _STATE["at_hook"] = None


def _install_at_trigger(spec):
    current = _STATE.get("at_hook")
    if current is not None and current["spec"] == spec:
        return
    _remove_at()
    if not spec:
        return
    target = _parse_at(spec)
    channel = "trace" if target["kind"] == "line" else "profile"
    get = sys.gettrace if channel == "trace" else sys.getprofile
    set_hook = sys.settrace if channel == "trace" else sys.setprofile
    get_thread = getattr(threading, "get" + channel, None)
    if get() is not None or (get_thread and get_thread() is not None):
        _warn("at trigger not installed: existing debugger/profiler hook is preserved")
        return
    wanted = os.path.realpath(target["path"]) if target["path"] else None
    state = {"spec": spec, "done": False, "channel": channel, "hook": None}
    guard = threading.Lock()

    def hook(frame, event, arg):
        if state["done"]:
            # Clean the global hook in a previously started worker on its next event.
            if get() is hook:
                set_hook(None)
            return None
        event_matches = event == ("line" if channel == "trace" else "call")
        if event_matches:
            matches = (frame.f_lineno == target["lineno"]) if channel == "trace" else (frame.f_code.co_name == target["name"])
            if matches and wanted is not None:
                matches = os.path.realpath(frame.f_code.co_filename) == wanted
            if matches and guard.acquire(blocking=False):
                try:
                    if not state["done"]:
                        state["done"] = True
                        _remove_at()
                        label = "at:%s:%s" % (os.path.basename(frame.f_code.co_filename), target.get("lineno", target.get("name")))
                        _do_snapshot(label, "deep", {"roots_scope": "all"}, frame)
                finally:
                    guard.release()
                return None
        return hook if channel == "trace" else None

    state["hook"] = hook
    _STATE["at_hook"] = state
    set_hook(hook)
    if get_thread is not None:
        getattr(threading, "set" + channel)(hook)
    else:
        _warn("this Python version cannot inspect threading hooks; at applies to installing thread only")


def _set_signal(enabled):
    if not hasattr(signal, "SIGUSR1"):
        return
    if threading.current_thread() is not threading.main_thread():
        if enabled or _STATE["signal_installed"]:
            _warn("signal hook changes require the main thread")
        return
    if _STATE["signal_installed"] and not enabled:
        if signal.getsignal(signal.SIGUSR1) is _STATE["signal_handler"]:
            signal.signal(signal.SIGUSR1, _STATE["orig_signal"])
        _STATE["signal_installed"] = False
        _STATE["signal_handler"] = None
    elif enabled and not _STATE["signal_installed"]:
        previous = signal.getsignal(signal.SIGUSR1)
        if previous not in (signal.SIG_DFL, signal.SIG_IGN, None):
            _warn("SIGUSR1 handler already belongs to the application; not replaced")
            return

        def handler(signum, frame):
            # A flag assignment, not Event.set() (which takes a lock).
            _STATE["signal_pending"] = True

        _STATE["orig_signal"], _STATE["signal_handler"] = previous, handler
        signal.signal(signal.SIGUSR1, handler)
        _STATE["signal_installed"] = True


def _worker(name, key, callback):
    existing = _STATE["workers"].get(name)
    if existing and existing["key"] == key and existing["thread"].is_alive():
        return
    if existing:
        existing["stop"].set()
    stop = threading.Event()

    def run():
        try:
            callback(stop)
        except Exception as exc:
            _warn("background " + name + ": " + type(exc).__name__)
    thread = threading.Thread(target=run, name="memwatch-" + name, daemon=True)
    _STATE["workers"][name] = {"key": key, "stop": stop, "thread": thread}
    thread.start()


def _configure_workers(cfg):
    wanted = set()
    generation = _STATE["generation"]

    def capture(label, profile=None):
        return _do_snapshot(label, profile, background_generation=generation)
    if _STATE["signal_installed"]:
        wanted.add("signal")

        def signal_worker(stop):
            while not stop.wait(0.1):
                if _STATE["signal_pending"]:
                    _STATE["signal_pending"] = False
                    capture("signal")
        _worker("signal", True, signal_worker)
    if cfg["watch_file"]:
        wanted.add("file")
        path = os.fspath(cfg["watch_file"])

        def file_worker(stop):
            while not stop.wait(0.1):
                if os.path.isfile(path):
                    try:
                        os.unlink(path)
                    except OSError as exc:
                        _warn("watch_file remove: " + type(exc).__name__)
                        continue
                    capture("file:" + os.path.basename(path))
        _worker("file", path, file_worker)
    if cfg["interval"] or cfg["rss_threshold_mb"]:
        wanted.add("watchdog")
        interval, threshold = cfg["interval"], cfg["rss_threshold_mb"]

        def watchdog(stop):
            next_due = time.monotonic() + interval if interval else math.inf
            last_bucket = 0
            while not stop.wait(0.1):
                now = time.monotonic()
                if now >= next_due:
                    next_due = now + interval
                    capture("interval")
                if threshold:
                    c = _phase_counters(cfg)
                    if not c.get("rss_is_peak") and c.get("rss_mb") is not None:
                        bucket = int(c["rss_mb"] // threshold)
                        if bucket > last_bucket:
                            last_bucket = bucket
                            capture("rss>%dMB" % (bucket * threshold), "deep")
        _worker("watchdog", (interval, threshold), watchdog)
    for name in list(_STATE["workers"]):
        if name not in wanted:
            _STATE["workers"].pop(name)["stop"].set()
    _STATE["threads"] = [w["thread"] for w in _STATE["workers"].values()]


def install(**kwargs):
    """Idempotent configuration; explicit kwargs override environment defaults.

    Invalid explicit configuration raises ValueError. Capture-time failures obey
    fail_silent. Changing a debugger's hooks is deliberately not supported.
    """
    with _STATE["lifecycle"]:
        base = dict(_STATE["config"] or DEFAULT)
        values, env_warnings = {}, []
        for env, key, cast in (
            ("MEMWATCH_OUT", "out_dir", str), ("MEMWATCH_PROFILE", "profile", str),
            ("MEMWATCH_ALL_PROFILE", "all_profile", str), ("MEMWATCH_TOP", "top", int),
            ("MEMWATCH_AT", "at", str), ("MEMWATCH_WATCH_FILE", "watch_file", str),
            ("MEMWATCH_FRAMES", "frames", int), ("MEMWATCH_RSS_MB", "rss_threshold_mb", float),
            ("MEMWATCH_INTERVAL", "interval", float),
            ("MEMWATCH_ROOT_PATHS", "root_paths", lambda s: [p for p in s.split(os.pathsep) if p]),
            ("MEMWATCH_RESOLVE_PROXY", "resolve_proxies", lambda s: s.lower() not in ("0", "false", "no")),
            ("MEMWATCH_ENABLED", "enabled", lambda s: s.lower() not in ("0", "false", "no")),
        ):
            if env in os.environ and key not in kwargs:
                try:
                    values[key] = cast(os.environ[env])
                except ValueError:
                    env_warnings.append("invalid environment value: " + env)
        values.update(kwargs)
        cfg, warnings = _normalise_cfg(base, values)
        fresh = not _STATE["installed"]
        if fresh:
            _STATE["stop"] = threading.Event()  # never revive an old worker's stop event
            _STATE["generation"] += 1
            _STATE["session"] = format(time.time_ns(), "x")
            _STATE["seq"] = 0
            _STATE["label_counts"] = {}
            _STATE["warnings"] = []
            _STATE["signal_pending"] = False
        _STATE["config"], _STATE["installed"] = cfg, True
        for message in warnings + env_warnings:
            _warn(message)
        if fresh and _STATE.get("forked_from_pid") is not None:
            _warn("fork child: new probe session; inherited baselines cleared; native runtime fork-safety is application responsibility")
        if cfg["trace"] and cfg["enabled"] and not tracemalloc.is_tracing():
            tracemalloc.start(cfg["frames"])
            _STATE["tracemalloc_owned"] = True
            _STATE["owned_trace_frames"] = cfg["frames"]
            _STATE["tracemalloc_early"] = False
            _STATE["prev_snapshot"] = _STATE["prev_label"] = None
        elif (not cfg["trace"] or not cfg["enabled"]) and _STATE["tracemalloc_owned"]:
            if tracemalloc.is_tracing() and tracemalloc.get_traceback_limit() == _STATE.get("owned_trace_frames"):
                tracemalloc.stop()
            _STATE["tracemalloc_owned"] = False
            _STATE["prev_snapshot"] = _STATE["prev_label"] = None
        _set_signal(cfg["on_signal"] and cfg["enabled"])
        _install_at_trigger(cfg["at"] if cfg["enabled"] else None)
        _configure_workers(cfg if cfg["enabled"] else dict(cfg, watch_file=None, interval=None, rss_threshold_mb=None))
        # Do not hand callers the mutable configuration object used by workers.
        result = {k: v for k, v in cfg.items() if not k.startswith("_")}
        result["root_paths"] = list(cfg["root_paths"])
        return result


def uninstall():
    with _STATE["lifecycle"]:
        _STATE["installed"] = False
        _STATE["generation"] += 1
        _STATE["stop"].set()
        workers = list(_STATE["workers"].values())
        for worker in workers:
            worker["stop"].set()
        _STATE["workers"], _STATE["threads"] = {}, []
        _set_signal(False)
        _remove_at()
        for worker in workers:
            if worker["thread"] is not threading.current_thread():
                worker["thread"].join(timeout=0.2)
        if _STATE["tracemalloc_owned"] and tracemalloc.is_tracing():
            if tracemalloc.get_traceback_limit() == _STATE.get("owned_trace_frames"):
                tracemalloc.stop()
        _STATE["tracemalloc_owned"] = _STATE["tracemalloc_early"] = False
        flush()
        _STATE["config"] = None


def _after_fork_child():
    """Drop inherited probe locks/workers without acquiring any old lock.

    Do not start threads or take a snapshot in an at-fork callback. The next
    explicit install/mark starts a fresh child session using the saved config.
    This isolates probe state; it cannot make an application's native code safe
    to fork. Parent state is unaffected and tracked registrations are preserved.
    """
    parent_pid = _STATE.get("pid")
    _STATE.update(lock=threading.Lock(), lifecycle=threading.RLock(),
                  stop=threading.Event(), trigger=threading.Event(),
                  workers={}, threads=[], installed=False,
                  generation=_STATE["generation"] + 1,
                  pid=os.getpid(), forked_from_pid=parent_pid,
                  seq=0, label_counts={}, signal_pending=False,
                  prev_snapshot=None, prev_label=None, growth_base=None,
                  prev_holders={}, prev_rss=None)
    _STATE["baseline_epoch"] = _STATE.get("baseline_epoch", 0) + 1
    _remove_at()
    _set_signal(False)


if hasattr(os, "register_at_fork") and not _STATE.get("at_fork_registered"):
    os.register_at_fork(after_in_child=_after_fork_child)
    _STATE["at_fork_registered"] = True


def _cli_run(argv):
    if not argv:
        print("usage: python -m memwatch run SCRIPT.py [args...]", file=sys.stderr)
        return 2
    script = os.path.abspath(argv[0])
    previous_argv, previous_path = sys.argv[:], sys.path[:]
    previous_main = sys.modules.get("__main__")
    module = types.ModuleType("__main__")
    module.__file__, module.__package__, module.__spec__ = script, None, None
    sys.modules["__main__"] = module
    sys.argv = argv[:]
    sys.path.insert(0, os.path.dirname(script))
    code = 0
    try:
        install(root_paths=[os.path.dirname(script)])
        _STATE["tracemalloc_early"] = tracemalloc.is_tracing()
        snapshot("boot")
        with open(script, "rb") as fh:
            source = compile(fh.read(), script, "exec")
        exec(source, module.__dict__)
    except SystemExit as exc:
        if exc.code is None:
            code = 0
        elif isinstance(exc.code, int):
            code = exc.code
        else:
            print(exc.code, file=sys.stderr)
            code = 1
    finally:
        snapshot("shutdown")
        uninstall()
        sys.argv, sys.path = previous_argv, previous_path
        sys.modules["__main__"] = previous_main
    return code


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        print("commands: run SCRIPT [args...] | report [json|txt|dir] | list [dir] | doctor")
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "run":
        return _cli_run(rest)
    if cmd == "report":
        path = rest[0] if rest else None
        if path and path.endswith(".txt"):
            with open(path, encoding="utf-8") as fh:
                print(fh.read())
        else:
            print(render_text(report(path)))
        return 0
    if cmd == "list":
        directory = rest[0] if rest else _out_dir(_STATE.get("config"))
        if not os.path.isdir(directory):
            return 1
        for name in sorted(os.listdir(directory)):
            if name.endswith(".txt"):
                print(name)
        return 0
    if cmd == "doctor":
        print("Run memwatch.diagnose() inside the target process.", file=sys.stderr)
        return 2
    print("unknown command: " + cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    # sitecustomize may have loaded the canonical module before python -m runs it.
    # Reuse that instance instead of installing two independent probes/hooks.
    existing = sys.modules.get("memwatch")
    if existing is not None and existing is not sys.modules[__name__]:
        sys.exit(existing.main())
    sys.modules["memwatch"] = sys.modules[__name__]
    sys.exit(main())
