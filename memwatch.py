"""memwatch —— 可直接接入正式代码的进程内存明细探针（单文件、零第三方依赖）。

设计目标
--------
1. 好用优先：`memwatch.mark()` 一行搞定 —— 自动命名、自动只看当前作用域、
   自动打控制台简报（含相对上次的增长）、自动落盘完整报告。
2. 允许零改动：`python -m memwatch run app.py`，业务代码一行不改。
3. 到点自动抓：`MEMWATCH_AT=app.py:123` 或 `MEMWATCH_AT=app.py:handler`。
4. 外部触发：  `kill -USR1 <pid>` 或 `touch /tmp/memwatch.trigger`。
5. 绝不因为测量把业务搞崩：默认 fail_silent，全部阶段包 try/except。
6. 绝不因为测量造成泄漏：只保留基本类型/字符串/数字，不持有被测对象。

实测成本（62 万对象 / 150 万条分配记录，CPython 3.9，macOS）
------------------------------------------------------------
| 档位            | 内容                                    | 单次耗时 |
| --------------- | --------------------------------------- | -------- |
| counters        | RSS/USS/峰值 + tracemalloc 总量          | 0.3 ms   |
| counters+对象数 | 上面 + len(gc.get_objects())            | 45 ms    |
| standard        | + 变量持有量 + LocalProxy 表             | 140 ms   |
| deep            | + 分配点统计/差分 + 类型普查 + 增长差分   | 0.3-7 s  |

`take_snapshot()` 是 0.5 s 的底价（按 trace 条数），`statistics()`/`compare_to()` 是 3-7 s 的
大头，`Snapshot.filter_traces()` 反而要 14 s（Python 层遍历 + 复制快照），所以 deep 档默认关闭、
按需触发，并在报告里记录 take_snapshot_ms / elapsed_ms 供你判断。
"""

from __future__ import annotations

import gc
import json
import os
import signal
import sys
import sysconfig
import threading
import time
import tracemalloc

__all__ = [
    "install", "mark", "mark_all", "snapshot", "node", "watch", "track", "untrack",
    "flush", "uninstall", "is_installed", "report", "diagnose", "DEFAULT",
]
__version__ = "1.1"

# --------------------------------------------------------------------------
# 默认配置
# --------------------------------------------------------------------------

DEFAULT = {
    "out_dir": None,          # None -> ./memwatch-reports 或 $MEMWATCH_OUT
    "enabled": True,          # False -> snapshot() 直接空转（生产可远程关掉）
    "profile": "standard",    # counters | standard | deep
    "top": 20,                # 报告里每节的条数
    "trace": True,            # install 时开启 tracemalloc（越早越好）
    "frames": 5,              # tracemalloc 栈深度：实测对成本几乎无影响
    "root_paths": None,       # 只把「自己项目」的模块/帧当根；None=自动推断
    "roots_scope": "all",     # all=全进程扫描；locals=只看当前调用栈的局部变量（mark 默认）
    "include_third_party": False,   # 是否把 site-packages 的模块也当根
    "include_threads": True,  # 是否采集所有线程的帧局部变量
    "include_asyncio": True,  # 是否沿 asyncio 任务收集协程帧
    "resolve_proxies": True,  # LocalProxy 解引用（关键）
    "skip_infra": True,       # 不把 module/function/frame 内部展开（避免一个根吃掉全世界）
    "max_objects": 500_000,   # 单次持有者扫描的对象上限
    "count_objects": True,    # len(gc.get_objects())：62 万对象要 40ms，热路径可关
    "max_seconds": 5.0,       # 协作式时间预算，超了就截断后续阶段
    "brief": None,            # None=mark 打简报、snapshot 不打；True/False 强制
    "brief_top": 5,           # 简报里列几个变量
    "preview": True,          # 持有者列表里带上类型/长度/小值预览
    "max_marks_per_label": 50,  # 同一个标签名最多抓几次（防止 mark() 被放进循环里爆盘）
    "auto_widen": True,       # 作用域内一个变量都收不到时，自动放宽路径过滤重试一次
    "write_report": True,     # 落盘 JSON + TXT
    "print_report": False,    # 是否同时打到 stdout
    "on_signal": True,        # SIGUSR1 -> 异步抓一次
    "watch_file": None,       # 该文件出现 -> 抓一次
    "at": None,               # 到点触发，见模块头
    "rss_threshold_mb": None, # 常驻看门狗：RSS 超过阈值 -> 抓一次（git 递增）
    "interval": None,         # 定时兜底快照（秒）
    "sink": None,             # callable(report: dict) -> None，用于上报到日志/监控
    "fail_silent": True,      # True: 异常吞掉并记进 report["errors"]
    "label_prefix": "",       # 报告文件名前缀
}

_STATE = {
    "installed": False,
    "config": None,
    "seq": 0,
    "prev_snapshot": None,
    "prev_label": None,
    "tracemalloc_owned": False,
    "tracemalloc_early": False,
    "lock": threading.RLock(),
    "tracked": {},            # name -> obj（显式登记的关注点）
    "stop": threading.Event(),
    "threads": [],
    "trigger": threading.Event(),
    "orig_signal": None,
    "orig_settrace": None,
    "orig_setprofile": None,
    "signal_installed": False,
}

# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

try:
    from werkzeug.local import LocalProxy as _WerkzeugLocalProxy

    _PROXY_TYPES = (_WerkzeugLocalProxy,)
except Exception:  # pragma: no cover - werkzeug 可选
    _PROXY_TYPES = ()


def _is_proxy(obj) -> bool:
    """识别 LocalProxy（含 flask 的 request/g/session、第三方仿制品）。

    只查类型，不碰实例属性——对任意对象 getattr 可能触发 property 或 __getattr__。
    """
    if _PROXY_TYPES and isinstance(obj, _PROXY_TYPES):
        return True
    if obj is None or isinstance(obj, (int, str, bytes, float, bool, tuple, list, dict, set)):
        return False
    try:
        return any("_get_current_object" in vars(klass) for klass in type(obj).__mro__)
    except Exception:
        return False


def _resolve_proxy(proxy):
    """解引用；未绑定上下文时返回 None（绝不抛异常）。"""
    try:
        target = proxy._get_current_object()
    except Exception:
        return None
    return None if target is proxy else target


def _proxy_state(obj) -> str:
    if not _is_proxy(obj):
        return ""
    return "bound" if _resolve_proxy(obj) is not None else "unbound"


def _sizeof(obj) -> int:
    try:
        return sys.getsizeof(obj)
    except Exception:
        return 0


def _typename(obj) -> str:
    t = type(obj)
    try:
        return f"{t.__module__}.{t.__qualname__}"
    except Exception:
        return getattr(t, "__name__", "?")


def _describe(obj, limit: int = 80) -> str:
    """安全 repr：只对「小」对象做，避免把大结构 repr 出来拖慢/撑爆日志。"""
    try:
        if isinstance(obj, (str, bytes, bytearray)) and len(obj) > limit:
            head = bytes(obj[:limit]) if isinstance(obj, (bytes, bytearray)) else obj[:limit]
            return f"{head!r}...(len={len(obj)})"
        text = repr(obj)
    except Exception as exc:
        return f"<repr failed: {type(exc).__name__}>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _safe_preview(obj, limit: int = 48) -> str:
    """给变量一个「一眼能认出来」的短描述。

    只对原子类型做 repr，且先切片再 repr；容器一律只给类型+长度，
    绝不 repr 大结构（reprim 一个 2 万条 dict 会先构造出几十 MB 字符串）。
    """
    t = type(obj)
    try:
        if obj is None or t in (bool, int, float, complex):
            return repr(obj)[:limit]
        if t is str:
            return repr(obj[:limit] if len(obj) > limit else obj) + (f"...(len={len(obj)})"
                                                                     if len(obj) > limit else "")
        if t in (bytes, bytearray):
            return repr(bytes(obj[:limit])) + (f"...(len={len(obj)})" if len(obj) > limit else "")
        if t in (list, tuple, set, frozenset, dict, str):
            return f"{t.__name__}(len={len(obj)})"
        if hasattr(t, "__len__"):
            return f"{t.__name__}(len={len(obj)})"
    except Exception:
        pass
    return t.__name__


def _size_hint(obj):
    """容器长度：能便宜拿到就给，拿不到返回 None。"""
    try:
        if isinstance(obj, (list, tuple, set, frozenset, dict, str, bytes, bytearray)):
            return len(obj)
    except Exception:
        pass
    return None


def _caller_frame(depth: int = 1):
    """调用方的帧，自动跳过 memwatch 自己（mark 的自动命名/自动定界要用）。"""
    try:
        frame = sys._getframe(depth)
    except Exception:
        return None
    while frame is not None:
        try:
            if os.path.realpath(frame.f_code.co_filename) != _SELF_FILE:
                return frame
        except Exception:
            return frame
        frame = frame.f_back
    return None


def _frame_label(frame) -> str:
    """调用点标签（带行号，给人看）。"""
    if frame is None:
        return "unknown"
    code = frame.f_code
    return f"{os.path.basename(code.co_filename)}:{frame.f_lineno}:{code.co_name}"


def _frame_root(frame) -> str:
    """帧内变量的根标签（不带行号）。

    必须稳定：行号会随执行位置变化，带上它会让同一处 mark 的标签每次都不同，
    「相对上次涨了多少」就永远对不上。
    """
    if frame is None:
        return "unknown"
    code = frame.f_code
    return f"{os.path.basename(code.co_filename)}:{code.co_name}"


_INFRA_TYPES = (
    type(sys),               # module
    type,                    # class
    type(lambda: 0),         # function
    type((lambda: 0).__code__),
    type(sys._getframe()),   # frame
)

_SELF_FILE = os.path.realpath(__file__)


def _site_prefixes():
    paths = sysconfig.get_paths()
    out = []
    for key in ("purelib", "platlib", "stdlib"):
        value = paths.get(key)
        if value:
            out.append(os.path.realpath(value) + os.sep)
    return out


def _under(path: str, prefixes) -> bool:
    return any(path.startswith(p) for p in prefixes)


# --------------------------------------------------------------------------
# 根收集：模块全局变量 + 线程帧 + asyncio 任务 + 显式登记
# --------------------------------------------------------------------------


def _auto_root_paths():
    """推断「自己的代码」在哪些目录下。

    除了入口脚本目录 / argv[0] 目录 / cwd，还从**已加载模块**反推：
    凡是不在标准库和 site-packages 里的模块，其所在目录就是业务代码。
    这条最关键 —— 用 gunicorn、console_script、systemd 或从别处启动时，
    入口脚本和 cwd 都可能跟业务代码毫无关系，只看它们会一个根都收不到。
    """
    cands = []
    main = getattr(sys.modules.get("__main__"), "__file__", None)
    if main:
        cands.append(os.path.dirname(main))
    if sys.argv and sys.argv[0]:
        cands.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    cands.append(os.getcwd())
    libs = _site_prefixes()
    for name, mod in list(sys.modules.items()):
        if mod is None or name == "memwatch":
            continue
        if name.startswith("_") and name != "__main__":
            continue
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        folder = os.path.realpath(os.path.dirname(path))
        if _under_lib(folder, libs):
            continue
        cands.append(folder)
    out, seen = [], set()
    for cand in cands:
        if not cand:
            continue
        real = os.path.realpath(cand)
        if real in seen or _under_lib(real, libs):
            continue
        if real == os.path.dirname(real):      # 文件系统根（cwd=/ 时出现）没有意义
            continue
        seen.add(real)
        out.append(real)
    return out or [os.path.realpath(os.getcwd())]


def _is_virtual_filename(filename: str) -> bool:
    """`python -c` / REPL / exec / Jupyter 的帧文件名是 <string>、<stdin>、<ipython-input-3>。
    这类代码没有真实路径，按「自己的项目」处理，否则作用域会被过滤空。"""
    return filename.startswith("<") and filename.endswith(">")


def _lib_prefixes():
    """标准库 + site-packages 前缀（realpath 后）。用于「放宽模式」下排除库代码。"""
    return _site_prefixes()


def _under_lib(folder: str, libs) -> bool:
    """folder 是否位于库目录内（含「folder 就是库根目录本身」的情况）。

    少这个相等判断会把 stdlib 根目录当成业务目录（os.py 的 dirname 正好是它），
    于是整棵标准库都会被当成「自己的代码」。
    """
    if not folder:
        return False
    stripped = folder.rstrip(os.sep)
    for prefix in libs:
        if stripped == prefix.rstrip(os.sep) or folder.startswith(prefix):
            return True
    return False


def _frame_verdict(frame, root_paths, include_tp, widen=False, libs=None, diag=None):
    """判断这个帧要不要当根，返回 (是否收, 原因)。

    路径比较两端都必须 realpath：软链部署（/app -> /srv/releases/x）或 macOS 的
    /tmp -> /private/tmp 会让「原样比较」误判成非本项目代码，从而一个根都收不到。
    """
    filename = frame.f_code.co_filename
    if not filename:
        return False, "无文件名"
    if _is_virtual_filename(filename):
        return True, "虚拟文件名（-c/REPL/Jupyter），按本项目处理"
    try:
        real = os.path.realpath(filename)
    except Exception:
        return False, "realpath 失败"
    if real == _SELF_FILE:
        return False, "探针自己的帧"
    if include_tp:
        return True, "include_third_party=True"
    if widen:
        libs = libs if libs is not None else _lib_prefixes()
        if _under_lib(real, libs) or _under_lib(os.path.dirname(real), libs):
            return False, "库代码（放宽模式下仍排除标准库/site-packages）"
        return True, "放宽模式：非库代码即视为本项目"
    if any(real.startswith(p + os.sep) or os.path.dirname(real) == p for p in root_paths):
        return True, "命中 root_paths"
    if diag is not None:
        rej = diag.setdefault("rejected_frames", [])
        if real not in rej and len(rej) < 8:
            rej.append(real)
    return False, "不在 root_paths 里"


def _frame_is_first_party(frame, root_paths, include_tp, widen=False, libs=None, diag=None):
    return _frame_verdict(frame, root_paths, include_tp, widen, libs, diag)[0]


def _module_is_first_party(mod, root_paths):
    path = getattr(mod, "__file__", None)
    if not path:
        return getattr(mod, "__name__", "") == "__main__"   # -c / REPL 的 __main__
    path = os.path.realpath(path)
    if path == _SELF_FILE:          # 探针自己的全局变量不是业务内存
        return False
    return any(path.startswith(p + os.sep) or os.path.dirname(path) == p for p in root_paths)


def _roots_from_modules(cfg, diag=None):
    root_paths = cfg["root_paths"]
    include_tp = cfg["include_third_party"]
    out = []
    for name, mod in list(sys.modules.items()):
        if mod is None or name == "memwatch":
            continue
        # __main__ 是主脚本，必须收；其余下划线开头的内部模块跳过
        if name.startswith("_") and name != "__main__":
            continue
        if not include_tp and not _module_is_first_party(mod, root_paths):
            if diag is not None and getattr(mod, "__file__", None):
                rej = diag.setdefault("rejected_modules", [])
                if len(rej) < 8:
                    rej.append(os.path.realpath(mod.__file__))
            continue
        try:
            items = list(vars(mod).items())
        except Exception:
            continue
        for attr, value in items:
            if attr.startswith("__"):
                continue
            out.append((f"{name}.{attr}", value, "module"))
    return out


def _roots_from_frames(cfg, diag=None):
    out = []
    root_paths = cfg["root_paths"]
    include_tp = cfg["include_third_party"]
    widen = bool(cfg.get("_widen"))
    libs = cfg.get("_libs")
    if not cfg["include_threads"]:
        return out
    try:
        frames = list(sys._current_frames().items())
    except Exception:
        return out
    for tid, frame in frames:
        depth = 0
        while frame is not None and depth < 64:
            if _frame_is_first_party(frame, root_paths, include_tp, widen, libs, diag):
                where = f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno}:{frame.f_code.co_name}"
                try:
                    for var, value in list(frame.f_locals.items()):
                        if var.startswith("__"):     # __builtins__/__loader__ 之类噪音
                            continue
                        out.append((f"thread{tid}:{where}:{var}", value, "frame"))
                except Exception:
                    pass
            frame = frame.f_back
            depth += 1
    return out


def _roots_from_asyncio(cfg):
    if not cfg["include_asyncio"]:
        return []
    out = []
    try:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return []
        try:
            tasks = list(asyncio.all_tasks())       # 3.10+ 起不再接受 loop 参数
        except TypeError:
            tasks = list(asyncio.all_tasks(loop))
        for task in tasks:
            get_name = getattr(task, "get_name", None)   # 3.8+
            label = f"task:{get_name() if get_name else id(task)}"
            # 只放「具体变量」的根。裸 task 根默认不放：3.12+ 的 Task.tp_traverse 会走到
            # event loop，从而把其它任务的变量整片吃掉（实测 payload 被算到主任务头上）。
            # 只有当完全读不到协程帧时，才用裸 task 兜底。
            frame = getattr(task, "get_coro", lambda: None)()
            depth = 0
            read_frame = False
            while frame is not None and depth < 32:
                cr_frame = getattr(frame, "cr_frame", None) or getattr(frame, "gi_frame", None)
                if cr_frame is not None:
                    read_frame = True
                    where = f"{os.path.basename(cr_frame.f_code.co_filename)}:{cr_frame.f_lineno}"
                    try:
                        for var, value in list(cr_frame.f_locals.items()):
                            if var.startswith("__"):
                                continue
                            out.append((f"{label}:{where}:{var}", value, "asyncio"))
                    except Exception:
                        pass
                frame = getattr(frame, "cr_await", None) or getattr(frame, "gi_yieldfrom", None)
                depth += 1
            if not read_frame:
                out.append((label, task, "asyncio"))
    except Exception:
        pass
    return out


def _roots_from_stack(cfg, frame, diag=None):
    """只看当前调用栈的局部变量：mark() 的默认口径。

    又快又准：「这个节点的内存花在哪些变量上」通常就是指这里手头这些变量，
    而不是全进程所有模块的全局变量。栈底如果是主模块，会自然带上 __main__ 的全局变量。
    """
    out = []
    root_paths = cfg["root_paths"]
    include_tp = cfg["include_third_party"]
    widen = bool(cfg.get("_widen"))
    libs = cfg.get("_libs")
    cur = frame
    depth = 0
    while cur is not None and depth < 64:
        if _frame_is_first_party(cur, root_paths, include_tp, widen, libs, diag):
            where = _frame_root(cur)     # 不带行号，保证跨 mark 标签稳定
            try:
                for var, value in list(cur.f_locals.items()):
                    if var.startswith("__"):
                        continue
                    out.append((f"{where}:{var}", value, "frame"))
            except Exception:
                pass
        cur = cur.f_back
        depth += 1
    return out


def collect_roots(cfg, frame=None, diag=None):
    """返回 [(label, obj, kind)]，已按 id 去重。

    roots_scope="locals" 且给了 frame 时只看调用栈；否则全进程扫描。
    显式 track() 的标签优先：用户点名要看的对象不该被同名的模块根抢走标签。
    diag 会记录「有哪些帧被过滤掉了」，用于把空报告变成可操作的提示。
    """
    rows = []
    for label, obj in list(_STATE["tracked"].items()):
        rows.append((f"tracked:{label}", obj, "tracked"))
    scope = cfg.get("roots_scope", "all")
    if scope == "locals" and frame is not None:
        rows.extend(_roots_from_stack(cfg, frame, diag))
    else:
        rows.extend((label, obj, kind) for label, obj, kind in _roots_from_modules(cfg, diag))
        rows.extend(_roots_from_frames(cfg, diag))
        rows.extend(_roots_from_asyncio(cfg))
    dedup, seen = [], set()
    for label, obj, kind in rows:
        oid = id(obj)
        if oid in seen:
            continue
        seen.add(oid)
        dedup.append((label, obj, kind))
    return dedup


# --------------------------------------------------------------------------
# 采集阶段
# --------------------------------------------------------------------------


def _phase_counters(cfg):
    counters = {"objects_tracked": None, "rss_mb": None, "uss_mb": None, "traced_mb": None,
                "traced_peak_mb": None, "tracemalloc": tracemalloc.is_tracing()}
    try:
        counters["traced_mb"] = round(tracemalloc.get_traced_memory()[0] / 1048576, 2)
        counters["traced_peak_mb"] = round(tracemalloc.get_traced_memory()[1] / 1048576, 2)
    except Exception:
        pass
    try:
        import psutil

        info = psutil.Process().memory_info()
        counters["rss_mb"] = round(info.rss / 1048576, 2)
        uss = getattr(info, "uss", None)
        counters["uss_mb"] = round(uss / 1048576, 2) if uss else None
    except Exception:
        try:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            scale = 1024 if sys.platform == "darwin" else 1
            counters["rss_mb"] = round(peak * scale / 1048576, 2)
            counters["rss_is_peak"] = True
        except Exception:
            pass
    return counters


def _phase_census(cfg):
    """按类型普查。带类型缓存：实测 62 万对象 2.6 s -> 0.44 s。"""
    cache, counts, sizes = {}, {}, {}
    try:
        objects = gc.get_objects()
    except Exception:
        return []
    for obj in objects:
        t = type(obj)
        name = cache.get(t)
        if name is None:
            try:
                name = f"{t.__module__}.{t.__qualname__}"
            except Exception:
                name = getattr(t, "__name__", "?")
            cache[t] = name
        counts[name] = counts.get(name, 0) + 1
        sizes[name] = sizes.get(name, 0) + _sizeof(obj)
    rows = [(name, sizes[name], counts[name]) for name in sizes]
    rows.sort(key=lambda r: r[1], reverse=True)
    cache.clear()
    return rows[: cfg["top"]]


def _phase_holders(cfg, deadline, frame=None, diag=None):
    """变量维度的保留大小（贪心：每个对象只算给第一个到达它的根）。"""
    roots = collect_roots(cfg, frame=frame, diag=diag)
    want_preview = cfg.get("preview", True)
    claimed = set()
    rows = []
    truncated = False
    visited = 0
    max_objects = cfg["max_objects"]
    for label, root, kind in roots:
        if time.monotonic() > deadline:
            truncated = True
            break
        # 函数/类/模块这类根自身几乎不占内存（我们也不展开它们），列出来只是噪音
        if kind != "tracked" and isinstance(root, _INFRA_TYPES):
            continue
        stack = [root]
        total = 0
        count = 0
        while stack:
            obj = stack.pop()
            oid = id(obj)
            if oid in claimed:
                continue
            claimed.add(oid)
            visited += 1
            if visited > max_objects:
                truncated = True
                break
            total += _sizeof(obj)
            count += 1
            if cfg["skip_infra"] and isinstance(obj, _INFRA_TYPES):
                continue
            refs = _children(obj)
            if cfg["resolve_proxies"] and _is_proxy(obj):
                target = _resolve_proxy(obj)
                if target is not None:
                    refs = list(refs) + [target]  # 显式解引用边
            if refs:
                stack.extend(refs)
        if truncated and visited > max_objects:
            break
        if total:
            state = _proxy_state(root) if _is_proxy(root) else ""
            row = {
                "root": label, "kind": kind, "bytes": total, "objects": count,
                "is_proxy": bool(state), "proxy_state": state,
                "type": _typename(root),
            }
            if want_preview:
                row["len"] = _size_hint(root)
                row["preview"] = _safe_preview(root)
            rows.append(row)
    rows.sort(key=lambda r: r["bytes"], reverse=True)
    return rows[: cfg["top"]], visited, truncated


def _phase_proxies(cfg, deadline, frame=None, diag=None):
    """LocalProxy 专表：自身开销 + 解引用后大小 + 共享同一目标的 proxy 个数。

    共享必须单列：3 个 proxy 指向同一份 8 MiB 数据时，直接相加会得到 24 MiB 的错误结论。
    """
    roots = collect_roots(cfg, frame=frame, diag=diag)
    seen = set()
    found = []
    for label, root, _kind in roots:
        if time.monotonic() > deadline:
            break
        stack = [root]
        while stack:
            obj = stack.pop()
            oid = id(obj)
            if oid in seen:
                continue
            seen.add(oid)
            if _is_proxy(obj):
                found.append((label, obj))
                continue
            if cfg["skip_infra"] and isinstance(obj, _INFRA_TYPES):
                continue
            stack.extend(_children(obj))
    by_target = {}
    for label, proxy in found:
        target = _resolve_proxy(proxy)
        key = id(target) if target is not None else None
        entry = by_target.setdefault(key, {
            "target_type": _typename(target) if target is not None else None,
            "target_bytes": 0, "proxies": [], "own_bytes": 0, "state": "unbound",
        })
        entry["proxies"].append(label)
        entry["own_bytes"] += _sizeof(proxy)
        if target is not None:
            entry["state"] = "bound"
            if not entry["target_bytes"]:
                deep, _, _ = _deep_size(target, cfg, deadline)
                entry["target_bytes"] = deep
    out = []
    for entry in by_target.values():
        entry["proxy_count"] = len(entry["proxies"])
        entry["shared"] = entry["proxy_count"] > 1
        entry["shared_warning_mb"] = round(entry["target_bytes"] * (entry["proxy_count"] - 1) / 1048576, 2)
        out.append(entry)
    out.sort(key=lambda e: e["target_bytes"], reverse=True)
    return out[: cfg["top"]]


def _deep_size(root, cfg, deadline):
    claimed = set()
    stack = [root]
    total = 0
    count = 0
    truncated = False
    while stack:
        if count % 4096 == 0 and time.monotonic() > deadline:
            truncated = True
            break
        obj = stack.pop()
        oid = id(obj)
        if oid in claimed:
            continue
        claimed.add(oid)
        total += _sizeof(obj)
        count += 1
        if count > cfg["max_objects"]:
            truncated = True
            break
        if cfg["skip_infra"] and isinstance(obj, _INFRA_TYPES):
            continue
        refs = _children(obj)
        if cfg["resolve_proxies"] and _is_proxy(obj):
            target = _resolve_proxy(obj)
            if target is not None:
                refs = list(refs) + [target]
        if refs:
            stack.extend(refs)
    return total, count, truncated


def _children(obj):
    """遍历一个对象的子对象，并补上 gc 故意跳过的部分。

    **坑**：CPython 的 dict_traverse 会跳过 str 类型的键（str 不参与循环引用，gc 不访问它），
    所以 gc.get_referents({'a': x}) 只返回 x、不返回 'a'。而 `{字符串: 值}` 是现实代码里
    最常见的结构，不补回来就会系统性漏算字典键的内存。
    """
    try:
        refs = gc.get_referents(obj)
    except Exception:
        refs = ()
    if type(obj) is dict:
        try:
            keys = [k for k in obj if type(k) is str]
        except Exception:
            keys = []
        if keys:
            return list(refs) + keys
    return refs


def _is_self_trace(stat) -> bool:
    """该分配点是不是探针/测量机制自己产生的（报告里不该出现这些行号）。"""
    try:
        filename = os.path.realpath(stat.traceback[0].filename)
    except Exception:
        return False
    if filename == _SELF_FILE:
        return True
    # tracemalloc 自己的快照/统计代码
    return os.path.basename(filename) == "tracemalloc.py" and "lib" in filename


def _phase_sites(cfg, label=None):
    """分配点归因。贵：take_snapshot ~0.5 s，statistics/compare_to 3-7 s。

    必须在 holders/proxies 之前跑：否则探针自己扫描时创建的 set/list 会被算进业务分配点。
    """
    t0 = time.perf_counter()
    try:
        snap = tracemalloc.take_snapshot()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    take_ms = round((time.perf_counter() - t0) * 1000, 1)
    prev = _STATE.get("prev_snapshot")
    out = {
        "take_snapshot_ms": take_ms,
        "traces": len(snap.traces),
        "since": _STATE.get("prev_label"),
        "rows": [],
    }
    try:
        if prev is None:
            stats = snap.statistics("lineno")
            mode = "absolute"
        else:
            stats = snap.compare_to(prev, "lineno")
            mode = "diff"
        kept = 0
        for st in stats:
            if _is_self_trace(st):
                continue
            if mode == "absolute":
                out["rows"].append({"kb": round(st.size / 1024, 1), "count": st.count,
                                    "where": str(st.traceback)})
            else:
                if st.size_diff == 0 and st.count_diff == 0:
                    continue
                out["rows"].append({"kb_diff": round(st.size_diff / 1024, 1),
                                    "count_diff": st.count_diff, "where": str(st.traceback)})
            kept += 1
            if kept >= cfg["top"]:
                break
        out["mode"] = mode
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    _STATE["prev_snapshot"] = snap
    _STATE["prev_label"] = label
    return out


def _phase_growth(cfg, snapshot_ids_before):
    """跨检查点新增对象。id 差分有复用风险，但对「哪个类型在涨」足够。"""
    gc.collect()
    try:
        objects = gc.get_objects()
    except Exception:
        return []
    skip = {id(objects)}
    agg = {}
    for obj in objects:
        oid = id(obj)
        if oid in snapshot_ids_before or oid in skip:
            continue
        name = _typename(obj)
        slot = agg.setdefault(name, [0, 0, 0])
        slot[0] += 1
        slot[1] += _sizeof(obj)
        for ref in _children(obj):
            if not gc.is_tracked(ref):
                slot[2] += _sizeof(ref)  # 非 gc 追踪对象（str/bytes/bytearray/ndarray）
    rows = [{"type": k, "count": v[0], "own_bytes": v[1], "untracked_bytes": v[2]}
            for k, v in agg.items()]
    rows.sort(key=lambda r: r["own_bytes"] + r["untracked_bytes"], reverse=True)
    return rows[: cfg["top"]]


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def render_text(rep) -> str:
    cfg = rep["config"]
    top = cfg["top"]
    lines = []
    lines.append("=" * 78)
    lines.append(f"memwatch 检查点 #{rep['seq']}  {rep['label']}   profile={rep['profile']}")
    lines.append(f"时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(rep['ts']))}  "
                 f"进程 {rep['pid']}  耗时 {rep['elapsed_ms']} ms")
    lines.append("=" * 78)
    c = rep["counters"]
    lines.append(
        f"RSS {c.get('rss_mb')} MB   峰值RSS {c.get('peak_rss_mb', '-')} MB   "
        f"tracemalloc {c.get('traced_mb')} MB (峰值 {c.get('traced_peak_mb')})   "
        f"gc 对象 {c.get('objects_tracked')}"
    )
    if rep.get("warnings"):
        for w in rep["warnings"]:
            lines.append(f"!! {w}")
    if rep.get("sites"):
        s = rep["sites"]
        lines.append("")
        lines.append(f"-- 分配点（{'绝对' if s.get('mode') == 'absolute' else '相对 ' + str(s.get('since'))}，"
                     f"{s.get('traces')} 条 trace，take {s.get('take_snapshot_ms')} ms）")
        for r in s.get("rows", [])[:top]:
            if "kb" in r:
                lines.append(f"   {r['kb']:10.1f} KiB {r['count']:8d} 处  {r['where']}")
            else:
                lines.append(f"   {r['kb_diff']:+10.1f} KiB {r['count_diff']:+8d} 处  {r['where']}")
        if s.get("error"):
            lines.append(f"   !! {s['error']}")
    if rep.get("holders") is not None:
        lines.append("")
        lines.append(f"-- 变量维度持有量（贪心保留大小，扫描 {rep.get('holders_scanned')} 个对象"
                     + ("，已截断" if rep.get("holders_truncated") else "") + "）")
        for r in rep["holders"][:top]:
            flag = f"  <LocalProxy:{r['proxy_state']}>" if r["is_proxy"] else ""
            short = r["root"].split(":")[-1]
            extra = r.get("preview") or r.get("type") or ""
            detail = f"   {r['type']}" + (f" len={r['len']}" if r.get("len") is not None else "")
            if extra and extra != r.get("type"):
                detail += f"  {extra}"
            lines.append(f"   {r['bytes'] / 1024:10.1f} KiB {r['objects']:7d} obj  "
                         f"[{r['kind']}] {short}{flag}\n        {r['root']}\n        {detail.strip()}")
    if rep.get("proxies"):
        lines.append("")
        lines.append("-- LocalProxy 明细（共享目标不能相加）")
        for p in rep["proxies"][:top]:
            warn = f"  <== {p['proxy_count']} 个 proxy 共享，直接相加会多算 {p['shared_warning_mb']} MB" \
                if p["shared"] else ""
            lines.append(f"   自身 {p['own_bytes']:5d} B x{p['proxy_count']}  "
                         f"解引用后 {p['target_bytes'] / 1024:9.1f} KiB  "
                         f"{p['target_type']} [{p['state']}]{warn}")
            lines.append(f"      -> {', '.join(p['proxies'][:6])}")
    if rep.get("census"):
        lines.append("")
        lines.append("-- 类型普查")
        for name, size, count in rep["census"][:top]:
            lines.append(f"   {size / 1024:10.1f} KiB {count:8d} 个  {name}")
    if rep.get("growth"):
        lines.append("")
        lines.append("-- 自上次检查点新增对象")
        for g in rep["growth"][:top]:
            total = g["own_bytes"] + g["untracked_bytes"]
            lines.append(f"   {total / 1024:10.1f} KiB {g['own_bytes'] / 1024:9.1f} 自身 + "
                         f"{g['untracked_bytes'] / 1024:9.1f} 非追踪  {g['count']:8d} 个  {g['type']}")
    if rep.get("errors"):
        lines.append("")
        for e in rep["errors"]:
            lines.append(f"!! {e}")
    return "\n".join(lines)


def _explain_empty(cfg, diag, scope):
    """把「一个根都没收到」翻译成可操作的提示，而不是让用户猜。"""
    lines = []
    lines.append(f"roots_scope={scope}，root_paths={cfg.get('root_paths')}")
    rejected = diag.get("rejected_frames") or []
    if scope == "locals":
        if rejected:
            lines.append(f"调用栈里有 {len(rejected)} 个（去重后）帧因不在 root_paths 而被过滤，例如：")
            for path in rejected[:3]:
                lines.append(f"    - {path}")
            lines.append("修复（任选其一）：")
            lines.append("    memwatch.install(root_paths=['/你的/代码/目录'])   # 最准确")
            lines.append("    memwatch.mark(roots_scope='all')                  # 全进程扫描")
            lines.append("    memwatch.mark(include_third_party=True)           # 不过滤路径")
        else:
            lines.append("调用栈里没有可取到局部变量的帧：确认 mark() 是写在函数体内，"
                         "而不是模块顶层、类体或没有局部变量的地方")
    else:
        if diag.get("rejected_modules"):
            lines.append(f"所有业务模块都被 root_paths 过滤了，例如：{diag['rejected_modules'][:3]}")
            lines.append("修复：memwatch.install(root_paths=['/你的/代码/目录'])")
        else:
            lines.append("没有采集到任何根：业务变量可能只被 C 扩展/闭包/生成器持有，"
                         "用 memwatch.track('名字', obj) 显式登记")
    lines.append("排查：memwatch.diagnose() 会打印每个帧的判定过程")
    return lines


def diagnose(frame=None, write=True):
    """体检：告诉你为什么某些变量没被归因。

    返回 dict，并在 write=True 时把结论打到 stderr。
    """
    if not _STATE["installed"]:
        install()
    cfg = _STATE["config"]
    frame = frame if frame is not None else _caller_frame(1)
    root_paths = cfg["root_paths"]
    libs = _lib_prefixes()

    stack, cur, depth = [], frame, 0
    while cur is not None and depth < 32:
        ok, why = _frame_verdict(cur, root_paths, cfg["include_third_party"], False, libs, None)
        wide_ok, wide_why = _frame_verdict(cur, root_paths, cfg["include_third_party"], True, libs, None)
        locals_info = []
        try:
            for name, value in list(cur.f_locals.items()):
                if name.startswith("__"):
                    continue
                locals_info.append({"name": name, "type": _typename(value),
                                    "bytes": _sizeof(value)})
        except Exception:
            pass
        locals_info.sort(key=lambda r: r["bytes"], reverse=True)
        stack.append({
            "file": cur.f_code.co_filename,
            "realpath": os.path.realpath(cur.f_code.co_filename)
            if not _is_virtual_filename(cur.f_code.co_filename) else cur.f_code.co_filename,
            "func": cur.f_code.co_name, "line": cur.f_lineno,
            "accepted": ok, "reason": why,
            "accepted_when_widened": wide_ok, "widen_reason": wide_why,
            "locals": locals_info[:10], "locals_count": len(locals_info),
        })
        cur = cur.f_back
        depth += 1

    mods_ok = mods_rej = 0
    rejected_examples = []
    for name, mod in list(sys.modules.items()):
        if mod is None or name == "memwatch" or (name.startswith("_") and name != "__main__"):
            continue
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        if _module_is_first_party(mod, root_paths):
            mods_ok += 1
        else:
            mods_rej += 1
            if len(rejected_examples) < 3:
                rejected_examples.append(os.path.realpath(path))

    out = {
        "root_paths": root_paths,
        "roots_scope": cfg.get("roots_scope"),
        "include_third_party": cfg["include_third_party"],
        "installed": True,
        "tracemalloc": tracemalloc.is_tracing(),
        "stack": stack,
        "modules_first_party": mods_ok,
        "modules_rejected": mods_rej,
        "rejected_examples": rejected_examples,
        "accepted_frames": sum(1 for s in stack if s["accepted"]),
        "would_be_accepted_if_widened": sum(1 for s in stack if s["accepted_when_widened"]),
    }

    if write:
        lines = ["=" * 74, "memwatch 体检报告", "=" * 74]
        lines.append(f"root_paths          : {root_paths}")
        lines.append(f"roots_scope         : {cfg.get('roots_scope')}")
        lines.append(f"tracemalloc 已开启   : {tracemalloc.is_tracing()}")
        lines.append(f"被认定为业务模块     : {mods_ok} 个；被过滤 {mods_rej} 个"
                     + (f"，例如 {rejected_examples}" if rejected_examples else ""))
        lines.append(f"调用栈中可用帧       : {out['accepted_frames']} / {len(stack)}"
                     f"（放宽过滤后可用 {out['would_be_accepted_if_widened']} 个）")
        lines.append("")
        lines.append("调用栈逐帧判定（从 mark() 处向上）：")
        for i, s in enumerate(stack):
            mark = "收" if s["accepted"] else ("放宽后收" if s["accepted_when_widened"] else "弃")
            lines.append(f"  [{mark:5s}] {s['func']}() {os.path.basename(s['file'])}:{s['line']}  "
                         f"局部变量 {s['locals_count']} 个  <- {s['reason']}")
            if not s["accepted"] and s["accepted_when_widened"]:
                lines.append(f"           {s['realpath']}")
            for info in s["locals"][:3]:
                lines.append(f"           · {info['name']}: {info['type']} {info['bytes']} B")
        lines.append("")
        lines.append("修复建议：")
        if out["accepted_frames"] == 0 and out["would_be_accepted_if_widened"] > 0:
            lines.append("  你的业务代码不在 root_paths 里。加一行：")
            lines.append(f"      memwatch.install(root_paths=['{os.path.dirname(stack[0]['realpath']) if stack else '/your/app'}'])")
            lines.append("  或临时用：memwatch.mark(roots_scope='all') / mark(include_third_party=True)")
        elif out["accepted_frames"] == 0:
            lines.append("  调用栈里没有可取局部变量的帧：把 mark() 放进函数体，"
                         "或用 memwatch.track('名字', obj) 显式登记关注对象")
        else:
            lines.append("  帧的采集是正常的；如果某个变量没出现，通常是它只被模块级单例或别的线程持有，"
                         "改用 mark_all() 或 memwatch.track()")
        print("\n".join(lines), file=sys.stderr)
    return out


def _render_brief(rep, prev_holders=None, prev_rss=None) -> str:
    """控制台简报：一眼看出「这个节点内存花在哪、比上次涨了什么」。"""
    c = rep.get("counters") or {}
    rss = c.get("rss_mb")
    delta = ""
    if rss is not None and prev_rss:
        delta = f" ({rss - prev_rss:+.1f} MB)"
    head = (f"[memwatch] #{rep.get('seq')} 「{rep.get('label')}」"
            f"{(' ' + rep['callsite']) if rep.get('callsite') else ''}"
            f"   {rep.get('elapsed_ms')} ms   "
            f"RSS {rss} MB{delta}   traced {c.get('traced_mb')} MB")
    lines = [head]
    old = prev_holders or {}
    for h in (rep.get("holders") or [])[: rep.get("brief_top", 5)]:
        diff = ""
        if h["root"] in old:
            d = h["bytes"] - old[h["root"]]
            if abs(d) >= 65536:
                diff = f"{d / 1048576:+.1f}MB"
        parts = h["root"].split(":")
        name = f"{parts[-2]}.{parts[-1]}" if len(parts) >= 3 and h["kind"] == "frame" else parts[-1]
        extra = h.get("preview") or h.get("type") or ""
        lines.append(f"   {h['bytes'] / 1048576:8.2f} MB {diff:>10s}  {name:<28s} {extra}"
                     + ("  <LocalProxy>" if h.get("is_proxy") else ""))
    if not (rep.get("holders") or []):
        lines.append("   （没有可归因的变量）")
        for d in (rep.get("diagnostics") or []):
            lines.append(f"   > {d}")
    for e in (rep.get("errors") or []):       # 报错要先说，否则会被当成「没有内存」
        lines.append(f"   !! {e}")
    for w in (rep.get("warnings") or []):     # proxy 的共享/未绑定也在这批里，不重复打
        lines.append(f"   ! {w}")
    if rep.get("report_file"):
        lines.append(f"   完整报告: {os.path.join(_out_dir(_STATE['config']), rep['report_file'] + '.txt')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 核心
# --------------------------------------------------------------------------


def _snapshot_impl(label, profile, explicit=None, frame=None):
    cfg = dict(DEFAULT)
    cfg.update(_STATE.get("config") or {})
    unknown = []
    if explicit:
        unknown = [k for k in explicit if k not in DEFAULT]
        cfg.update({k: v for k, v in explicit.items() if k in DEFAULT})
    # 自动推导的 root_paths 每次快照重算：业务模块是逐步 import 进来的，
    # 只在 install() 时算一次会让后加载的模块全部被判成「不是自己项目」。
    if cfg.get("_root_paths_auto") and "root_paths" not in (explicit or {}):
        cfg["root_paths"] = _auto_root_paths()
    profile = profile or cfg["profile"]
    t0 = time.perf_counter()
    deadline = t0 + float(cfg["max_seconds"])
    rep = {
        "seq": _STATE["seq"], "label": label, "profile": profile, "pid": os.getpid(),
        "ts": time.time(), "config": {k: cfg[k] for k in ("top", "resolve_proxies",
                                                          "include_threads", "root_paths",
                                                          "roots_scope")},
        "counters": {}, "errors": [], "warnings": [],
    }
    if unknown:  # 拼错参数名时别静默忽略，否则会以为生效了
        rep["warnings"].append(f"忽略未知参数: {', '.join(sorted(unknown))}")
    if frame is not None:
        rep["callsite"] = _frame_label(frame)
    if profile not in ("counters", "standard", "deep"):
        rep["errors"].append(f"未知 profile={profile!r}，按 standard 处理")
        profile = "standard"
        rep["profile"] = profile
    try:
        rep["counters"] = _phase_counters(cfg)
        if cfg.get("count_objects", True):
            try:
                rep["counters"]["objects_tracked"] = len(gc.get_objects())
            except Exception:
                pass
    except Exception as exc:
        rep["errors"].append(f"counters: {type(exc).__name__}: {exc}")

    # 分配点必须最先取：晚于 holders 的话，探针扫描时自己创建的 set/list
    # 会被算成业务分配点，报告里全是 memwatch.py 的行号（实测 2 万处）。
    if profile == "deep":
        try:
            rep["sites"] = _phase_sites(cfg, label)
        except Exception as exc:
            rep["errors"].append(f"sites: {type(exc).__name__}: {exc}")

    if profile in ("standard", "deep"):
        diag = {}
        try:
            holders, scanned, truncated = _phase_holders(cfg, deadline, frame=frame, diag=diag)
            # 一个根都没收到 -> 自动放宽过滤重试一次。宁可给一份标注了「已放宽」的数据，
            # 也不要给用户一份空报告（实测最常见的失败是 root_paths 没覆盖业务代码目录）。
            if not holders and cfg.get("auto_widen", True):
                wide = dict(cfg)
                wide["_widen"] = True
                wide["_libs"] = _lib_prefixes()
                h2, s2, t2 = _phase_holders(wide, deadline, frame=frame, diag=diag)
                if h2:
                    holders, scanned, truncated = h2, s2, t2
                    rep["auto_widened"] = True
                    rep["warnings"].append(
                        "本次作用域没有命中「自己项目」的文件，已自动放宽过滤重试（结果里可能混入入口脚本变量）")
            rep["holders"] = holders
            rep["holders_scanned"] = scanned
            rep["holders_truncated"] = truncated
            rep["roots_scope"] = cfg.get("roots_scope")
            rep["root_paths"] = list(cfg.get("root_paths") or [])
            if truncated:
                rep["warnings"].append(
                    f"持有者扫描被预算截断（max_seconds={cfg['max_seconds']} 或 "
                    f"max_objects={cfg['max_objects']}），结果不完整")
            if not holders:
                rep["diagnostics"] = _explain_empty(cfg, diag, rep.get("roots_scope"))
        except Exception as exc:
            rep["errors"].append(f"holders: {type(exc).__name__}: {exc}")
        try:
            rep["proxies"] = _phase_proxies(cfg, deadline, frame=frame)
            shared = [p for p in rep["proxies"] if p.get("shared") and p.get("target_bytes")]
            if shared:
                rep["warnings"].append(
                    f"检测到 {len(shared)} 组 LocalProxy 共享同一目标，"
                    f"持有量不可直接相加（最大一组 {shared[0]['shared_warning_mb']} MB）")
            unbound = [p for p in rep["proxies"] if p["state"] == "unbound"]
            if unbound:
                rep["warnings"].append(
                    f"{len(unbound)} 个 LocalProxy 当前未绑定（_get_current_object 抛异常），已按 unbound 记录")
        except Exception as exc:
            rep["errors"].append(f"proxies: {type(exc).__name__}: {exc}")

    if profile == "deep":
        try:
            rep["census"] = _phase_census(cfg)
        except Exception as exc:
            rep["errors"].append(f"census: {type(exc).__name__}: {exc}")
        if _STATE.get("growth_base") is not None:
            try:
                rep["growth"] = _phase_growth(cfg, _STATE["growth_base"])
                _STATE["growth_base"] = {id(o) for o in gc.get_objects()}
            except Exception as exc:
                rep["errors"].append(f"growth: {type(exc).__name__}: {exc}")
        else:
            _STATE["growth_base"] = {id(o) for o in gc.get_objects()}
            rep["warnings"].append("首次 deep 快照，仅建立增长基线，无 growth 数据")

    rep["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if not (cfg.get("trace") and tracemalloc.is_tracing()):
        rep["warnings"].append("tracemalloc 未在启动早期开启，分配点数据不完整（用 python -m memwatch run 启动）")
    return rep


def _out_dir(cfg) -> str:
    return (cfg or {}).get("out_dir") or os.environ.get("MEMWATCH_OUT") or os.path.join(
        os.getcwd(), "memwatch-reports")


def _write_report(rep, cfg):
    out_dir = _out_dir(cfg)
    try:
        os.makedirs(out_dir, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(rep["label"]))[:60]
        base = f"{cfg.get('label_prefix', '')}{rep['pid']}_{rep['seq']:04d}_{safe}"
        with open(os.path.join(out_dir, base + ".json"), "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2, default=str)
        with open(os.path.join(out_dir, base + ".txt"), "w", encoding="utf-8") as fh:
            fh.write(render_text(rep))
        return base
    except Exception:
        return None


def _do_snapshot(label, profile=None, explicit=None, frame=None):
    if not _STATE["installed"]:
        install()
    cfg = _STATE["config"]
    if not cfg.get("enabled", True):
        return {"skipped": "disabled", "label": label}
    limit = cfg.get("max_marks_per_label") or 0
    if limit:
        counts = _STATE.setdefault("label_counts", {})
        if counts.get(label, 0) >= limit:
            once = _STATE.setdefault("limit_warned", set())
            if label not in once:
                once.add(label)
                print(f"[memwatch] 标签 {label!r} 已达上限 {limit} 次，后续同名 mark 被跳过"
                      f"（改 memwatch.install(max_marks_per_label=0) 可取消限制）", file=sys.stderr)
            return {"skipped": "label_limit", "label": label}
        counts[label] = counts.get(label, 0) + 1
    if not _STATE["lock"].acquire(blocking=False):
        return {"skipped": "busy", "label": label}  # 上一次还没跑完，绝不排队堆积
    try:
        _STATE["seq"] += 1
        rep = _snapshot_impl(label, profile, explicit, frame=frame)
        if cfg.get("write_report", True):
            rep["report_file"] = _write_report(rep, cfg)
        # 记录本次持有量，供下一次 mark 的「+ 增量」用
        if rep.get("holders"):
            _STATE["prev_holders"] = {h["root"]: h["bytes"] for h in rep["holders"]}
        sink = cfg.get("sink")
        if callable(sink):
            try:
                sink(rep)
            except Exception as exc:
                rep.setdefault("errors", []).append(f"sink: {type(exc).__name__}: {exc}")
        if cfg.get("print_report"):
            print(render_text(rep), file=sys.stderr)
        return rep
    except Exception as exc:
        if cfg.get("fail_silent", True):
            return {"label": label, "errors": [f"snapshot failed: {type(exc).__name__}: {exc}"]}
        raise
    finally:
        _STATE["lock"].release()


# --------------------------------------------------------------------------
# 公开 API
# --------------------------------------------------------------------------


def install(**kwargs):
    """安装探针。幂等，可在 import 期调用。返回当前有效配置。"""
    cfg = dict(DEFAULT)
    cfg.update({k: v for k, v in kwargs.items() if k in DEFAULT})
    for env_key, key, cast in (
        ("MEMWATCH_OUT", "out_dir", str), ("MEMWATCH_PROFILE", "profile", str),
        ("MEMWATCH_TOP", "top", int), ("MEMWATCH_AT", "at", str),
        ("MEMWATCH_WATCH_FILE", "watch_file", str), ("MEMWATCH_FRAMES", "frames", int),
        ("MEMWATCH_RSS_MB", "rss_threshold_mb", float), ("MEMWATCH_INTERVAL", "interval", float),
        # 零改动部署（PYTHONPATH 注入）时没法改代码，只能靠这个指定业务代码目录
        ("MEMWATCH_ROOT_PATHS", "root_paths", lambda v: [p for p in v.split(os.pathsep) if p]),
        ("MEMWATCH_RESOLVE_PROXY", "resolve_proxies", lambda v: v not in ("0", "false", "False")),
        ("MEMWATCH_ENABLED", "enabled", lambda v: v not in ("0", "false", "False")),
    ):
        raw = os.environ.get(env_key)
        if raw:
            try:
                cfg[key] = cast(raw)
            except Exception:
                pass
    if cfg["root_paths"] is None:
        cfg["root_paths"] = _auto_root_paths()
        cfg["_root_paths_auto"] = True     # 每次快照时重算：业务模块是逐步 import 进来的
    elif isinstance(cfg["root_paths"], str):
        cfg["root_paths"] = [cfg["root_paths"]]
    cfg["root_paths"] = [os.path.realpath(p) for p in cfg["root_paths"]]
    _STATE["config"] = cfg
    _STATE["installed"] = True

    if cfg["trace"] and not tracemalloc.is_tracing():
        try:
            tracemalloc.start(cfg["frames"])
            _STATE["tracemalloc_owned"] = True
            _STATE["tracemalloc_early"] = True   # 由 install 开启即视为「早」
        except Exception:
            pass
    if cfg["on_signal"]:
        _install_signal()
    if cfg["watch_file"]:
        _install_watch_file(cfg["watch_file"])
    if cfg["at"]:
        _install_at_trigger(cfg["at"])
    if cfg["rss_threshold_mb"] or cfg["interval"]:
        _install_watchdog(cfg)
    return cfg


def snapshot(label="checkpoint", profile=None, **overrides):
    """显式检查点：全进程扫描，不打简报。默认永不抛异常。"""
    return _do_snapshot(label, profile, overrides or None)


def mark(label=None, profile=None, **opts):
    """**主推接口**：在监控节点写一行 `memwatch.mark()` 就够了。

    比 snapshot() 少想三件事：
      1. 不用起名字 —— 自动用「文件:行号:函数名」当标签；
      2. 不用配作用域 —— 自动只看当前调用栈的局部变量（快且准，几十毫秒）；
      3. 不用去翻文件 —— 直接在控制台打一行简报，含「相对上次涨了多少」。

    可选：mark("支付流程")、mark(profile="deep")、mark(top=15)、mark(brief=False)。
    """
    frame = _caller_frame(1)
    if label is None:
        label = _frame_label(frame)
    opts.setdefault("roots_scope", "locals")
    if not _STATE["installed"]:
        install()
    prev_holders = dict(_STATE.get("prev_holders") or {})
    prev_rss = _STATE.get("prev_rss")
    rep = _do_snapshot(label, profile or "standard", opts, frame=frame)
    if isinstance(rep, dict) and rep.get("counters"):
        _STATE["prev_rss"] = rep["counters"].get("rss_mb")
    brief = opts.get("brief", _STATE["config"].get("brief"))
    if brief is None:
        brief = True
    if brief and isinstance(rep, dict) and not rep.get("skipped"):
        rep["brief_top"] = opts.get("brief_top", _STATE["config"].get("brief_top", 5))
        print(_render_brief(rep, prev_holders, prev_rss), file=sys.stderr)
    return rep


def mark_all(label=None, profile=None, **opts):
    """全进程扫描版 mark()：要把「所有模块全局变量 + 所有线程帧」都算进来时用。

    比 mark() 慢（全堆遍历），但不会漏掉不在当前调用栈上的持有者。
    """
    frame = _caller_frame(1)
    if label is None:
        label = f"ALL@{_frame_label(frame)}"
    opts.setdefault("roots_scope", "all")
    return mark(label, profile or "standard", **opts)


class node:
    """with memwatch.node("阶段名"): ...  —— 进入打一次，退出打一次并给出增量。"""

    def __init__(self, label, profile=None):
        self.label = label
        self.profile = profile
        self.entry = None

    def __enter__(self):
        self.entry = snapshot(f"{self.label}:enter", self.profile)
        return self.entry

    def __exit__(self, exc_type, exc, tb):
        rep = snapshot(f"{self.label}:exit", self.profile)
        if isinstance(self.entry, dict) and isinstance(rep, dict):
            try:
                before = self.entry["counters"].get("traced_mb") or 0
                after = rep["counters"].get("traced_mb") or 0
                rep["node_delta_mb"] = round(after - before, 2)
                if _STATE["config"].get("write_report", True):
                    _write_report(rep, _STATE["config"])   # 补了字段要重写落盘文件
            except Exception:
                pass
        return False  # 不吞业务异常


def watch(label=None, profile=None):
    """装饰器：@memwatch.watch("函数名")"""
    def deco(func):
        name = label or f"{func.__module__}.{func.__qualname__}"

        def wrapper(*args, **kwargs):
            snapshot(f"{name}:enter", profile)
            try:
                return func(*args, **kwargs)
            finally:
                snapshot(f"{name}:exit", profile)
        wrapper.__name__ = getattr(func, "__name__", "wrapped")
        wrapper.__doc__ = getattr(func, "__doc__", None)
        wrapper.__wrapped__ = func
        return wrapper
    return deco


def track(name, obj):
    """显式登记关注对象（适用于拿不到帧局部变量的场景，如协程/全局缓存）。"""
    _STATE["tracked"][name] = obj
    return obj


def untrack(name):
    _STATE["tracked"].pop(name, None)


def flush():
    """释放内部持有的大对象（tracemalloc 快照、增长基线），避免测量自身占内存。"""
    _STATE["prev_snapshot"] = None
    _STATE["prev_label"] = None
    _STATE["growth_base"] = None
    _STATE["tracked"].clear()
    gc.collect()


def uninstall():
    """还原触发钩子；已产生的报告不受影响。"""
    _STATE["stop"].set()
    for th in list(_STATE["threads"]):
        try:
            th.join(timeout=0.2)
        except Exception:
            pass
    _STATE["threads"] = []
    if _STATE["signal_installed"] and _STATE["orig_signal"] is not None:
        try:
            signal.signal(signal.SIGUSR1, _STATE["orig_signal"])
        except Exception:
            pass
    if _STATE["orig_setprofile"] is not None or _STATE["orig_settrace"] is not None:
        try:
            sys.setprofile(_STATE["orig_setprofile"])
            sys.settrace(_STATE["orig_settrace"])
        except Exception:
            pass
    if _STATE["tracemalloc_owned"] and tracemalloc.is_tracing():
        try:
            tracemalloc.stop()
        except Exception:
            pass
    _STATE["installed"] = False
    flush()


def is_installed():
    return _STATE["installed"]


def report(path=None, seq=None):
    """读取落盘报告；不传参则返回最近一份。"""
    out_dir = path or _out_dir(_STATE["config"])
    if os.path.isfile(out_dir) and out_dir.endswith(".json"):
        with open(out_dir, encoding="utf-8") as fh:
            return json.load(fh)
    files = sorted(f for f in os.listdir(out_dir) if f.endswith(".json"))
    if not files:
        raise FileNotFoundError(f"{out_dir} 下没有报告")
    target = files[-1]
    if seq is not None:
        target = next((f for f in files if f.split("_")[1] == f"{int(seq):04d}"), target)
    with open(os.path.join(out_dir, target), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# 触发方式
# --------------------------------------------------------------------------


def _install_signal():
    if _STATE["signal_installed"] or not hasattr(signal, "SIGUSR1"):
        return
    if threading.current_thread() is not threading.main_thread():
        return  # 信号只能装在主线程

    def handler(_signum, _frame):
        # 信号处理器里只置位，绝不做事：可能打断 malloc/GC，重入会死锁
        _STATE["trigger"].set()

    try:
        _STATE["orig_signal"] = signal.getsignal(signal.SIGUSR1)
        signal.signal(signal.SIGUSR1, handler)
        _STATE["signal_installed"] = True
        _start_worker("memwatch-trigger", _trigger_worker)
    except Exception:
        pass


def _trigger_worker():
    while not _STATE["stop"].is_set():
        if _STATE["trigger"].wait(0.2):
            _STATE["trigger"].clear()
            _do_snapshot("signal")
            flush()


def _install_watch_file(path):
    def worker():
        while not _STATE["stop"].is_set():
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
                _do_snapshot(f"file:{os.path.basename(path)}")
                flush()
            _STATE["stop"].wait(0.5)

    _start_worker("memwatch-watchfile", worker)


def _parse_at(spec):
    """'pkg/mod.py:123' -> 行触发；'pkg/mod.py:func' 或 'func' -> 调用触发。"""
    if ":" in spec:
        path, _, target = spec.rpartition(":")
    else:
        path, target = "", spec
    filename = os.path.basename(path) if path else None
    if target.isdigit():
        return {"kind": "line", "filename": filename, "path": path, "lineno": int(target)}
    return {"kind": "call", "filename": filename, "path": path, "name": target}


def _install_at_trigger(spec):
    """到点触发。

    调用级用 sys.setprofile（只在 call 事件回调，开销小）；
    行级用 sys.settrace（每行都回调，会拖慢 3-10 倍，仅建议短流程用）。
    命中后**同步**抓快照：目标帧还活在本线程栈上，locals 才是那一刻的真实状态。
    抓完立刻摘下钩子，把开销降到只有「命中前」。
    """
    target = _parse_at(spec)
    want_path = os.path.realpath(target["path"]) if target["path"] else None
    base = target["filename"]
    fired = {"done": False}

    def _matches(frame):
        code = frame.f_code
        if want_path:
            try:
                if os.path.realpath(code.co_filename) != want_path:
                    return False
            except Exception:
                return False
        elif base and not code.co_filename.endswith(base):
            return False
        if target["kind"] == "call":
            return code.co_name == target["name"]
        return frame.f_lineno == target["lineno"]

    def _restore():
        try:
            sys.setprofile(_STATE["orig_setprofile"])
        except Exception:
            pass
        try:
            sys.settrace(_STATE["orig_settrace"])
            if hasattr(threading, "settrace"):     # threading.gettrace/settrace 是 3.10+
                threading.settrace(_STATE.get("orig_thread_trace"))
        except Exception:
            pass

    def _fire(frame):
        fired["done"] = True           # 先置位：抓取过程中的 call/line 事件不再重入
        label = (f"at:{os.path.basename(frame.f_code.co_filename)}:"
                 f"{target.get('lineno') or target.get('name')}")
        _do_snapshot(label, "deep")    # 到点触发默认给最全的数据
        flush()
        _restore()

    if target["kind"] == "call":
        prev_profile = sys.getprofile()
        _STATE["orig_setprofile"] = prev_profile

        def profiler(frame, event, arg):
            if not fired["done"] and event == "call" and _matches(frame):
                _fire(frame)
            if prev_profile is not None:
                return prev_profile(frame, event, arg)
            return None

        sys.setprofile(profiler)
    else:
        prev_trace = sys.gettrace()
        prev_thread_trace = getattr(threading, "gettrace", lambda: None)()  # 3.10+ 才有
        _STATE["orig_settrace"] = prev_trace
        _STATE["orig_thread_trace"] = prev_thread_trace

        def tracer(frame, event, arg):
            if not fired["done"] and event == "line" and _matches(frame):
                _fire(frame)
            if prev_trace is not None:
                return prev_trace(frame, event, arg)
            return None if fired["done"] else tracer

        sys.settrace(tracer)
        if hasattr(threading, "settrace"):
            threading.settrace(tracer)


def _install_watchdog(cfg):
    state = {"last": 0.0, "fired": set()}

    def worker():
        while not _STATE["stop"].is_set():
            now = time.monotonic()
            if cfg["interval"] and now - state["last"] >= cfg["interval"]:
                state["last"] = now
                _do_snapshot("interval", "standard")
                flush()
            if cfg["rss_threshold_mb"]:
                try:
                    import psutil

                    rss = psutil.Process().memory_info().rss / 1048576
                    bucket = int(rss // cfg["rss_threshold_mb"])
                    if bucket > 0 and bucket not in state["fired"]:
                        state["fired"].add(bucket)
                        _do_snapshot(f"rss>{bucket * cfg['rss_threshold_mb']:.0f}MB", "deep")
                        flush()
                except Exception:
                    pass
            _STATE["stop"].wait(0.5)

    _start_worker("memwatch-watchdog", worker)


def _start_worker(name, fn):
    th = threading.Thread(target=fn, name=name, daemon=True)
    th.start()
    _STATE["threads"].append(th)
    return th


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cli_run(argv):
    """python -m memwatch run app.py [args...]  —— 业务代码零改动。"""
    if not argv:
        print("用法: python -m memwatch run <script.py> [args...]", file=sys.stderr)
        return 2
    script = os.path.abspath(argv[0])
    sys.argv = argv[:]
    sys.path.insert(0, os.path.dirname(script))
    install(root_paths=[os.path.dirname(script)])
    snapshot("boot")
    import runpy

    code = 0
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        snapshot("shutdown")
    return code


def _cli_report(argv):
    target = argv[0] if argv else None
    if target and target.endswith(".txt") and os.path.isfile(target):
        with open(target, encoding="utf-8") as fh:      # 已渲染好的文本直接给出
            print(fh.read())
        return 0
    print(render_text(report(target)))
    return 0


def _cli_list(argv):
    out_dir = argv[0] if argv else (os.environ.get("MEMWATCH_OUT") or
                                    os.path.join(os.getcwd(), "memwatch-reports"))
    if not os.path.isdir(out_dir):
        print(f"{out_dir} 不存在", file=sys.stderr)
        return 1
    for name in sorted(os.listdir(out_dir)):
        if name.endswith(".txt"):
            path = os.path.join(out_dir, name)
            print(f"{name}   {os.path.getsize(path):>9d} B")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        print("命令: run <script.py> [args...] | doctor | report [json|txt] | list [dir]")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "run":
        return _cli_run(rest)
    if cmd == "report":
        return _cli_report(rest)
    if cmd == "list":
        return _cli_list(rest)
    if cmd == "doctor":
        print("doctor 要在业务进程内跑（它要看你的调用栈）：在 mark() 旁边加一行 "
              "memwatch.diagnose()，或执行：", file=sys.stderr)
        print('  python -c "import memwatch; memwatch.install(); memwatch.diagnose()"', file=sys.stderr)
        return 2
    print(f"未知命令: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
