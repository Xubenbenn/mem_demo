"""memprobe —— 进程内内存归因探针（标准库实现，werkzeug 可选）。

目标：在程序执行到某个「检查点」时，回答两个问题
  1) 这块内存是「谁分配的」（分配点归因）-> tracemalloc
  2) 这块内存现在「被哪个变量持有」（持有者归因）-> 对象图 + 保留大小

注意：内存是「图」不是「树」。同一个 list 可以被 5 个变量同时引用，
不存在唯一的数学答案，只有三种可辩护的约定：
  - exclusive：谁先被遍历到算谁的（贪心，本次实现，顺序敏感但可复现）
  - shared   ：所有能到达它的根都记一份（会超算，但能暴露共享）
  - dominator：按支配树算真正「释放谁才能回收」-> guppy3/heapy 的做法

LocalProxy 是一个动态间接节点，不在此列：它本身只占几十字节，
真正的数据挂在 Local/LocalStack 的 __storage__ 里，
所以任何纯对象图遍历都不会把内存算到 proxy 那个变量头上。
本模块对此做了显式的「解引用边」处理，并在 report 里单列一节。
"""

from __future__ import annotations

import gc
import sys
import tracemalloc
from collections import Counter

# ---------------------------------------------------------------- 代理类型

try:  # werkzeug 可能不存在
    from werkzeug.local import LocalProxy as _LocalProxy

    PROXY_TYPES = (_LocalProxy,)
except Exception:  # pragma: no cover
    PROXY_TYPES = ()

# 这些类型只统计自身，不向下遍历：否则第一个根（比如某函数）会把整个世界算成自己的
INFRA_TYPES = (
    type(sys),  # module
    type,  # class
    type(lambda: 0),  # function
    type((lambda: 0).__code__),  # code
    type(sys._getframe()),  # frame
    type(sys._getframe().f_back),  # 同上，防御性
)


def shallow(obj) -> int:
    """对象自身占用（不含其引用到的对象）。"""
    try:
        return sys.getsizeof(obj)
    except TypeError:
        return 0


def is_proxy(obj) -> bool:
    return bool(PROXY_TYPES) and isinstance(obj, PROXY_TYPES)


def resolve_proxy(proxy):
    """安全解引用 LocalProxy：解不开（未绑定上下文）返回 None，绝不抛异常。"""
    try:
        target = proxy._get_current_object()
    except Exception:
        return None
    return None if target is proxy else target


# ---------------------------------------------------------------- 根收集


def roots_from_module(mod, include_private: bool = False):
    """模块级变量作为根（这就是「变量」最常见的形态）。"""
    out = []
    for name, value in list(vars(mod).items()):
        if name.startswith("__"):
            continue
        if not include_private and name.startswith("_"):
            continue
        out.append((f"{mod.__name__}.{name}", value))
    return out


def roots_from_frame(frame, prefix: str = ""):
    """某个栈帧的局部变量 + 闭包变量。"""
    where = f"{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}"
    return [(f"{prefix}{where}:{name}", value) for name, value in list(frame.f_locals.items())]


def roots_from_threads():
    """当前进程所有线程的栈帧局部变量。"""
    out = []
    for tid, frame in list(sys._current_frames().items()):
        out.extend(roots_from_frame(frame, prefix=f"thread{tid}:"))
    return out


def collect_roots(modules=None, include_threads: bool = True, include_private: bool = False):
    """默认：所有非标准库模块的全局变量 + 所有线程的帧局部变量。"""
    out = []
    if modules is None:
        modules = [
            m
            for name, m in list(sys.modules.items())
            if m is not None and not name.startswith(("_", "importlib", "encodings"))
        ]
    for mod in modules:
        try:
            out.extend(roots_from_module(mod, include_private=include_private))
        except Exception:
            continue
    if include_threads:
        out.extend(roots_from_threads())
    # 按 id 去重，保留第一个标签
    dedup, seen = [], set()
    for label, obj in out:
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        dedup.append((label, obj))
    return dedup


# ---------------------------------------------------------------- 遍历核心


def _referents(obj, resolve_proxies: bool):
    refs = gc.get_referents(obj)
    if resolve_proxies and is_proxy(obj):
        target = resolve_proxy(obj)
        if target is not None:
            refs = list(refs) + [target]  # 显式「解引用边」
    return refs


def retained_attribution(roots, resolve_proxies: bool = True, skip_infra: bool = True,
                         limit: int = 2_000_000):
    """贪心保留大小归因：每个对象只算给「第一个到达它的根」。

    返回 [(label, own_bytes, n_objects, is_proxy, proxy_state)]，按字节降序。
    """
    claimed = set()
    rows = []
    visited_total = 0
    for label, root in roots:
        stack = [root]
        total = 0
        count = 0
        while stack:
            obj = stack.pop()
            oid = id(obj)
            if oid in claimed:
                continue
            claimed.add(oid)
            visited_total += 1
            if visited_total > limit:
                raise RuntimeError("对象图过大，请缩小根集合或降低 limit")
            total += shallow(obj)
            count += 1
            if skip_infra and isinstance(obj, INFRA_TYPES):
                continue
            stack.extend(_referents(obj, resolve_proxies))
        if total:
            rows.append((label, total, count, is_proxy(root), _proxy_state(root)))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def _proxy_state(obj) -> str:
    if not is_proxy(obj):
        return ""
    return "bound" if resolve_proxy(obj) is not None else "unbound"


def find_proxies(roots):
    """找出根可达的所有 LocalProxy，并给出解析结果。"""
    seen = set()
    out = []
    for label, root in roots:
        stack = [root]
        while stack:
            obj = stack.pop()
            oid = id(obj)
            if oid in seen:
                continue
            seen.add(oid)
            if is_proxy(obj):
                out.append((label, obj))
                continue  # proxy 内部结构不再深入，交给 resolve 处理
            if isinstance(obj, INFRA_TYPES):
                continue
            stack.extend(gc.get_referents(obj))
    return out


def census(top: int = 25, min_bytes: int = 1) -> list:
    """整个进程的堆对象普查：按类型统计实例数与自身占用。"""
    counts, sizes = Counter(), Counter()
    for obj in gc.get_objects():
        t = type(obj)
        try:
            name = f"{t.__module__}.{t.__qualname__}"
        except Exception:
            name = repr(t)
        counts[name] += 1
        sizes[name] += shallow(obj)
    rows = [(name, sizes[name], counts[name]) for name in sizes if sizes[name] >= min_bytes]
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:top]


def describe(obj, max_len: int = 60) -> str:
    try:
        r = repr(obj)
    except Exception as exc:
        return f"<repr failed: {exc!r}>"
    return r if len(r) <= max_len else r[: max_len - 3] + "..."


def owner_chain(obj, max_depth: int = 5, ignore_files=("memprobe.py",)):
    """向上找「谁持有它」，优先给出 dict 的键名 / 帧里的变量名。

    ignore_files：跳过探针自身的帧和容器，否则你会看到自己的采集代码。
    """
    chain = []
    current = obj
    seen = {id(obj)}

    def _skip(ref):
        if id(ref) in seen:
            return True
        if isinstance(ref, type(sys._getframe())):  # frame
            return any(ref.f_code.co_filename.endswith(f) for f in ignore_files)
        return False

    for _ in range(max_depth):
        best = None
        for ref in gc.get_referrers(current):
            if _skip(ref):
                continue
            if isinstance(ref, dict):
                key = next((k for k, v in list(ref.items()) if v is current), None)
                if key is not None:
                    best = ("dict", ref, key)
                    break
            if isinstance(ref, (list, tuple, set)):
                best = ("seq", ref, None)
                break
        if best is None:
            for ref in gc.get_referrers(current):
                if _skip(ref):
                    continue
                best = ("other", ref, None)
                break
        if best is None:
            break
        kind, ref, key = best
        seen.add(id(ref))
        if kind == "dict":
            chain.append(f"[{key!r}]")
        elif kind == "seq":
            chain.append(f"<{type(ref).__name__}>")
        elif isinstance(ref, INFRA_TYPES) and type(ref) is type(sys._getframe()):
            chain.append(f"<frame {ref.f_code.co_filename}:{ref.f_lineno}:{ref.f_code.co_name}>")
        else:
            chain.append(f"<{type(ref).__name__} {describe(ref, 40)}>")
        current = ref
    return " <- ".join(chain)


# ---------------------------------------------------------------- 对象差分


class Growth:
    """跨检查点的「新增对象」差分：只存 id，不持有对象（否则探针自己就是泄漏源）。

    两个必须知道的坑：
      * str/bytes/bytearray/numpy.ndarray 不被 gc 追踪，永远不会出现在 gc.get_objects()
        里。所以本类额外统计「新对象直接引用到的非追踪对象」字节数（atoms 列），
        否则 400 KiB 的 bytearray 会只剩下列表本身的 1 KiB。
      * CPython 会复用 id：两次快照之间大量释放对象时可能误判。要求严格时用
        pympler.SummaryTracker 或 Probe.holders 的持有者视角。

    diff() 返回 [(类型名, 自身字节, 直接持有的非追踪对象字节, 个数)]。
    """

    def __init__(self):
        gc.collect()
        objs = gc.get_objects()
        self.base = set()
        skip = {id(objs), id(self.base), id(self)}
        self.base.update(id(o) for o in objs if id(o) not in skip)

    @staticmethod
    def _atom_bytes(obj) -> int:
        total = 0
        for ref in gc.get_referents(obj):
            if not gc.is_tracked(ref):
                total += shallow(ref)
        return total

    def diff(self, top: int = 15, advance: bool = False):
        gc.collect()
        objs = gc.get_objects()
        skip = {id(objs), id(self.base), id(self)}
        fresh = [o for o in objs if id(o) not in self.base and id(o) not in skip]
        agg = {}
        for obj in fresh:
            t = type(obj)
            key = f"{getattr(t, '__module__', '?')}.{getattr(t, '__qualname__', t.__name__)}"
            slot = agg.setdefault(key, [0, 0, 0])  # count, own, atoms
            slot[0] += 1
            slot[1] += shallow(obj)
            slot[2] += self._atom_bytes(obj)
        if advance:
            self.base.update(id(o) for o in objs)
        rows = [(k, v[1], v[2], v[0]) for k, v in agg.items()]
        rows.sort(key=lambda r: r[1] + r[2], reverse=True)
        return rows[:top]


# ---------------------------------------------------------------- 检查点


class Probe:
    """检查点式探针：在每个「节点」打一次快照，跑完出增量报告。"""

    def __init__(self, frames: int = 25, trace: bool = True):
        self.frames = frames
        self.trace = trace
        self.prev = None
        self.marks = []
        if trace and not tracemalloc.is_tracing():
            tracemalloc.start(frames)

    # -- 分配点归因 ------------------------------------------------------
    def snapshot(self):
        if not self.trace:
            return None
        gc.collect()
        return tracemalloc.take_snapshot()

    def diff(self, label: str, top: int = 15, group: str = "lineno"):
        snap = self.snapshot()
        if snap is None:
            return []
        base = self.prev
        self.prev = snap
        if base is None:
            stats = snap.statistics(group)
            head = f"[{label}] 初始快照（相对进程启动）"
        else:
            stats = snap.compare_to(base, group)
            head = f"[{label}] 相对上一个检查点的增量"
        lines = [head]
        for st in stats[:top]:
            if base is not None and st.size_diff == 0:
                continue
            where = str(st.traceback) if hasattr(st, "traceback") else ""
            if base is None:
                lines.append(f"  {st.size / 1024:10.1f} KiB  {st.count:8d} 处  {where}")
            else:
                lines.append(
                    f"  {st.size_diff / 1024:+10.1f} KiB  {st.count_diff:+8d} 处  {where}"
                )
        return lines

    # -- 持有者归因 ------------------------------------------------------
    def holders(self, label: str, roots, top: int = 15, resolve_proxies: bool = True,
                skip_infra: bool = True):
        rows = retained_attribution(roots, resolve_proxies=resolve_proxies, skip_infra=skip_infra)
        lines = [f"[{label}] 变量持有量（贪心保留大小，共 {len(rows)} 个非空根）"]
        for name, total, count, proxied, state in rows[:top]:
            flag = f"  <LocalProxy:{state}>" if proxied else ""
            lines.append(f"  {total / 1024:10.1f} KiB  {count:7d} obj  {name}{flag}")
        return lines

    def proxies(self, label: str, roots):
        rows = find_proxies(roots)
        lines = [f"[{label}] 发现的 LocalProxy：{len(rows)} 个"]
        if not rows:
            return lines
        agg = {}
        for owner, p in rows:
            key = (owner, type(p).__name__)
            agg.setdefault(key, [0, 0, 0])
            state = _proxy_state(p)
            tgt = resolve_proxy(p)
            deep = 0
            if tgt is not None:
                deep = sum(r[1] for r in retained_attribution([("t", tgt)]))
            agg[key][0] += 1
            agg[key][1] += shallow(p)
            agg[key][2] = max(agg[key][2], deep)
        for (owner, tname), (n, own, deep) in sorted(agg.items(), key=lambda kv: -kv[1][2]):
            lines.append(
                f"  自身 {own:5d} B x{n:<3d} 解引用后 {deep / 1024:9.1f} KiB  {owner}  ({tname})"
            )
        return lines

    def report(self, label: str, roots=None, top: int = 15):
        print("\n" + "=" * 78)
        print(f"### 检查点 {label}")
        print("=" * 78)
        for line in self.diff(label, top=top):
            print(line)
        if roots is not None:
            for line in self.holders(label, roots, top=top):
                print(line)
            for line in self.proxies(label, roots):
                print(line)
