"""Compatibility API backed by memwatch's bounded, identity-deduplicated engine.

Version 2 changes accounting semantics: reachable sizes are not additive or
retained sizes; Growth reports type NET changes, not guessed new object IDs.
Prefer memwatch.mark()/mark_all() for structured coverage and native-buffer data.
"""
from __future__ import annotations

import gc
import sys
import time
import tracemalloc
import types
import warnings

import memwatch as _mw

INFRA_TYPES = _mw._INFRA_TYPES
shallow = _mw._sizeof
is_proxy = _mw._is_proxy
resolve_proxy = _mw._resolve_proxy
_proxy_state = _mw._proxy_state
def describe(obj, max_len=60):
    return _mw._describe(obj, max_len)


def roots_from_module(mod, include_private=False):
    rows = []
    if not issubclass(type(mod), types.ModuleType):
        return rows
    for name, value in types.ModuleType.__getattribute__(mod, "__dict__").items():
        if name.startswith("__") or (name.startswith("_") and not include_private):
            continue
        rows.extend((label, obj) for label, obj, _ in _mw._root_members(
            mod.__name__ + "." + name, value, "module"))
    return rows


def roots_from_frame(frame, prefix=""):
    return [(prefix + _mw._frame_root(frame) + ":" + name, value)
            for name, value in frame.f_locals.items() if not name.startswith("__")]


def roots_from_threads():
    cfg, _ = _mw._normalise_cfg(_mw.DEFAULT)
    frame = _mw._caller_frame()
    try:
        return [(label, obj) for label, obj, _ in
                (_mw._roots_from_stack(cfg, frame) + _mw._roots_from_frames(cfg))]
    finally:
        del frame


def collect_roots(modules=None, include_threads=True, include_private=False):
    """Returned roots themselves are strong references; discard them after use."""
    cfg, _ = _mw._normalise_cfg(_mw.DEFAULT)
    if modules is None:
        rows = [(label, obj) for label, obj, _ in _mw._roots_from_modules(cfg)]
    else:
        rows = [row for mod in modules for row in roots_from_module(mod, include_private)]
    if include_threads:
        rows.extend(roots_from_threads())
    return rows  # aliases deliberately retained


def _analysis(roots, resolve_proxies=True, skip_infra=True, limit=500_000, census=False):
    cfg = dict(_mw.DEFAULT, max_objects=limit, resolve_proxies=resolve_proxies,
               skip_infra=skip_infra, top=4096)
    pool = gc.get_objects() if census else None
    result = _mw._scan_roots([(name, obj, "tracked") for name, obj in roots], cfg, gc_pool=pool)
    result.pop("_census_all", None)
    return result


def retained_attribution(roots, resolve_proxies=True, skip_infra=True, limit=2_000_000):
    """Legacy tuple shape; v2 values are REACHABLE, not retained or additive.

    A warning carries truncation because this legacy return type has no coverage
    field. Use memwatch for a structured report with explicit completeness.
    """
    result = _analysis(roots, resolve_proxies, skip_infra, limit)
    if result["holders_truncated"] or result["holders_omitted"]:
        warnings.warn("memprobe attribution is partial; use memwatch coverage fields", RuntimeWarning, stacklevel=2)
    return [(" / ".join(r["aliases"]), r["bytes"], r["objects"], r["is_proxy"], r["proxy_state"])
            for r in result["holders"]]


def _referents(obj, resolve_proxies=True):
    if is_proxy(obj):
        if resolve_proxies:
            state, target, _ = _mw._resolve_proxy_result(obj)
            if state == "bound":
                return [target]
        return []
    return list(_mw._children(obj))


def find_proxies(roots, limit=500_000, max_seconds=5.0):
    """Legacy strong-reference results; traversal is now bounded."""
    seen, rows, edges = set(), [], 0
    deadline = time.monotonic() + max_seconds
    for label, root in roots:
        stack = [iter((root,))]
        while stack:
            if len(seen) >= limit or edges >= limit * 4 or time.monotonic() >= deadline:
                warnings.warn("memprobe proxy discovery is partial", RuntimeWarning, stacklevel=2)
                return rows
            try:
                obj = next(stack[-1])
                edges += 1
            except StopIteration:
                stack.pop()
                continue
            except Exception:
                stack.pop()
                continue
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            if is_proxy(obj):
                rows.append((label, obj))
            elif not _mw._is_infra(obj):
                stack.append(iter(_mw._children(obj)))
    return rows


def census(top=25, min_bytes=1):
    result = _analysis(collect_roots(), census=True)
    if result["census_meta"]["truncated"]:
        warnings.warn("memprobe census is partial; not total process memory", RuntimeWarning, stacklevel=2)
    return [row for row in result["census"] if row[1] >= min_bytes][:top]


def owner_chain(obj, max_depth=5, ignore_files=("memprobe.py",), max_seconds=0.2):
    """Bounded heuristic only, NOT evidence of allocation or exclusive ownership.

    gc.get_referrers itself is non-preemptible. No application repr is invoked.
    """
    parts, seen = [], {id(obj)}
    current = obj
    deadline = time.monotonic() + max_seconds
    for _ in range(max_depth):
        if time.monotonic() >= deadline:
            parts.append("<time limit>")
            break
        selected = None
        for ref in gc.get_referrers(current)[:10000]:
            if id(ref) in seen:
                continue
            if type(ref) is types.FrameType:
                if any(ref.f_code.co_filename.endswith(f) for f in ignore_files):
                    continue
            if type(ref) is dict:
                if ref is globals() or ref is vars(_mw):
                    continue
                for key, value in ref.items():
                    if time.monotonic() >= deadline:
                        break
                    if value is current:
                        selected = (ref, "[" + _mw._safe_preview(key) + "]")
                        break
            elif type(ref) in (list, tuple, set):
                selected = (ref, "<" + type(ref).__name__ + ">")
            if selected:
                break
        if selected is None:
            break
        current, label = selected
        seen.add(id(current))
        parts.append(label)
    return "heuristic: " + " <- ".join(parts)


class Growth:
    """Net type sizes/counts, not new object identity (ids can be reused).

    Legacy tuple shape: (type, NET bytes, 0, NET count). The old atoms column is
    zero because untracked reachable objects now have their OWN deduplicated type.
    A truncated census never advances the baseline.
    """
    def __init__(self):
        self.base = self._inventory()

    @staticmethod
    def _inventory():
        result = _analysis(collect_roots(), census=True)
        if result["census_meta"]["truncated"] or result["census_meta"]["types_omitted"]:
            warnings.warn("incomplete type census; growth baseline not advanced", RuntimeWarning, stacklevel=3)
            return None
        return {name: (size, count) for name, size, count in result["census"]}

    def diff(self, top=15, advance=False):
        now = self._inventory()
        if now is None:
            return []
        rows = []
        if self.base is not None:
            for name in set(now) | set(self.base):
                before, after = self.base.get(name, (0, 0)), now.get(name, (0, 0))
                if before != after:
                    rows.append((name, after[0] - before[0], 0, after[1] - before[1]))
        if advance or self.base is None:
            self.base = now
        return sorted(rows, key=lambda r: -abs(r[1]))[:top]


class Probe:
    def __init__(self, frames=25, trace=True):
        self.frames, self.trace, self.prev, self.marks = frames, trace, None, []
        self._owned = trace and not tracemalloc.is_tracing()
        if self._owned:
            tracemalloc.start(frames)

    def snapshot(self):
        return tracemalloc.take_snapshot() if self.trace and tracemalloc.is_tracing() else None

    def diff(self, label, top=15, group="lineno"):
        snap = self.snapshot()
        if snap is None:
            return []
        before, self.prev = self.prev, snap
        stats = snap.compare_to(before, group) if before is not None else snap.statistics(group)
        lines = ["[%s] %s (only traced allocations)" % (label, "delta" if before else "absolute")]
        for st in [st for st in stats if not _mw._is_self_trace(st)][:top]:
            size = st.size_diff if before else st.size
            count = st.count_diff if before else st.count
            lines.append("  %+.1f KiB  %+d allocations  %s" % (size / 1024, count, st.traceback))
        return lines

    @staticmethod
    def _holder_lines(label, result, top):
        lines = ["[%s] reachable bytes; NOT additive or retained; partial=%s" % (label, result["holders_truncated"])]
        for row in result["holders"][:top]:
            lines.append("  %.1f KiB  %s" % (row["bytes"] / 1024, " / ".join(row["aliases"])))
        return lines

    @staticmethod
    def _proxy_lines(label, result):
        lines = ["[%s] proxy targets deduplicated union: %s B; partial=%s" %
                 (label, result["proxy_totals"]["targets_union_bytes"], result["proxy_totals"]["truncated"])]
        for row in result["proxies"]:
            lines.append("  state=%s wrappers=%d target=%s B aliases=%s" %
                         (row["state"], row["proxy_count"], row["target_bytes"], row["proxies"]))
        return lines

    def holders(self, label, roots, top=15, resolve_proxies=True, skip_infra=True):
        return self._holder_lines(label, _analysis(roots, resolve_proxies, skip_infra), top)

    def proxies(self, label, roots):
        return self._proxy_lines(label, _analysis(roots))

    def report(self, label, roots=None, top=15):
        lines = self.diff(label, top)
        if roots is not None:
            result = _analysis(roots)
            lines.extend(self._holder_lines(label, result, top))
            lines.extend(self._proxy_lines(label, result))
        print("\n".join(lines))

    def close(self):
        self.prev = None
        if self._owned and tracemalloc.is_tracing() and tracemalloc.get_traceback_limit() == self.frames:
            tracemalloc.stop()
        self._owned = False
