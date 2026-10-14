"""Internal bounded object graph and memory accounting engine for memwatch.

Keep this module alongside memwatch.py. No mandatory third-party dependencies.
Reachability is not allocation ownership, retained size, or process RSS.
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

__version__ = "2.0"
DEFAULT = {
    "out_dir": None, "enabled": True, "profile": "standard", "all_profile": "deep",
    "top": 20, "trace": True, "frames": 5, "root_paths": None,
    "roots_scope": "all", "include_third_party": False,
    "include_threads": True, "include_asyncio": True, "resolve_proxies": True,
    "skip_infra": True, "max_objects": 500_000, "max_edges": 2_000_000,
    "max_roots": 4096, "max_propagations": 2_000_000, "max_root_links": 2_000_000,
    "max_census_objects": 500_000, "max_proxy_groups": 1024, "max_seconds": 5.0, "count_objects": True,
    "brief": None, "brief_top": 5, "preview": True, "max_marks_per_label": 50,
    "max_labels": 1024, "auto_widen": True, "write_report": True,
    "print_report": False, "on_signal": True, "watch_file": None, "at": None,
    "rss_threshold_mb": None, "interval": None, "sink": None,
    "fail_silent": True, "label_prefix": "", "allow_custom_sizeof": False,
}
_SELF_FILE = os.path.realpath(__file__)
_OBSERVER_FILES = {_SELF_FILE, os.path.join(os.path.dirname(_SELF_FILE), "memwatch.py"),
                   os.path.join(os.path.dirname(_SELF_FILE), "memprobe.py")}
_INFRA_TYPES = (types.ModuleType, type, types.FunctionType, types.CodeType,
                types.FrameType, types.BuiltinFunctionType)
_GC_HEAD = sys.getsizeof([]) - list.__sizeof__([])
_STATE = {
    "installed": False, "config": None, "seq": 0, "generation": 0,
    "prev_snapshot": None, "prev_label": None, "growth_base": None,
    "tracemalloc_owned": False, "tracemalloc_early": False,
    "lock": threading.Lock(), "lifecycle": threading.RLock(),
    "tracked": {}, "weak_names": set(), "stop": threading.Event(),
    "threads": [], "workers": {}, "trigger": threading.Event(),
    "signal_pending": False, "signal_installed": False, "orig_signal": None,
    "signal_handler": None, "at_hook": None, "label_counts": {},
    "prev_holders": {}, "prev_rss": None, "warnings": [],
}


def _typename(obj):
    t = type(obj)
    try:
        return "%s.%s" % (type.__getattribute__(t, "__module__"),
                          type.__getattribute__(t, "__qualname__"))
    except Exception:
        return "unknown"


def _is_proxy(obj):
    # isinstance(obj, ...) may ask LocalProxy for its forwarded __class__.
    try:
        return any("_get_current_object" in vars(c) for c in type(obj).__mro__)
    except Exception:
        return False


def _resolve_proxy_result(obj):
    try:
        target = object.__getattribute__(obj, "_get_current_object")()
        return ("cycle", None, "self reference") if target is obj else ("bound", target, None)
    except RuntimeError as exc:
        return "unbound", None, type(exc).__name__
    except Exception as exc:
        return "error", None, type(exc).__name__


def _resolve_proxy(obj):
    state, target, _ = _resolve_proxy_result(obj)
    return target if state == "bound" else None


def _proxy_state(obj):
    return _resolve_proxy_result(obj)[0] if _is_proxy(obj) else ""


def _sizeof(obj, allow_custom=False):
    """Shallow size, bypassing forwarded proxy and Python user __sizeof__ hooks.

    Built-in C size descriptors are used directly. They remain cooperative calls,
    not a sandbox or a hard real-time guarantee. NumPy owners may INCLUDE payload.
    """
    try:
        if _is_proxy(obj):
            value = object.__sizeof__(obj)
        else:
            method = next((vars(c)["__sizeof__"] for c in type(obj).__mro__
                           if "__sizeof__" in vars(c)), object.__sizeof__)
            if not allow_custom and not isinstance(method, (types.MethodDescriptorType,
                                                            types.WrapperDescriptorType)):
                method = object.__sizeof__
            value = method(obj)
        value = max(0, int(value))
        if getattr(type(obj), "__flags__", 0) & (1 << 14):
            value += _GC_HEAD
        return value
    except Exception:
        return 0


def _safe_preview(obj, limit=48):
    t = type(obj)
    try:
        if obj is None or t in (bool, float, complex):
            return repr(obj)[:limit]
        if t is int:
            return repr(obj)[:limit] if obj.bit_length() < 512 else "int(%d bits)" % obj.bit_length()
        if t is str:
            return repr(obj[:limit]) + ("...(len=%d)" % len(obj) if len(obj) > limit else "")
        if t in (bytes, bytearray):
            return repr(bytes(obj[:limit])) + ("...(len=%d)" % len(obj) if len(obj) > limit else "")
        if t in (list, tuple, set, frozenset, dict):
            return "%s(len=%d)" % (t.__name__, len(obj))
    except Exception:
        pass
    return _typename(obj)


def _describe(obj, limit=80):
    return _safe_preview(obj, limit)


def _size_hint(obj):
    return len(obj) if type(obj) in (list, tuple, set, frozenset, dict, str, bytes, bytearray) else None


def _is_infra(obj):
    return issubclass(type(obj), _INFRA_TYPES)


def _numpy_type(obj):
    # Never import NumPy just to measure a process that does not use it.
    mod = sys.modules.get("numpy")
    cls = vars(mod).get("ndarray") if issubclass(type(mod), types.ModuleType) else None
    return cls if isinstance(cls, type) and issubclass(type(obj), cls) else None


def _measure(obj, cfg):
    own = _sizeof(obj, cfg.get("allow_custom_sizeof", False))
    native = mapped = 0
    logical = None
    source = "shallow"
    note = None
    array_type = _numpy_type(obj)
    try:
        if array_type is not None:
            # Use base descriptors, not overridden subclass properties.
            nbytes = int(array_type.nbytes.__get__(obj))
            logical = nbytes
            flags = array_type.flags.__get__(obj)
            if flags.owndata:
                # ndarray.__sizeof__ already includes its own data allocation.
                own = max(0, int(array_type.__sizeof__(obj)) - nbytes)
                if getattr(type(obj), "__flags__", 0) & (1 << 14):
                    own += _GC_HEAD
                native = nbytes
                source = "numpy_owned_buffer"
            else:
                source = "numpy_view_follow_base"
                if array_type.base.__get__(obj) is None:
                    note = "numpy external backing has no inspectable owner"
        elif type(obj) is mmap.mmap:
            mapped = len(obj)  # mapping length, NOT file size and NOT resident bytes
            logical = mapped
            source = "mmap_virtual_extent"
        elif type(obj) is memoryview:
            logical = obj.nbytes
            source = "memoryview_follow_exporter"
    except (ValueError, BufferError):
        note = "closed_or_released_buffer"
    except Exception as exc:
        note = "buffer_size_error:" + type(exc).__name__
    return own, native, mapped, source, note, logical


def _children(obj):
    """Lazy common-container edges; never repr/property-inspect arbitrary objects."""
    t = type(obj)
    if t is dict:
        for k, v in obj.items():
            yield k
            yield v
        return
    if t in (list, tuple, set, frozenset):
        yield from obj
        return
    array_type = _numpy_type(obj)
    if array_type is not None:
        base = array_type.base.__get__(obj)
        if base is not None:
            yield base
        dtype = array_type.dtype.__get__(obj)
        if dtype.hasobject:
            # Plain object arrays contain Python references invisible to GC.
            if dtype.names:
                # Do not add temporary field views created by observation to the
                # measured graph. Visit only their actual Python object elements.
                yield from _numpy_object_elements(obj, array_type)
            else:
                yield from array_type.flat.__get__(obj)
        return
    if t is memoryview:
        yield obj.obj
        return
    # gc.get_referents is one non-preemptible call for unknown extension types.
    yield from gc.get_referents(obj)


def _numpy_object_elements(obj, array_type):
    dtype = array_type.dtype.__get__(obj)
    if dtype.names:
        for name in dtype.names:
            field = array_type.__getitem__(obj, name)
            if array_type.dtype.__get__(field).hasobject:
                yield from _numpy_object_elements(field, array_type)
    else:
        yield from array_type.flat.__get__(obj)


class _Budget:
    def __init__(self, cfg, deadline=None):
        self.cfg = cfg
        self.deadline = time.monotonic() + cfg["max_seconds"] if deadline is None else deadline
        self.reasons = set()
        self.stop = cfg.get("_stop")

    def alive(self):
        if self.stop is not None and self.stop.is_set():
            self.reasons.add("cancelled")
            return False
        if time.monotonic() >= self.deadline:
            self.reasons.add("max_seconds")
            return False
        return True


def _caller_frame(depth=1):
    try:
        frame = sys._getframe(depth)
        while frame is not None and os.path.realpath(frame.f_code.co_filename) in _OBSERVER_FILES:
            frame = frame.f_back
        return frame
    except Exception:
        return None


def _frame_label(frame):
    if frame is None:
        return "unknown"
    return "%s:%s:%s" % (os.path.basename(frame.f_code.co_filename), frame.f_lineno, frame.f_code.co_name)


def _frame_root(frame):
    if frame is None:
        return "unknown"
    return "%s:%s" % (frame.f_code.co_filename, frame.f_code.co_name)


def _is_virtual_filename(name):
    return name.startswith("<") and name.endswith(">")


def _site_prefixes():
    return [os.path.realpath(p) + os.sep for k, p in sysconfig.get_paths().items()
            if k in ("stdlib", "purelib", "platlib") and p]


def _under_lib(path, prefixes):
    return any(path == p.rstrip(os.sep) or path.startswith(p) for p in prefixes)


def _under(path, prefixes):
    return _under_lib(path, prefixes)


def _lib_prefixes():
    return _site_prefixes()


def _module_path(mod):
    if not issubclass(type(mod), types.ModuleType):
        return None
    path = types.ModuleType.__getattribute__(mod, "__dict__").get("__file__")
    return os.path.realpath(path) if isinstance(path, str) else None


def _auto_root_paths():
    libs = _site_prefixes()
    candidates = [os.getcwd()]
    if sys.argv and sys.argv[0]:
        candidates.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    for mod in list(sys.modules.values()):
        try:
            path = _module_path(mod)
            if path and path not in _OBSERVER_FILES:
                candidates.append(os.path.dirname(path))
        except Exception:
            continue
    return sorted({os.path.realpath(p) for p in candidates if p
                   and os.path.realpath(p) != os.path.dirname(os.path.realpath(p))
                   and not _under_lib(os.path.realpath(p), libs)})


def _path_accepted(filename, cfg):
    if _is_virtual_filename(filename):
        return True
    real = os.path.realpath(filename)
    if real in _OBSERVER_FILES:
        return False
    if cfg["include_third_party"]:
        return True
    if cfg.get("_widen"):
        return not _under_lib(real, _site_prefixes())
    return any(real == p or real.startswith(p.rstrip(os.sep) + os.sep) for p in cfg["root_paths"])


def _frame_verdict(frame, root_paths, include_tp, widen=False, libs=None, diag=None):
    cfg = {"root_paths": root_paths, "include_third_party": include_tp, "_widen": widen}
    filename = frame.f_code.co_filename
    ok = bool(filename) and _path_accepted(filename, cfg)
    if not ok and diag is not None:
        rejected = diag.setdefault("rejected_frames", [])
        if filename not in rejected and len(rejected) < 8:
            rejected.append(filename)
    return ok, "accepted" if ok else "outside root_paths or observer frame"


def _frame_is_first_party(frame, root_paths, include_tp, widen=False, libs=None, diag=None):
    return _frame_verdict(frame, root_paths, include_tp, widen, libs, diag)[0]


def _module_is_first_party(mod, root_paths):
    path = _module_path(mod)
    if path is None:
        return issubclass(type(mod), types.ModuleType) and vars(mod).get("__name__") == "__main__"
    return _path_accepted(path, {"root_paths": root_paths, "include_third_party": False})


def _root_members(label, value, kind):
    """Expose class data/defaults/closures without following function globals."""
    if type(value) is types.FunctionType:
        for suffix, obj in (("defaults", value.__defaults__), ("kwdefaults", value.__kwdefaults__),
                            ("closure", value.__closure__)):
            if obj:
                yield label + "." + suffix, obj, kind
    elif issubclass(type(value), type):
        for attr, obj in type.__getattribute__(value, "__dict__").items():
            if not attr.startswith("__") and not _is_infra(obj) and not issubclass(type(obj), (property, staticmethod, classmethod)):
                yield label + "." + attr, obj, kind
    elif not _is_infra(value):
        yield label, value, kind


def _roots_from_modules(cfg, diag=None):
    diag = {} if diag is None else diag
    budget = cfg.get("_budget")
    out = []
    seen_modules = set()
    for name, mod in list(sys.modules.items()):
        if budget and not budget.alive():
            break
        if not issubclass(type(mod), types.ModuleType) or id(mod) in seen_modules:
            continue
        seen_modules.add(id(mod))
        try:
            path = _module_path(mod)
            if path in _OBSERVER_FILES or name == "memwatch":
                continue
            if path and not _path_accepted(path, cfg):
                rejected = diag.setdefault("rejected_modules", [])
                if len(rejected) < 8:
                    rejected.append(path)
                continue
            if not path and name != "__main__":
                continue
            for attr, value in types.ModuleType.__getattribute__(mod, "__dict__").items():
                if budget and not budget.alive():
                    break
                if attr.startswith("__"):
                    continue
                for row in _root_members(name + "." + attr, value, "module"):
                    out.append(row)
                    diag.setdefault("root_contexts", {})[row[0]] = True
                    if len(out) >= cfg["max_roots"]:
                        diag["roots_truncated"] = True
                        return out
        except RuntimeError:
            diag["roots_mutated"] = True
        except Exception:
            diag["module_read_errors"] = diag.get("module_read_errors", 0) + 1
    return out


def _stack_rows(cfg, frame, prefix, kind, context_ok, diag):
    out, depth, repeats = [], 0, {}
    budget = cfg.get("_budget")
    while frame is not None and depth < 64:
        if budget and not budget.alive():
            break
        if _frame_is_first_party(frame, cfg["root_paths"], cfg["include_third_party"], cfg.get("_widen", False), diag=diag):
            where = _frame_root(frame)
            n = repeats.get(where, 0)
            repeats[where] = n + 1
            if n:
                where += "#%d" % n
            try:
                for name, value in frame.f_locals.items():
                    if budget and not budget.alive():
                        break
                    if name.startswith("__"):
                        continue
                    for row in _root_members(prefix + where + ":" + name, value, kind):
                        out.append(row)
                        diag.setdefault("root_contexts", {})[row[0]] = context_ok
                        if len(out) >= cfg["max_roots"]:
                            diag["roots_truncated"] = True
                            return out
            except RuntimeError:
                diag["roots_mutated"] = True
        frame = frame.f_back
        depth += 1
    if frame is not None and depth == 64:
        diag["stack_depth_limited"] = True
    return out


def _roots_from_stack(cfg, frame, diag=None):
    return _stack_rows(cfg, frame, "", "frame", True, {} if diag is None else diag)


def _roots_from_frames(cfg, diag=None):
    if not cfg["include_threads"]:
        return []
    diag = {} if diag is None else diag
    out = []
    for tid, frame in list(sys._current_frames().items()):
        if tid == threading.get_ident():
            continue
        out.extend(_stack_rows(cfg, frame, "thread%d:" % tid, "frame", False, diag))
        if len(out) >= cfg["max_roots"]:
            diag["roots_truncated"] = True
            return out[:cfg["max_roots"]]
    return out


def _roots_from_asyncio(cfg, diag=None):
    if not cfg["include_asyncio"]:
        return []
    diag = {} if diag is None else diag
    # Do not import a framework as a side effect of observation.
    aio = sys.modules.get("asyncio")
    if aio is None:
        return []
    try:
        tasks = list(aio.all_tasks())
        current = aio.current_task()
    except RuntimeError:
        diag["asyncio_scope"] = "no_running_loop_in_snapshot_thread"
        return []
    out = []
    for task in tasks:
        coro = task.get_coro()
        seen = set()
        for depth in range(32):
            if coro is None or id(coro) in seen:
                break
            seen.add(id(coro))
            if cfg.get("_budget") and not cfg["_budget"].alive():
                return out
            frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
            if frame is not None and _path_accepted(frame.f_code.co_filename, cfg):
                # Only this coroutine frame, never its event-loop/back stack.
                try:
                    for name, value in frame.f_locals.items():
                        if cfg.get("_budget") and not cfg["_budget"].alive():
                            return out
                        if name.startswith("__"):
                            continue
                        label = "task:%s@%x:%s:%s" % (task.get_name(), id(task), _frame_root(frame), name)
                        for row in _root_members(label, value, "asyncio"):
                            out.append(row)
                            diag.setdefault("root_contexts", {})[row[0]] = task is current
                            if len(out) >= cfg["max_roots"]:
                                diag["roots_truncated"] = True
                                return out
                except RuntimeError:
                    diag["roots_mutated"] = True
            coro = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None)
    return out


def collect_roots(cfg, frame=None, diag=None):
    """Keep aliases. Identity deduplication belongs in the graph, not in labels."""
    diag = {} if diag is None else diag
    rows = []
    with _STATE["lifecycle"]:
        tracked = list(_STATE["tracked"].items())
        weak_names = set(_STATE["weak_names"])
    for name, obj in tracked:
        if cfg.get("_budget") and not cfg["_budget"].alive():
            break
        if name in weak_names:
            obj = obj()
            if obj is None:
                continue
        label = "tracked:" + name
        if len(rows) >= cfg["max_roots"]:
            diag["roots_truncated"] = True
            break
        rows.append((label, obj, "tracked"))
        diag.setdefault("root_contexts", {})[label] = True
    scope = cfg.get("roots_scope", "all")
    if scope != "tracked":
        rows.extend(_roots_from_stack(cfg, frame, diag))
        if scope == "all":
            rows.extend(_roots_from_modules(cfg, diag))
            rows.extend(_roots_from_frames(cfg, diag))
            rows.extend(_roots_from_asyncio(cfg, diag))
    if len(rows) > cfg["max_roots"]:
        diag["roots_truncated"] = True
        rows = rows[:cfg["max_roots"]]
    if not rows and cfg["auto_widen"] and not cfg.get("_widen") and scope != "tracked":
        wide = dict(cfg, _widen=True)
        rows = collect_roots(wide, frame, diag)
        if rows:
            diag["auto_widened"] = True
    return rows


class _Object:
    __slots__ = ("obj", "edges", "info", "name", "path", "context", "expanded",
                 "proxy", "state", "target", "target_index", "error", "mask")

    def __init__(self, obj, info, path, context):
        self.obj, self.info, self.path, self.context = obj, info, path, context
        self.edges, self.name = [], _typename(obj)
        self.expanded, self.proxy = False, _is_proxy(obj)
        self.state, self.target, self.target_index, self.error = "not_scanned", None, None, None
        self.mask = 0


def _add_type(table, name, info):
    if name not in table and len(table) >= 2048:
        name = "<other_types>"
    row = table.setdefault(name, {"type": name, "count": 0, "python_bytes": 0,
                                  "native_bytes": 0, "mapped_bytes": 0, "bytes": 0})
    row["count"] += 1
    row["python_bytes"] += info[0]
    row["native_bytes"] += info[1]
    row["mapped_bytes"] += info[2]
    row["bytes"] += info[0] + info[1]


def _edge_iter(obj):
    if type(obj) is dict:
        for key, value in obj.items():
            label = _safe_preview(key, 32) if type(key) in (str, int) else _typename(key)
            yield ".key[" + label + "]", key
            yield "[" + label + "]", value
    else:
        for i, child in enumerate(_children(obj)):
            yield "->ref[%d]" % i, child


def _bits(mask):
    while mask:
        bit = mask & -mask
        yield bit.bit_length() - 1
        mask ^= bit


def _scan_roots(roots, cfg, deadline=None, diag=None, gc_pool=None):
    """One identity graph for holders, aliases, proxy targets and types.

    Root and proxy-target reachability are propagated as bit sets. Both propagation
    and expansion of those sets into report rows have explicit operation budgets.
    No graph or measured object is returned or saved in _STATE.
    """
    diag = {} if diag is None else diag
    budget = cfg.get("_budget") or _Budget(cfg, deadline)
    nodes, index, queue, groups, group_index = [], {}, deque(), [], {}
    graph_types, notes = {}, set()
    proxy_refs = {}
    edges = 0
    contexts = diag.get("root_contexts", {})

    def add(obj, path, context):
        oid = id(obj)
        previous = index.get(oid)
        if previous is not None:
            if nodes[previous].proxy:
                refs = proxy_refs[previous]
                refs[1] += 1
                path = path[:256]
                if path not in refs[0]:
                    if len(refs[0]) < 64:
                        refs[0].add(path)
                    else:
                        refs[2] = True
            if context and not nodes[previous].context:
                nodes[previous].context = True
                queue.append(previous)
            return previous
        if not budget.alive():
            return None
        if len(nodes) >= cfg["max_objects"]:
            budget.reasons.add("max_objects")
            return None
        i = len(nodes)
        info = _measure(obj, cfg)
        record = _Object(obj, info, path[:256], context)
        nodes.append(record)
        if record.proxy:
            proxy_refs[i] = [{record.path}, 1, False]
        index[oid] = i
        queue.append(i)
        _add_type(graph_types, record.name, info)
        if info[4] and len(notes) < 32:
            notes.add(info[4])
        return i

    for label, obj, kind in roots:
        if len(groups) >= cfg["max_roots"]:
            diag["roots_truncated"] = True
            break
        oid = id(obj)
        gi = group_index.get(oid)
        context = contexts.get(label, True)
        i = add(obj, label, context)
        alias = {"label": label, "kind": kind, "resolution_context_available": context}
        if gi is None:
            group_index[oid] = len(groups)
            groups.append({"node": i, "aliases": [alias], "obj": obj})
        elif alias not in groups[gi]["aliases"]:
            groups[gi]["aliases"].append(alias)

    while queue and budget.alive():
        i = queue.popleft()
        record = nodes[i]
        obj = record.obj
        if record.expanded:
            # At most one false -> true context upgrade per ordinary node.
            if record.proxy and record.state == "context_unavailable" and record.context:
                record.expanded = False
            else:
                if record.context:
                    for child in record.edges:
                        if not nodes[child].context:
                            nodes[child].context = True
                            queue.append(child)
                continue
        record.expanded = True
        if record.proxy:
            if not cfg["resolve_proxies"]:
                record.state = "disabled"
                continue
            if not record.context:
                record.state = "context_unavailable"
                continue
            record.state, record.target, record.error = _resolve_proxy_result(obj)
            refs = (("._get_current_object()", record.target),) if record.state == "bound" else ()
        elif cfg["skip_infra"] and _is_infra(obj):
            continue
        else:
            refs = _edge_iter(obj)
        try:
            for suffix, child in refs:
                if not budget.alive():
                    break
                if edges >= cfg["max_edges"]:
                    budget.reasons.add("max_edges")
                    break
                edges += 1
                j = add(child, record.path + suffix, record.context)
                if j is None:
                    break
                record.edges.append(j)
                if record.proxy:
                    record.target_index = j
        except Exception as exc:
            budget.reasons.add("referents_error")
            if len(notes) < 32:
                notes.add(record.name + ":" + type(exc).__name__)
        if "max_edges" in budget.reasons or "max_objects" in budget.reasons:
            break
    graph_reasons = set(budget.reasons)
    if queue:
        graph_reasons.add("pending_objects")
    if diag.get("roots_truncated"):
        graph_reasons.add("max_roots")
    if diag.get("roots_mutated") or diag.get("module_read_errors"):
        graph_reasons.add("root_read_incomplete")
    if diag.get("stack_depth_limited"):
        graph_reasons.add("stack_depth")
    graph_complete = not graph_reasons

    # Same wrapper with many aliases != many wrappers. Unbound proxies must NOT
    # all be grouped together as though they shared a None target.
    proxy_groups, target_groups = [], {}
    proxy_groups_limited = False
    distinct_proxies = 0
    for i, record in enumerate(nodes):
        if not record.proxy:
            continue
        distinct_proxies += 1
        key = ("bound", id(record.target)) if record.state == "bound" else (record.state, i)
        pi = target_groups.get(key)
        if pi is None:
            if len(proxy_groups) >= cfg["max_proxy_groups"]:
                proxy_groups_limited = True
                continue
            pi = len(proxy_groups)
            target_groups[key] = pi
            proxy_groups.append({"state": record.state, "target_index": record.target_index,
                                 "target_id": hex(id(record.target)) if record.state == "bound" else None,
                                 "target_type": _typename(record.target) if record.state == "bound" else None,
                                 "members": [], "own_bytes": 0, "paths": set(), "errors": [],
                                 "reference_edges": 0, "paths_truncated": False})
        pg = proxy_groups[pi]
        pg["members"].append(i)
        pg["own_bytes"] += record.info[0]
        paths, count, limited = proxy_refs[i]
        pg["reference_edges"] += count
        pg["paths_truncated"] |= limited
        for path in paths:
            if path not in pg["paths"]:
                if len(pg["paths"]) < 64:
                    pg["paths"].add(path)
                else:
                    pg["paths_truncated"] = True
        if record.error and len(pg["errors"]) < 8:
            pg["errors"].append(record.error)

    # Root masks and target masks share the same propagation. They do not change
    # the physical union; a target is never traversed again just to size a proxy.
    nroots = len(groups)
    for r, group in enumerate(groups):
        if group["node"] is not None:
            nodes[group["node"]].mask |= 1 << r
    # Foreign-context root bits cannot cross a proxy resolution edge. Identity
    # aliases retain per-reference context metadata in the report.
    resolution_mask = sum(1 << r for r, group in enumerate(groups)
                          if any(a["resolution_context_available"] for a in group["aliases"]))
    for p, pg in enumerate(proxy_groups):
        resolution_mask |= 1 << (nroots + p)
        if pg["target_index"] is not None:
            nodes[pg["target_index"]].mask |= 1 << (nroots + p)
    propagation = deque(i for i, n in enumerate(nodes) if n.mask)
    pending = set(propagation)
    operations = 0
    propagation_complete = True
    while propagation:
        if not budget.alive() or operations >= cfg["max_propagations"]:
            propagation_complete = False
            break
        i = propagation.popleft()
        pending.discard(i)
        record = nodes[i]
        for j in record.edges:
            if not budget.alive() or operations >= cfg["max_propagations"]:
                propagation_complete = False
                break
            operations += 1
            outgoing = record.mask & resolution_mask if record.proxy else record.mask
            updated = nodes[j].mask | outgoing
            if updated != nodes[j].mask:
                nodes[j].mask = updated
                if j not in pending:
                    propagation.append(j)
                    pending.add(j)
        if not propagation_complete:
            break

    root_mask = (1 << nroots) - 1
    root_stats = [[0, 0, 0, 0] for _ in groups]  # bytes, objects, mapped, observed single-root
    proxy_stats = [[0, 0, 0] for _ in proxy_groups]
    union = {"python_bytes": 0, "native_bytes": 0, "mapped_bytes": 0,
             "accounted_bytes": 0, "objects": len(nodes), "shared_bytes": 0,
             "single_root_observed_bytes": 0, "unattributed_bytes": 0,
             "proxy_targets_union_bytes": 0}
    links = 0
    rows_complete = True
    # Physical/type totals are already bounded by max_objects. Do not throw away
    # these totals when attribution exhausts its own budget.
    for record in nodes:
        own, native, mapped = record.info[:3]
        size = own + native
        union["python_bytes"] += own
        union["native_bytes"] += native
        union["mapped_bytes"] += mapped
        union["accounted_bytes"] += size
        mask = record.mask & root_mask
        if not mask:
            union["unattributed_bytes"] += size
        elif mask & (mask - 1):
            union["shared_bytes"] += size
        else:
            union["single_root_observed_bytes"] += size
        if record.mask >> nroots:
            union["proxy_targets_union_bytes"] += size
        if not rows_complete:
            continue
        for r in _bits(record.mask):
            if links >= cfg["max_root_links"] or not budget.alive():
                rows_complete = False
                break
            links += 1
            if r < nroots:
                row = root_stats[r]
                row[0] += size
                row[1] += 1
                row[2] += mapped
                if mask and not mask & (mask - 1):
                    row[3] += size
            else:
                row = proxy_stats[r - nroots]
                row[0] += size
                row[1] += 1
                row[2] += mapped
    complete = graph_complete and propagation_complete and rows_complete
    union["exclusive_bytes"] = union["single_root_observed_bytes"] if complete else None
    union["attribution_complete"] = complete
    union["scope"] = "selected_root_graph; not RSS; mappings separate"
    rank = {"tracked": 0, "module": 1, "frame": 2, "asyncio": 3}
    holders = []
    for i, group in enumerate(groups):
        aliases = sorted(group["aliases"], key=lambda a: (rank.get(a["kind"], 9), a["label"]))
        primary = aliases[0]
        obj = group["obj"]
        record = nodes[group["node"]] if group["node"] is not None else None
        size, count, mapped, single = root_stats[i]
        holders.append({"root": primary["label"], "kind": primary["kind"],
                        "aliases": [a["label"] for a in aliases], "references": aliases,
                        "bytes": size, "reachable_bytes": size, "objects": count,
                        "mapped_bytes": mapped, "exclusive_bytes": single if complete else None,
                        "is_proxy": bool(record and record.proxy),
                        "proxy_state": record.state if record and record.proxy else "",
                        "own_python_bytes": record.info[0] if record else None,
                        "own_native_bytes": record.info[1] if record else None,
                        "size_source": record.info[3] if record else "not_scanned",
                        "type": _typename(obj), "truncated": not complete,
                        "measurement": "reachable; non-additive; not retained size",
                        "len": _size_hint(obj) if cfg["preview"] else None,
                        "preview": _safe_preview(obj) if cfg["preview"] else None})
    holders.sort(key=lambda r: (-r["bytes"], r["root"]))
    proxies = []
    for i, pg in enumerate(proxy_groups):
        size, count, mapped = proxy_stats[i]
        available = pg["target_index"] is not None
        paths = sorted(pg["paths"])
        proxies.append({"state": pg["state"], "target_id": pg["target_id"],
                        "target_type": pg["target_type"], "target_bytes": size if available else None,
                        "target_objects": count, "target_mapped_bytes": mapped if available else None,
                        "target_truncated": not complete or not available,
                        "own_bytes": pg["own_bytes"], "proxy_count": len(pg["members"]),
                        "proxy_ids": [hex(id(nodes[j].obj)) for j in pg["members"][:64]],
                        "proxy_ids_truncated": len(pg["members"]) > 64,
                        "observed_reference_edges": pg["reference_edges"],
                        "alias_count": len(paths), "proxies": paths[:64],
                        "references_truncated": pg["paths_truncated"],
                        "shared": pg["state"] == "bound" and len(pg["members"]) > 1,
                        "shared_warning_mb": round(size * max(0, len(pg["members"]) - 1) / 1048576, 2),
                        "resolution_context": "snapshot thread and current task only",
                        "errors": pg["errors"][:8]})
    proxies.sort(key=lambda r: -(r["target_bytes"] or 0))
    attribution_reasons = []
    if not propagation_complete:
        attribution_reasons.append("max_propagations_or_time")
    if not rows_complete:
        attribution_reasons.append("max_root_links_or_time")

    buffers = []
    for record in nodes:
        if record.info[3] == "shallow" and record.info[4] is None:
            continue
        buffers.append({"type": record.name, "object_id": hex(id(record.obj)),
                        "first_observed_path": record.path, "source": record.info[3],
                        "python_bytes": record.info[0], "native_bytes": record.info[1],
                        "mapped_bytes": record.info[2], "logical_bytes": record.info[5],
                        "note": record.info[4]})
    buffers.sort(key=lambda r: -(r["native_bytes"] + r["mapped_bytes"]))
    census_types = {k: dict(v) for k, v in graph_types.items()}
    census_scanned, census_reasons = 0, []
    if gc_pool is not None:
        for obj in gc_pool:
            if not budget.alive():
                census_reasons.append("max_seconds")
                break
            if census_scanned >= cfg["max_census_objects"]:
                census_reasons.append("max_census_objects")
                break
            census_scanned += 1
            if id(obj) not in index:
                _add_type(census_types, _typename(obj), _measure(obj, cfg))
    census_rows = sorted(census_types.values(), key=lambda r: (-r["bytes"], r["type"]))
    graph_rows = sorted(graph_types.values(), key=lambda r: (-r["bytes"], r["type"]))
    top = cfg["top"]
    census_scope = "gc_tracked_plus_reachable" if gc_pool is not None else "reachable_graph"
    return {
        "holders": holders[:top], "holders_scanned": len(nodes), "holders_truncated": not complete,
        "holders_total": len(holders), "holders_omitted": max(0, len(holders) - top),
        "proxies": proxies[:top], "proxies_total": len(proxies),
        "proxy_totals": {"distinct_proxies": distinct_proxies,
                         "groups_truncated": proxy_groups_limited,
                         "distinct_bound_targets": sum(p["state"] == "bound" for p in proxies),
                         "targets_union_bytes": union["proxy_targets_union_bytes"],
                         "truncated": not complete or proxy_groups_limited},
        "totals": union, "types": graph_rows[:top], "buffers": buffers[:top],
        "buffers_total": len(buffers), "buffers_omitted": max(0, len(buffers) - top),
        "type_totals": {"objects": len(nodes), "bytes": union["accounted_bytes"],
                        "mapped_bytes": union["mapped_bytes"]},
        "census": [(r["type"], r["bytes"], r["count"]) for r in census_rows[:top]],
        "census_details": census_rows[:top],
        "census_meta": {"scope": census_scope, "gc_objects_examined": census_scanned,
                        "gc_objects_available": len(gc_pool) if gc_pool is not None else None,
                        "complete_for_scope": graph_complete and not census_reasons,
                        "truncated": not graph_complete or bool(census_reasons),
                        "reasons": census_reasons, "types_total": len(census_rows),
                        "types_grouped": "<other_types>" in census_types,
                        "types_omitted": max(0, len(census_rows) - top),
                        "bytes": sum(r["bytes"] for r in census_rows),
                        "objects": sum(r["count"] for r in census_rows),
                        "mapped_bytes": sum(r["mapped_bytes"] for r in census_rows)},
        "_census_all": {r["type"]: (r["bytes"], r["count"]) for r in census_rows},
        "coverage": {"proxies": {"groups_complete": not proxy_groups_limited,
                                 "reason": "max_proxy_groups" if proxy_groups_limited else None},
                     "graph": {"complete": graph_complete, "reasons": sorted(graph_reasons),
                               "objects": len(nodes), "edges_examined": edges},
                     "attribution": {"complete": complete, "reasons": attribution_reasons,
                                     "propagations": operations, "root_links": links},
                     "native": {"complete": False,
                                "adapters": ["numpy.ndarray", "memoryview", "mmap.mmap"],
                                "notes": sorted(notes)},
                     "consistency": "best_effort_live_graph; not stop-the-world"},
    }


def _deep_size(root, cfg, deadline):
    config = dict(DEFAULT, **cfg)
    result = _scan_roots([("root", root, "tracked")], config, deadline)
    return result["totals"]["accounted_bytes"], result["holders_scanned"], result["holders_truncated"]


def _phase_holders(cfg, deadline, frame=None, diag=None):
    diag = {} if diag is None else diag
    rep = _scan_roots(collect_roots(cfg, frame, diag), cfg, deadline, diag)
    return rep["holders"], rep["holders_scanned"], rep["holders_truncated"]


def _phase_proxies(cfg, deadline, frame=None, diag=None):
    diag = {} if diag is None else diag
    return _scan_roots(collect_roots(cfg, frame, diag), cfg, deadline, diag)["proxies"]


def _phase_census(cfg):
    # Compatibility helper. The main pipeline additionally supplies root objects
    # because a GC census alone cannot discover untracked NumPy/bytes/dict objects.
    config = dict(DEFAULT, **cfg)
    result = _scan_roots([], config, gc_pool=gc.get_objects())
    return result["census"]


def _phase_counters(cfg):
    c = {"objects_tracked": None, "rss_mb": None, "uss_mb": None, "pss_mb": None,
         "vms_mb": None, "peak_rss_mb": None, "traced_mb": None,
         "traced_peak_mb": None, "tracemalloc": tracemalloc.is_tracing()}
    if c["tracemalloc"]:
        current, peak = tracemalloc.get_traced_memory()
        c.update(traced_bytes=current, traced_peak_bytes=peak,
                 traced_mb=round(current / 1048576, 3), traced_peak_mb=round(peak / 1048576, 3),
                 trace_metadata_bytes=tracemalloc.get_tracemalloc_memory())
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        c["peak_rss_bytes"] = int(peak if sys.platform == "darwin" else peak * 1024)
        c["peak_rss_mb"] = round(c["peak_rss_bytes"] / 1048576, 3)
    except (ImportError, OSError, AttributeError):
        pass
    try:
        import psutil
        proc = psutil.Process()
        info = proc.memory_info()
        c.update(rss_bytes=info.rss, rss_mb=round(info.rss / 1048576, 3),
                 vms_bytes=info.vms, vms_mb=round(info.vms / 1048576, 3), rss_source="psutil")
        try:
            full = proc.memory_full_info()
            for name in ("uss", "pss"):
                value = getattr(full, name, None)
                if value is not None:
                    c[name + "_bytes"] = int(value)
                    c[name + "_mb"] = round(value / 1048576, 3)
        except Exception as exc:
            c["private_memory_unavailable"] = type(exc).__name__
    except Exception as exc:
        c["rss_source"] = "resource_peak_fallback"
        c["rss_bytes"] = c.get("peak_rss_bytes")
        c["rss_mb"] = c.get("peak_rss_mb")
        c["rss_is_peak"] = True
        c["psutil_unavailable"] = type(exc).__name__
    return c


def _is_self_trace(stat):
    try:
        filename = os.path.realpath(stat.traceback[0].filename)
        return filename in _OBSERVER_FILES or filename == os.path.realpath(tracemalloc.__file__)
    except Exception:
        return False


