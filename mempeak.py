"""mempeak: whole-run peak memory monitor with a plug-in, low-intrusion interface.

Zero code change (recommended for OOM work; survives the target being OOM-killed):

    python -m mempeak run --out before.json -- python train.py --epochs 1
    python -m mempeak run --out after.json  -- python train.py --epochs 1
    python -m mempeak compare before.json after.json

In-process (one line around the code you care about):

    import mempeak
    with mempeak.monitor(out="peak.json") as m:
        main()
    print(m.report.summary())

Optional phase markers tell you WHICH stage produced the peak. They are no-ops
unless a monitor (in-process, or the `run` wrapper of a parent process) is active,
so they can stay in production code:

    with mempeak.phase("load"):
        data = load()

    @mempeak.phase("train")
    def train(): ...

Only the standard library is required. Linux reads /proc and cgroup files; on
other platforms psutil is used when installed. See README_MEMPEAK.md.
"""
from __future__ import annotations

import atexit
import contextlib
import functools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

__all__ = ["start", "stop", "monitor", "profile", "phase", "active", "Report",
           "load", "compare", "rss_bytes", "hwm_bytes", "main"]
__version__ = "1.0"

_LINUX = sys.platform.startswith("linux") and os.path.exists("/proc/self/statm")
try:
    _PAGE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError, OSError):
    _PAGE = 4096
_PHASE_ENV = "MEMPEAK_PHASE_FILE"
_MAX_TIMELINE = 2000
_UNITS = {"": 1, "B": 1, "K": 1024, "KB": 1024, "KIB": 1024, "M": 1024 ** 2, "MB": 1024 ** 2,
          "MIB": 1024 ** 2, "G": 1024 ** 3, "GB": 1024 ** 3, "GIB": 1024 ** 3,
          "T": 1024 ** 4, "TB": 1024 ** 4, "TIB": 1024 ** 4}


# --------------------------------------------------------------------------- readers
def _read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:
        return None


def rss_bytes(pid=None):
    """Current resident set size of a process (default: this one), or None."""
    if _LINUX:
        try:
            return int(_read("/proc/%s/statm" % (pid or "self")).split()[1]) * _PAGE
        except (OSError, ValueError, IndexError):
            return None
    ps = _psutil()
    if ps is not None:
        try:
            return (ps.Process(pid) if pid else ps.Process()).memory_info().rss
        except Exception:
            return None
    return None


def _status(pid):
    out = {}
    for line in _read("/proc/%s/status" % pid).splitlines():
        key, _, value = line.partition(":")
        if key in ("VmRSS", "VmHWM", "VmSwap", "RssAnon", "RssFile", "RssShmem"):
            out[key] = int(value.split()[0]) * 1024
    return out


def hwm_bytes(pid=None):
    """Kernel-recorded peak RSS (exact, not sampled) of a process, or None."""
    if _LINUX:
        try:
            return _status(pid or "self").get("VmHWM")
        except (OSError, ValueError):
            return None
    if not pid:
        try:
            import resource
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return peak if sys.platform == "darwin" else peak * 1024
        except Exception:
            pass
    ps = _psutil()
    if ps is not None:
        try:
            info = (ps.Process(pid) if pid else ps.Process()).memory_info()
            return getattr(info, "peak_wset", None)  # Windows only
        except Exception:
            return None
    return None


def _pss_bytes(pid):
    try:
        for line in _read("/proc/%s/smaps_rollup" % pid).splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _children_map():
    """ppid -> [pid] for every visible process (Linux /proc scan)."""
    kids = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            stat = _read("/proc/%s/stat" % name)
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(name))
    return kids


def _process_tree(root):
    if _LINUX:
        kids = _children_map()
        pids, i = [root], 0
        while i < len(pids):
            pids.extend(kids.get(pids[i], ()))
            i += 1
        return pids
    ps = _psutil()
    if ps is not None:
        try:
            return [root] + [c.pid for c in ps.Process(root).children(recursive=True)]
        except Exception:
            pass
    return [root]


class _Cgroup:
    """Memory cgroup (v1 or v2) of a process: what the container OOM killer looks at."""

    def __init__(self, pid="self"):
        self.version = self.dir = None
        if not _LINUX:
            return
        try:
            groups = _read("/proc/%s/cgroup" % pid).splitlines()
            mounts = _read("/proc/self/mountinfo").splitlines()
        except OSError:
            return
        v1 = v2 = None
        for line in groups:
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            if parts[0] == "0" and parts[1] == "":
                v2 = parts[2]
            elif "memory" in parts[1].split(","):
                v1 = parts[2]
        for line in mounts:
            fields = line.split()
            try:
                sep = fields.index("-")
            except ValueError:
                continue
            fstype, root, mnt = fields[sep + 1], fields[3], fields[4]
            opts = fields[sep + 3].split(",") if len(fields) > sep + 3 else []
            if v1 is not None and fstype == "cgroup" and "memory" in opts:
                self._pick(1, mnt, root, v1, "memory.usage_in_bytes")
            elif v2 is not None and fstype == "cgroup2" and self.version is None:
                self._pick(2, mnt, root, v2, "memory.current")
            if self.version == 1:
                break

    def _pick(self, version, mnt, root, path, probe):
        rel = path[len(root):] if root != "/" and path.startswith(root) else path
        for cand in (os.path.join(mnt, rel.lstrip("/")), mnt):
            if os.path.exists(os.path.join(cand, probe)):
                self.version, self.dir = version, cand
                return

    def _int(self, name):
        try:
            text = _read(os.path.join(self.dir, name)).strip()
        except (OSError, TypeError):
            return None
        if text == "max":
            return None
        try:
            value = int(text)
        except ValueError:
            return None
        return None if value >= 1 << 62 else value  # v1 "unlimited" is a huge number

    def _oom_kills(self):
        name = "memory.events" if self.version == 2 else "memory.oom_control"
        try:
            for line in _read(os.path.join(self.dir, name)).splitlines():
                key, _, value = line.partition(" ")
                if key == "oom_kill":
                    return int(value)
        except (OSError, TypeError, ValueError):
            pass
        return None

    def usage(self):
        if self.version is None:
            return None
        return self._int("memory.current" if self.version == 2 else "memory.usage_in_bytes")

    def read(self):
        if self.version is None:
            return None
        v2 = self.version == 2
        return {"version": self.version, "path": self.dir,
                "usage_bytes": self.usage(),
                "peak_bytes": self._int("memory.peak" if v2 else "memory.max_usage_in_bytes"),
                "limit_bytes": self._int("memory.max" if v2 else "memory.limit_in_bytes"),
                "oom_kills": self._oom_kills()}


# --------------------------------------------------------------------------- helpers
def _fmt(n):
    if n is None:
        return "n/a"
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%s%.0f %s" % (sign, n, unit) if unit == "B" else "%s%.1f %s" % (sign, n, unit)
        n /= 1024.0


def parse_size(text):
    """'512M', '2G', '1.5GiB', '1048576' -> bytes."""
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*", str(text))
    if not m or m.group(2).upper() not in _UNITS:
        raise ValueError("bad size: %r (use e.g. 512M, 2G)" % (text,))
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])


def _write_json(path, data):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mempeak-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _max(*values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


# --------------------------------------------------------------------------- report
class Report(dict):
    """Plain dict (JSON-serialisable) with a human summary."""

    @property
    def peak_bytes(self):
        return self["peak"]["peak_bytes"]

    def save(self, path):
        _write_json(path, self)
        return path

    def summary(self):
        p, lines = self["peak"], []
        head = "[mempeak] %s  status=%s  %.2fs  samples=%d @ %.0fms" % (
            self.get("mode"), self.get("status"), self.get("duration_s") or 0.0,
            self.get("samples", 0), (self.get("interval_s") or 0) * 1000)
        lines.append(head)
        ex = self.get("exit")
        if ex:
            lines.append("  exit: code=%s signal=%s%s" % (
                ex.get("code"), ex.get("signal_name") or ex.get("signal"),
                "  <-- 可能被 OOM killer 杀死" if ex.get("likely_oom") else
                ("  <-- 超过 --kill-above，已被 mempeak 终止" if ex.get("killed_by_mempeak") else "")))
        lines.append("  PEAK            %s   (at %.2fs, phase=%s)" % (
            _fmt(p["peak_bytes"]), p.get("sampled_at_s") or 0.0, p.get("phase_at_peak") or "-"))
        lines.append("  ├ kernel HWM    %s   (exact peak RSS of the largest single process)" % _fmt(p.get("hwm_bytes")))
        lines.append("  ├ sampled RSS   %s   (sum over %d process(es) seen)" % (
            _fmt(p.get("sampled_bytes")), p.get("max_processes") or 1))
        if p.get("pss_bytes") is not None:
            lines.append("  ├ sampled PSS   %s" % _fmt(p.get("pss_bytes")))
        if p.get("python_heap_bytes") is not None:
            lines.append("  ├ Python heap   %s   (tracemalloc peak)" % _fmt(p.get("python_heap_bytes")))
        lines.append("  └ baseline %s → final %s" % (_fmt(self.get("baseline_bytes")), _fmt(self.get("final_bytes"))))
        cg = self.get("cgroup")
        if cg:
            limit = cg.get("limit_bytes")
            pct = " = %.0f%% of limit %s" % (100.0 * cg["sampled_peak_bytes"] / limit, _fmt(limit)) \
                if limit and cg.get("sampled_peak_bytes") else " (no limit)"
            lines.append("  cgroup v%s: peak usage %s%s; oom_kill +%s" % (
                cg.get("version"), _fmt(cg.get("sampled_peak_bytes")), pct, cg.get("oom_kills_delta")))
        if self.get("phases"):
            lines.append("  %-28s %6s %9s %12s %12s" % ("phase", "count", "time(s)", "peak", "Δ enter→exit"))
            for ph in self["phases"]:
                lines.append("  %-28s %6d %9.2f %12s %12s%s" % (
                    ph["name"][:28], ph["count"], ph["total_s"], _fmt(ph.get("peak_bytes")),
                    _fmt(ph.get("delta_bytes")), "  *exact" if ph.get("exact") else ""))
        for note in self.get("notes", []):
            lines.append("  note: " + note)
        return "\n".join(lines)


def load(path):
    with open(path, encoding="utf-8") as f:
        return Report(json.load(f))


# --------------------------------------------------------------------------- phases
class _PhaseBook:
    """Active phase stacks keyed by (pid, thread) and per-phase aggregates."""

    def __init__(self):
        self.stacks = {}
        self.stats = {}
        self.open = {}

    def label(self):
        names = []
        for key in sorted(self.stacks, key=str):
            if self.stacks[key]:
                names.append("/".join(self.stacks[key]))
        return " | ".join(names) or None

    def active_names(self):
        return {n for stack in self.stacks.values() for n in stack}

    def enter(self, key, name, value, t):
        self.stacks.setdefault(key, []).append(name)
        st = self.stats.setdefault(name, {"name": name, "count": 0, "total_s": 0.0, "peak_bytes": None,
                                          "delta_bytes": None, "first_s": t})
        self.open[(key, name, len(self.stacks[key]))] = (t, value)
        st["peak_bytes"] = _max(st["peak_bytes"], value)

    def exit(self, key, name, value, t):
        stack = self.stacks.get(key) or []
        depth = len(stack)
        if name in stack:
            while stack and stack[-1] != name:  # tolerate unbalanced inner phases
                stack.pop()
            depth = len(stack)
            stack.pop()
        st = self.stats.get(name)
        if st is None:
            return
        t0, v0 = self.open.pop((key, name, depth), (t, None))
        st["count"] += 1
        st["total_s"] += max(0.0, t - t0)
        st["peak_bytes"] = _max(st["peak_bytes"], value)
        if v0 is not None and value is not None:
            st["delta_bytes"] = value - v0

    def observe(self, value, names=None):
        for name in self.active_names() if names is None else names:
            st = self.stats.get(name)
            if st is not None:
                st["peak_bytes"] = _max(st["peak_bytes"], value)

    def rows(self):
        return sorted(self.stats.values(), key=lambda s: s["first_s"])


# --------------------------------------------------------------------------- core sampler
class _Monitor:
    def __init__(self, interval=0.01, out=None, flush_every=1.0, pss=False, python_heap=False,
                 exact_phases=False, pid=None, tree=False, label=None, command=None, quiet=False):
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self.interval = float(interval)
        self.out = out
        self.flush_every = flush_every
        self.pss = pss and _LINUX
        self.python_heap = python_heap
        self.exact_phases = exact_phases and _LINUX and pid is None
        self.pid = pid                  # None -> this process (in-process mode)
        self.tree = tree
        self.label = label
        self.command = command
        self.quiet = quiet
        self.lock = threading.RLock()
        self.phases = _PhaseBook()
        self.cg = _Cgroup(pid or "self")
        self.notes = []
        self.timeline = []
        self.stride = 1
        self.samples = 0
        self.stop_evt = threading.Event()
        self.thread = None
        self.report = None
        self.exit_info = None
        self.on_sample = None
        self._started_tracemalloc = False
        self._hwm_folded = None         # max HWM read before any clear_refs reset
        self._phase_file = None
        self._phase_pos = 0

    # -- lifecycle
    def start(self):
        self.t0 = time.monotonic()
        self.wall0 = time.time()
        self.baseline = self._sample_value()[0]
        self.peak = {"sampled_bytes": self.baseline, "sampled_at_s": 0.0, "phase_at_peak": None,
                     "pss_bytes": None, "max_processes": 1, "hwm_by_pid": {}}
        cg = self.cg.read()
        self.cg_before = cg
        self.cg_peak = cg["usage_bytes"] if cg else None
        if self.python_heap and self.pid is None:
            import tracemalloc
            if not tracemalloc.is_tracing():
                tracemalloc.start()
                self._started_tracemalloc = True
            if hasattr(tracemalloc, "reset_peak"):
                tracemalloc.reset_peak()
            else:
                self.notes.append("tracemalloc.reset_peak unavailable (<3.9): heap peak includes pre-start history")
        self.thread = threading.Thread(target=self._loop, name="mempeak-sampler", daemon=True)
        self.thread.start()
        return self

    def _loop(self):
        next_flush = time.monotonic() + (self.flush_every or 0)
        while not self.stop_evt.wait(self.interval):
            try:
                self.tick()
                if self.out and self.flush_every and time.monotonic() >= next_flush:
                    next_flush = time.monotonic() + self.flush_every
                    self.build("running").save(self.out)
            except Exception as exc:  # never break the host program
                with self.lock:
                    msg = "sampler error: %s: %s" % (type(exc).__name__, exc)
                    if msg not in self.notes:
                        self.notes.append(msg)

    def finish(self, status="finished"):
        self.stop_evt.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)
        self.tick()
        rep = self.build(status)
        if self._started_tracemalloc:
            import tracemalloc
            tracemalloc.stop()
        if self.out:
            try:
                rep.save(self.out)
            except OSError as exc:
                rep.setdefault("notes", []).append("could not write %s: %s" % (self.out, exc))
        self.report = rep
        return rep

    # -- measurement
    def _pids(self):
        if self.pid is None:
            return [None]
        return _process_tree(self.pid) if self.tree else [self.pid]

    def _sample_value(self):
        pids = self._pids()
        total, alive = 0, 0
        for p in pids:
            v = rss_bytes(p)
            if v is not None:
                total += v
                alive += 1
        return (total if alive else None), pids

    def tick(self):
        value, pids = self._sample_value()
        t = time.monotonic() - self.t0
        with self.lock:
            self._drain_phase_file(value)
            label = self.phases.label()
        cg_usage = self.cg.usage()
        with self.lock:
            self.samples += 1
            if value is not None and (self.peak["sampled_bytes"] is None or value > self.peak["sampled_bytes"]):
                self.peak.update(sampled_bytes=value, sampled_at_s=round(t, 4), phase_at_peak=label)
            self.peak["max_processes"] = max(self.peak["max_processes"], len(pids))
            self.phases.observe(value)
            if cg_usage is not None:
                self.cg_peak = _max(self.cg_peak, cg_usage)
            if self.pss and value is not None:
                pss = sum(_pss_bytes(p or "self") or 0 for p in pids)
                self.peak["pss_bytes"] = _max(self.peak["pss_bytes"], pss)
            if self.pid is not None:
                for p in pids:
                    h = hwm_bytes(p)
                    if h is not None:
                        self.peak["hwm_by_pid"][p] = _max(self.peak["hwm_by_pid"].get(p), h)
            if self.samples % self.stride == 0:
                self.timeline.append([round(t, 3), value, cg_usage, label])
                if len(self.timeline) >= _MAX_TIMELINE:
                    self.timeline = self.timeline[::2]
                    self.stride *= 2
        if self.on_sample is not None:
            self.on_sample(value, t)
        return value

    # -- exact per-phase peaks (in-process, Linux): fold VmHWM into active phases, then reset it
    def _fold_hwm(self):
        h = hwm_bytes()
        self._hwm_folded = _max(self._hwm_folded, h)
        self.phases.observe(h)

    def _reset_hwm(self):
        try:
            with open("/proc/self/clear_refs", "w") as f:
                f.write("5")
        except OSError as exc:
            self.exact_phases = False
            self.notes.append("exact_phases disabled: cannot reset VmHWM (%s)" % exc)

    def _fold_heap(self):
        import tracemalloc
        if not tracemalloc.is_tracing():
            return
        peak = tracemalloc.get_traced_memory()[1]
        self.heap_peak = _max(getattr(self, "heap_peak", None), peak)
        for name in self.phases.active_names():
            st = self.phases.stats[name]
            st["python_heap_peak_bytes"] = _max(st.get("python_heap_peak_bytes"), peak)

    def _reset_heap(self):
        import tracemalloc
        if tracemalloc.is_tracing() and hasattr(tracemalloc, "reset_peak"):
            tracemalloc.reset_peak()

    def phase_event(self, kind, name, key=None, t=None):
        t = time.monotonic() if t is None else t
        with self.lock:
            if self.pid is None:
                # credit the peak so far to the phases active until now ...
                if self.exact_phases:
                    self._fold_hwm()
                if self.python_heap:
                    self._fold_heap()
                value = rss_bytes()
            else:
                value = self._sample_value()[0]
            key = key or (os.getpid(), threading.get_ident())
            rel = t - self.t0
            if kind == "enter":
                self.phases.enter(key, name, value, rel)
            else:
                self.phases.exit(key, name, value, rel)
                if self.exact_phases:
                    self.phases.stats[name]["exact"] = True
            if self.pid is None:
                # ... then restart the kernel/tracemalloc peak so the next span starts clean
                if self.exact_phases:
                    self._reset_hwm()
                if self.python_heap:
                    self._reset_heap()

    def _drain_phase_file(self, value):
        if not self._phase_file:
            return
        try:
            with open(self._phase_file, "rb") as f:
                f.seek(self._phase_pos)
                chunk = f.read()
        except OSError:
            return
        end = chunk.rfind(b"\n") + 1
        self._phase_pos += end
        for raw in chunk[:end].splitlines():
            try:
                t, pid, tid, kind, name = raw.decode("utf-8", "replace").split("\t", 4)
                key, t = (int(pid), int(tid)), float(t)
            except ValueError:
                continue
            rel = t - self.t0
            if kind == "enter":
                self.phases.enter(key, name, value, rel)
            else:
                self.phases.exit(key, name, value, rel)
                # markers are read at the next sample, so enter/exit RSS are not the child's
                # values at that instant; keep the peak, drop the misleading delta
                self.phases.stats[name]["delta_bytes"] = None

    # -- report
    def build(self, status):
        with self.lock:
            now = time.monotonic()
            hwm_by_pid = self.peak["hwm_by_pid"]
            if self.pid is None:
                if self.exact_phases:
                    self._fold_hwm()
                hwm = _max(self._hwm_folded, hwm_bytes())
            else:
                hwm = _max(*hwm_by_pid.values())
            if self.exit_info and self.exit_info.get("ru_maxrss_bytes"):
                hwm = _max(hwm, self.exit_info["ru_maxrss_bytes"])
            heap = None
            if self.python_heap and self.pid is None:
                self._fold_heap()
                heap = getattr(self, "heap_peak", None)
            peak = {"peak_bytes": _max(self.peak["sampled_bytes"], hwm),
                    "hwm_bytes": hwm,
                    "sampled_bytes": self.peak["sampled_bytes"],
                    "sampled_at_s": self.peak["sampled_at_s"],
                    "phase_at_peak": self.peak["phase_at_peak"],
                    "pss_bytes": self.peak["pss_bytes"],
                    "python_heap_bytes": heap,
                    "max_processes": self.peak["max_processes"]}
            cg = None
            after = self.cg.read()
            if after:
                cg = dict(after)
                cg["sampled_peak_bytes"] = _max(self.cg_peak, after.get("usage_bytes"))
                cg["peak_before_run_bytes"] = (self.cg_before or {}).get("peak_bytes")
                before = (self.cg_before or {}).get("oom_kills")
                cg["oom_kills_delta"] = (after["oom_kills"] - before) \
                    if after.get("oom_kills") is not None and before is not None else None
            notes = list(self.notes)
            if peak["max_processes"] > 1:
                notes.append("multi-process: sampled RSS sums processes and double-counts shared pages; "
                             "use --pss for proportional accounting")
            if self.peak["sampled_bytes"] is not None and hwm and hwm > self.peak["sampled_bytes"] * 1.05:
                notes.append("kernel HWM exceeds the sampled peak: a short spike fell between samples")
            rep = Report(
                tool="mempeak", version=__version__, status=status,
                mode="in-process" if self.pid is None else "external",
                label=self.label, command=self.command,
                pid=self.pid or os.getpid(), host=os.uname().nodename if hasattr(os, "uname") else None,
                python=sys.version.split()[0], started_at=self.wall0,
                duration_s=round(now - self.t0, 4), interval_s=self.interval, samples=self.samples,
                peak=peak, baseline_bytes=self.baseline,
                final_bytes=next((row[1] for row in reversed(self.timeline) if row[1] is not None), None),
                exit=self.exit_info, cgroup=cg,
                phases=[dict(ph) for ph in self.phases.rows()],
                timeline_columns=["t_s", "rss_bytes", "cgroup_bytes", "phase"],
                timeline=list(self.timeline), notes=notes)
            return rep


# --------------------------------------------------------------------------- in-process API
_ACTIVE = None
_ACTIVE_LOCK = threading.Lock()
_EXT_PHASE = {"pid": None, "fd": None}


def active():
    """The running in-process monitor, or None."""
    return _ACTIVE


def start(interval=0.01, out=None, flush_every=1.0, pss=False, python_heap=False,
          exact_phases=False, label=None, print_summary=True):
    """Start sampling this process. Returns the monitor; call stop() for the Report.

    interval      sampling period in seconds (kernel HWM still catches spikes in between)
    out           JSON path; rewritten every flush_every seconds while running, so the
                  last known peak survives even if the process is OOM-killed
    pss           also sample PSS (Linux, slower)
    python_heap   also track the Python-heap peak via tracemalloc (slows allocation)
    exact_phases  exact per-phase peak RSS by resetting VmHWM at phase boundaries (Linux).
                  Side effect: other tools reading VmHWM / ru_maxrss see reset values.
    """
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is not None:
            raise RuntimeError("mempeak is already running in this process; call stop() first")
        mon = _Monitor(interval=interval, out=out, flush_every=flush_every, pss=pss,
                       python_heap=python_heap, exact_phases=exact_phases, label=label)
        mon.print_summary = print_summary
        _ACTIVE = mon.start()
    return mon


def stop(status="finished"):
    """Stop the in-process monitor and return its Report (None if not running)."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        mon, _ACTIVE = _ACTIVE, None
    if mon is None:
        return None
    rep = mon.finish(status)
    if getattr(mon, "print_summary", False):
        print(rep.summary(), file=sys.stderr)
    return rep


@atexit.register
def _stop_at_exit():
    if _ACTIVE is not None:
        stop("finished-at-exit")


@contextlib.contextmanager
def monitor(**kwargs):
    """with mempeak.monitor(out="peak.json") as m: ...;  m.report afterwards."""
    mon = start(**kwargs)
    status = "finished"
    try:
        yield mon
    except BaseException as exc:
        status = "error: " + type(exc).__name__
        raise
    finally:
        stop(status)


def profile(func=None, **kwargs):
    """Decorator: @mempeak.profile or @mempeak.profile(out="peak.json")."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if _ACTIVE is not None:      # nested/recursive: just run
                return fn(*a, **kw)
            with monitor(**kwargs):
                return fn(*a, **kw)
        return wrapper
    return deco(func) if func is not None else deco


def _ext_phase_write(kind, name):
    path = os.environ.get(_PHASE_ENV)
    if not path:
        return
    try:
        if _EXT_PHASE["pid"] != os.getpid():  # (re)open after fork
            _EXT_PHASE["fd"] = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            _EXT_PHASE["pid"] = os.getpid()
        line = "%.6f\t%d\t%d\t%s\t%s\n" % (time.monotonic(), os.getpid(), threading.get_ident(), kind,
                                          name.replace("\t", " ").replace("\n", " "))
        os.write(_EXT_PHASE["fd"], line.encode("utf-8"))
    except OSError:
        pass


class phase(contextlib.ContextDecorator):
    """Mark a stage: `with mempeak.phase("load"):` or `@mempeak.phase("load")`.

    A no-op costing a few microseconds when nothing is monitoring.
    """

    def __init__(self, name):
        self.name = str(name)

    def __enter__(self):
        mon = _ACTIVE
        if mon is not None:
            mon.phase_event("enter", self.name)
        _ext_phase_write("enter", self.name)
        return self

    def __exit__(self, *exc):
        mon = _ACTIVE
        if mon is not None:
            mon.phase_event("exit", self.name)
        _ext_phase_write("exit", self.name)
        return False


# --------------------------------------------------------------------------- external runner
def run_command(cmd, interval=0.02, out=None, flush_every=1.0, pss=False, label=None,
                kill_above=None, quiet=False):
    """Run cmd as a child, monitor its whole process tree, return (Report, exit_code)."""
    if not cmd:
        raise ValueError("no command given")
    fd, phase_file = tempfile.mkstemp(prefix="mempeak-phases-", suffix=".tsv")
    os.close(fd)
    env = dict(os.environ)
    env[_PHASE_ENV] = phase_file  # read by mempeak.phase() if the target happens to use it
    try:
        proc = subprocess.Popen(cmd, env=env)
    except OSError:
        os.unlink(phase_file)
        raise
    mon = _Monitor(interval=interval, out=out, flush_every=flush_every, pss=pss, pid=proc.pid,
                   tree=True, label=label, command=list(cmd), quiet=quiet)
    mon._phase_file = phase_file
    killed = {"by_us": False}
    if kill_above:
        def guard(value, _t):
            if value is not None and value > kill_above and not killed["by_us"]:
                killed["by_us"] = True
                mon.notes.append("sampled RSS %s exceeded --kill-above %s; child killed"
                                 % (_fmt(value), _fmt(kill_above)))
                for p in reversed(_process_tree(proc.pid)):
                    with contextlib.suppress(OSError):
                        os.kill(p, signal.SIGKILL)
        mon.on_sample = guard
    mon.start()
    forwarded = {}

    def forward(signum, _frame):  # keep running so the report is still written
        forwarded["sig"] = signum
        if signum != signal.SIGINT:  # the terminal already delivers Ctrl-C to the child
            with contextlib.suppress(OSError):
                proc.send_signal(signum)
    old = {}
    for s in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            old[s] = signal.signal(s, forward)
    ru_maxrss = None
    try:
        if hasattr(os, "wait4"):
            while True:
                try:
                    _, status, ru = os.wait4(proc.pid, 0)
                    break
                except InterruptedError:
                    continue
            code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else \
                (-os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status))
            proc.returncode = code
            ru_maxrss = ru.ru_maxrss if sys.platform == "darwin" else ru.ru_maxrss * 1024
        else:
            code = proc.wait()
    finally:
        for s, h in old.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(s, h)
    sig = -code if code < 0 else None
    cg_after = mon.cg.read() or {}
    oom_delta = None
    if mon.cg_before and cg_after.get("oom_kills") is not None and mon.cg_before.get("oom_kills") is not None:
        oom_delta = cg_after["oom_kills"] - mon.cg_before["oom_kills"]
    likely_oom = sig == signal.SIGKILL and not killed["by_us"] and \
        (oom_delta is None or oom_delta > 0) and "sig" not in forwarded
    signame = None
    if sig:
        with contextlib.suppress(ValueError):
            signame = signal.Signals(sig).name
    mon.exit_info = {"code": code, "signal": sig, "signal_name": signame, "ru_maxrss_bytes": ru_maxrss,
                     "likely_oom": bool(likely_oom), "killed_by_mempeak": killed["by_us"]}
    if likely_oom and oom_delta is None:
        mon.notes.append("SIGKILL without readable cgroup oom_kill counter: OOM is likely but not confirmed "
                         "(check `dmesg | grep -i oom`)")
    elif sig == signal.SIGKILL and oom_delta == 0 and not killed["by_us"]:
        mon.notes.append("SIGKILL but the cgroup oom_kill counter did not change: a host-level OOM or an "
                         "external kill (check `dmesg | grep -i oom`)")
    status = "killed" if sig else "exited"
    try:
        rep = mon.finish(status)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(phase_file)
    return rep, code


# --------------------------------------------------------------------------- compare
def compare(before, after):
    """Compare two reports (paths or dicts). Returns (text, rows)."""
    a = load(before) if isinstance(before, str) else before
    b = load(after) if isinstance(after, str) else after
    rows = []

    def row(name, x, y):
        delta = None if x is None or y is None else y - x
        pct = None if delta is None or not x else 100.0 * delta / x
        rows.append({"metric": name, "before": x, "after": y, "delta": delta, "pct": pct})

    pa, pb = a["peak"], b["peak"]
    row("PEAK", pa.get("peak_bytes"), pb.get("peak_bytes"))
    row("kernel HWM", pa.get("hwm_bytes"), pb.get("hwm_bytes"))
    row("sampled RSS", pa.get("sampled_bytes"), pb.get("sampled_bytes"))
    for key, name in (("pss_bytes", "sampled PSS"), ("python_heap_bytes", "Python heap")):
        if pa.get(key) is not None or pb.get(key) is not None:
            row(name, pa.get(key), pb.get(key))
    if a.get("cgroup") or b.get("cgroup"):
        row("cgroup peak", (a.get("cgroup") or {}).get("sampled_peak_bytes"),
            (b.get("cgroup") or {}).get("sampled_peak_bytes"))
    pha = {p["name"]: p for p in a.get("phases") or []}
    phb = {p["name"]: p for p in b.get("phases") or []}
    for name in list(pha) + [n for n in phb if n not in pha]:
        row("phase " + name, (pha.get(name) or {}).get("peak_bytes"), (phb.get(name) or {}).get("peak_bytes"))
    lines = ["%-34s %12s %12s %12s %8s" % ("metric", "before", "after", "delta", "%")]
    for r in rows:
        lines.append("%-34s %12s %12s %12s %8s" % (
            r["metric"][:34], _fmt(r["before"]), _fmt(r["after"]), _fmt(r["delta"]),
            "n/a" if r["pct"] is None else "%+.1f%%" % r["pct"]))
    for rep, tag in ((a, "before"), (b, "after")):
        ex = rep.get("exit") or {}
        if ex.get("likely_oom") or ex.get("killed_by_mempeak") or str(rep.get("status")) == "running":
            lines.append("WARNING: %s run did not finish normally (status=%s, exit=%s); its peak is a lower bound"
                         % (tag, rep.get("status"), ex.get("signal_name") or ex.get("code")))
    return "\n".join(lines), rows


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m mempeak", description="Whole-run peak memory monitor.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a command and report its peak memory",
                       usage="python -m mempeak run [options] -- command [args...]")
    r.add_argument("--out", "-o", help="write the JSON report here (updated live while running)")
    r.add_argument("--interval", type=float, default=0.02, help="sampling period in seconds (default 0.02)")
    r.add_argument("--pss", action="store_true", help="also sample PSS (multi-process, Linux)")
    r.add_argument("--label", help="free-form tag stored in the report, e.g. a git commit")
    r.add_argument("--fail-above", type=parse_size, metavar="SIZE",
                   help="exit with code 3 if PEAK exceeds SIZE (e.g. 6G); for CI regression gates")
    r.add_argument("--kill-above", type=parse_size, metavar="SIZE",
                   help="kill the command once sampled RSS exceeds SIZE (simulate a memory limit "
                        "without risking the machine)")
    r.add_argument("--quiet", "-q", action="store_true", help="do not print the summary")
    r.add_argument("command", nargs=argparse.REMAINDER)
    c = sub.add_parser("compare", help="compare two JSON reports (before/after an optimisation)")
    c.add_argument("before")
    c.add_argument("after")
    c.add_argument("--fail-if-higher", type=float, metavar="PCT",
                   help="exit 3 if PEAK grew by more than PCT percent")
    s = sub.add_parser("show", help="print the summary of a saved report")
    s.add_argument("report")
    args = ap.parse_args(argv)

    if args.cmd == "show":
        print(load(args.report).summary())
        return 0
    if args.cmd == "compare":
        text, rows = compare(args.before, args.after)
        print(text)
        if args.fail_if_higher is not None and rows[0]["pct"] is not None and rows[0]["pct"] > args.fail_if_higher:
            return 3
        return 0
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        ap.error("run: missing command (python -m mempeak run -- python app.py)")
    rep, code = run_command(cmd, interval=args.interval, out=args.out, pss=args.pss, label=args.label,
                            kill_above=args.kill_above, quiet=args.quiet)
    if not args.quiet:
        print(rep.summary(), file=sys.stderr)
        if args.out:
            print("[mempeak] report: %s" % os.path.abspath(args.out), file=sys.stderr)
    if args.fail_above and rep.peak_bytes and rep.peak_bytes > args.fail_above:
        print("[mempeak] PEAK %s > --fail-above %s" % (_fmt(rep.peak_bytes), _fmt(args.fail_above)),
              file=sys.stderr)
        return 3
    if code < 0:
        return 128 - code
    return code


if __name__ == "__main__":
    sys.exit(main())
