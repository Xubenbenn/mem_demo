"""验证：LocalProxy 在各类内存分析手法下分别会发生什么。

跑法：python3 demo_run.py
"""

import gc
import sys
import tracemalloc

import psutil

import a
import b
import memprobe as mp


def hr(title):
    print("\n" + "-" * 78)
    print(title)
    print("-" * 78)


probe = mp.Probe()
probe.diff("基线（绑定前）")

print("### 0. 绑定前：proxy 未绑定")
hr("LocalProxy 只是一个几十字节的间接节点")
print("proxy 自身 sizeof          :", sys.getsizeof(b.payload_proxy))
print("proxy 的 slot 值           :", [type(r).__name__ for r in gc.get_referents(b.payload_proxy)])
print("此刻能解引用到真实数据吗   :", mp.resolve_proxy(b.payload_proxy))
try:
    getattr(b.payload_proxy, "__dict__")
    print("getattr(proxy,'__dict__')  : 成功")
except Exception as exc:
    print(f"getattr(proxy,'__dict__')  : {type(exc).__name__}: {exc}")

print("\n### 1. 进入业务节点：8 MiB 数据装进 b 的 Local，A 只留下 proxy")
a.bind(8)
print("真实 payload 对象 id       :", id(mp.resolve_proxy(b.payload_proxy)))
print("len(a.proxy)               :", len(a.proxy))
print("a.proxy is b.payload_proxy :", a.proxy is b.payload_proxy)
print("psutil RSS                 : %.1f MiB" % (psutil.Process().memory_info().rss / 1048576))

print("\n### 2. 陷阱 A：用 getattr 走对象图（pympler.asizeof、部分 objgraph 用法）")
proxy_dict = getattr(b.payload_proxy, "__dict__", None)
print("getattr(proxy,'__dict__') 就是真实对象的 __dict__:",
      proxy_dict is getattr(a.local_ref, "__dict__", None))
print("  -> proxy 被算成 payload 的持有者；数据实际挂在 Local.__storage__，")
print("     同一次遍历还会重复计数；未绑定时直接抛 RuntimeError。")

print("\n### 3. 陷阱 B：只用 gc.get_referents 走图（objgraph/guppy3 的底层）")
path, obj = [], b.payload_proxy
for _ in range(6):
    path.append(f"{type(obj).__name__}({sys.getsizeof(obj)}B)")
    nxt = gc.get_referents(obj)
    if not nxt:
        break
    obj = nxt[0]
print("proxy 出发的引用链         :", " -> ".join(path))
print("这条链能到 payload 吗      :", any(o is a.local_ref for o in gc.get_referents(b.payload_proxy)))
print("  -> werkzeug>=2.0 的 Local 用 ContextVar 存值，值不在 Python 引用图上，")
print("     所以「从变量出发」的遍历会把 8 MiB 判成不可达或归属到上下文内部。")

print("\n### 4. 陷阱 C：gc 看得见对象，但溯源不回变量")
print("payload 在 gc.get_objects() 里吗 :", any(o is a.local_ref for o in gc.get_objects()))
print("gc.get_referrers(payload) 类型   :", [type(r).__name__ for r in gc.get_referrers(a.local_ref)][:4])
print("payload 的 owner 链              :", mp.owner_chain(a.local_ref, max_depth=6))
print("  -> 链子走进 contextvars 内部结构，指不回 a.proxy / b.*，")
print("     所以按类型普查（census）能看见字节，按变量归因则必然错。")

print("\n### 5. 内存归因：解引用 vs 不解引用")
gc.collect()
print("tracemalloc 追踪到的堆     : %.1f MiB" % (tracemalloc.get_traced_memory()[0] / 1048576))
print("\n[按类型普查 top8，看不到 proxy 这一层]")
for name, size, cnt in mp.census(top=8):
    print(f"  {size / 1024:10.1f} KiB  {cnt:7d} 个  {name}")

roots = mp.collect_roots(modules=[a, b], include_threads=False)
for line in probe.holders("解引用=True（推荐）", roots, top=8, resolve_proxies=True):
    print(line)
for line in probe.proxies("解引用=True（推荐）", roots):
    print(line)
for line in probe.holders("解引用=False（纯对象图）", roots, top=8, resolve_proxies=False):
    print(line)

print("\n### 6. 退出业务节点：数据应当可回收")
del roots  # 关键：探针自己的根列表就是强引用，必须先删
before = tracemalloc.get_traced_memory()[0]
b.uninstall()
a.local_ref = None
gc.collect()
after = tracemalloc.get_traced_memory()[0]
print("释放                       : %.1f MiB" % ((before - after) / 1048576))
print("proxy 此刻状态             :", mp._proxy_state(b.payload_proxy))
print("  -> 探针持有的 roots / tracemalloc 快照 / 日志里的 repr")
print("     都会延长对象寿命，测量代码本身必须一起清理。")
