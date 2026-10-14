# Python 进程内内存归因方案（含 LocalProxy / A 依赖 b 的场景）

## 一句话结论

**有方案，但要先把问题拆成两条正交的线**，因为它们是不同的工具：

| 你想问的问题 | 归因方式 | 主力工具 |
| --- | --- | --- |
| 这 8 MiB 是**哪行代码分配**的 | 分配点归因（谁 malloc） | `tracemalloc` / memray / filprofiler / scalene |
| 这 8 MiB 现在**被哪个变量持有** | 持有者归因（对象图 + 保留大小） | objgraph / pympler / guppy3 / 本仓库 `memprobe.py` |

「每个变量占多少内存」**没有唯一答案**：内存是图不是树，同一个 list 可以被 5 个变量同时引用。
所以只有三种可辩护的记账约定：

- **exclusive / 贪心**：谁先被遍历到算谁的（`memprobe.retained_attribution` 用这个，顺序敏感但可复现）
- **shared**：所有能到达它的根各记一份（会超算，但能暴露共享 —— LocalProxy 场景必须看这个）
- **dominator**：按支配树算「释放谁才能真回收」（guppy3/heapy 的做法，最准也最慢）

---

## 1. 工具矩阵（实测环境：CPython 3.9 / werkzeug 2.0.3）

| 工具 | 回答什么 | 覆盖范围 | 开销 | 检查点用法 |
| --- | --- | --- | --- | --- |
| **tracemalloc**（标准库） | 分配点 + 调用栈，可做检查点差分 | 只覆盖 CPython 分配器；看不到纯 C 库的 `malloc` | 低（`frames=1` 可再降） | `PYTHONTRACEMALLOC=25` 或 `take_snapshot()` + `compare_to()` |
| **memray** | 分配点火焰图、原生 C 分配、**泄漏模式** | `malloc` 全量，含 numpy / C 扩展 | 中 | `memray run --leaks app.py`、`memray attach <pid>` |
| **filprofiler** | **峰值内存**的行级归因 | `malloc` 全量 | 高 | `fil-profile run app.py` |
| **scalene** | 行级 CPU / 内存 / 泄漏，采样式 | 全量 | 低 | `scalene --memory --leaks app.py` |
| **memory_profiler** | 逐行 RSS 增量 | 只有 RSS，分不清对象 | 高 | `@profile` / `mprof run` |
| **pympler** | `asizeof` 深尺寸、`SummaryTracker` 差分 | gc 图（漏 str/bytes/ndarray） | 中 | `SummaryTracker()` 打点 |
| **objgraph** | 类型统计、反向引用图、增长排名 | gc 图 | 低 | `show_growth()` / `show_backrefs()` |
| **guppy3 / heapy** | 支配树、按引用者分类 | gc 图 | 中 | `hpy().heap()` |
| **psutil** | 进程 RSS / USS 总量 | 全部（含 C 库） | 极低 | 定时线程打点，超阈值 dump |
| **`memprobe.py`**（本仓库） | 检查点：分配点差分 + **变量维度持有量** + LocalProxy 解引用 | 两者结合 | 中 | `Probe().report("节点名", roots)` |

---

## 2. 「执行到某个节点」怎么做：检查点 + 差分

最可靠的形式是**两两差分**，而不是看绝对快照。

```python
import memprobe as mp

probe = mp.Probe()                 # 内部 tracemalloc.start()

probe.diff("节点1：加载配置")        # 分配点基线
prepare()
probe.diff("节点2：加载配置后")      # 相对节点1 的分配增量（精确到行）

roots = mp.collect_roots(modules=[a, b])          # 模块全局变量 + 线程帧局部变量
for line in probe.holders("节点2：持有者", roots):  # 变量维度保留大小
    print(line)
```

再加一个跨检查点的「新增对象」差分（对应 objgraph/pympler 的 growth）：

```python
g = mp.Growth()
handle_request()                       # 你怀疑泄漏的那个节点
for name, own, atoms, cnt in g.diff(top=15):
    print(f"{own/1024:8.1f} KiB 自身 + {atoms/1024:9.1f} KiB 非追踪对象  {cnt:6d} 个  {name}")
```

> `atoms` 那一列是关键：`str` / `bytes` / `bytearray` / `numpy.ndarray` **不被 gc 追踪**，
> 永远不会出现在 `gc.get_objects()` 里。少了这列，400 KiB 的 `bytearray` 只会显示成列表自身的 1 KiB。

自动触发（不想改业务代码）：

```bash
PYTHONTRACEMALLOC=25 python app.py          # 从启动第一行就开始记录
```

```python
# 或者按 RSS 阈值 dump：常驻线程 + psutil
if psutil.Process().memory_info().rss > 800 * 1024 * 1024:
    snap = tracemalloc.take_snapshot()      # 再 group_by("traceback") 落盘
```

---

## 3. LocalProxy 专题：A 依赖 b 定义的 LocalProxy

`demo_run.py` 在真实 werkzeug `LocalProxy` + `Local` + `LocalStack` 上跑出的实测结论：

### 事实 0：proxy 本身只有 56 字节

```
proxy 自身 sizeof          : 56
proxy 的 slot 值           : ['Local', 'str', 'type']   # 真实数据不在里面
len(a.proxy)               : 8                          # 但用起来像真对象
```

### 陷阱 A：用 `getattr` 走对象图 → 解引用过头 + 未绑定就崩

`pympler.asizeof` 和部分 objgraph 用法是靠属性遍历的，而 `LocalProxy.__getattr__` 会把
**所有**属性转发给真实对象，包括 `__dict__`：

```
getattr(proxy,'__dict__') 就是真实对象的 __dict__: True      # 绑定后：双重计数
getattr(proxy,'__dict__')  : RuntimeError: no object bound to payload   # 未绑定时直接抛
```

### 陷阱 B：只用 `gc.get_referents` 走图 → **根本走不到数据**

```
proxy 出发的引用链         : LocalProxy(56B) -> Local(40B) -> ContextVar(80B) -> str(62B)
这条链能到 payload 吗      : False
```

werkzeug ≥ 2.0 的 `Local` 改用 `ContextVar` 存值（`object.__setattr__(self, "_storage", ContextVar(...))`），
值挂在**线程的 Context 内部 HAMT** 上，不在 Python 引用图里。于是「从变量出发」的遍历
会把 8 MiB 判成不可达，或者干脆不统计。

### 陷阱 C：`gc.get_objects()` 看得见对象，但溯源不回变量

```
payload 在 gc.get_objects() 里吗 : True
payload 的 owner 链              : ['payload'] <- <hamt_bitmap_node> <- <hamt> <- <Context> <- <cell>
```

**按类型普查能看见字节，按变量归因必然错。** 这条链走进 `contextvars` 内部结构，
永远指不回 `a.proxy` 或 `b.*`，`objgraph.find_backref_chain` / guppy3 的支配树都到不了根。

### 正确做法：把 proxy 当「显式解引用边」处理

```python
def _referents(obj, resolve_proxies=True):
    refs = gc.get_referents(obj)
    if resolve_proxies and is_proxy(obj):
        try:
            target = obj._get_current_object()      # 唯一可靠的解引用方式
        except Exception:                           # 未绑定上下文 -> RuntimeError
            target = None                           # 必须吞掉，否则探针自己会崩业务
        if target is not None and target is not obj:
            refs = list(refs) + [target]            # 加一条「解引用边」
    return refs
```

配套的三条纪律（`memprobe.py` 已实现）：

1. **全局 `visited` 按 `id()` 去重**，否则解引用边会让同一棵子树被重复计算 N 次。
2. **单列「proxy 共享」报告**：`probe.proxies()` 会告诉你 3 个 proxy 都解引用到同一份
   8192.6 KiB —— 不然你会以为 3 个变量各占 8 MiB。
3. **`_get_current_object()` 必须包 `try/except`**：request 上下文外解引用会抛
   `RuntimeError`，很多分析工具就是在这里把被测程序搞崩的。

实测对比（同一时刻、同一份 8 MiB 数据）：

```
[解引用=True（推荐）]                      [解引用=False（纯对象图）]
  8194.6 KiB  16 obj  a.proxy               8192.6 KiB  9 obj  a.local_ref   <- 只是碰巧
    1.0 KiB   1 obj  b.LocalStack             2.0 KiB   7 obj  a.proxy       <- 56 B 的壳
    0.4 KiB   6 obj  b.stack_proxy            1.0 KiB   1 obj  b.LocalStack
    ...
  发现的 LocalProxy：3 个
    自身 56 B x1   解引用后 8192.6 KiB  a.proxy
    自身 56 B x1   解引用后 8192.6 KiB  a.lazy
    自身 56 B x1   解引用后 8192.6 KiB  b.stack_proxy     <- 共享同一份，别重复相加
```

> 注意 `False` 那一列之所以还能看到 8192.6 KiB，是因为 demo 里 `a.local_ref` 恰好还留着一个直接引用。
> 真实项目里 A 通常**只有 proxy**，这时纯对象图归因会诚实地告诉你「一切正常」。

---

## 4. 三个把探针变成泄漏源的坑

1. **探针自己持有强引用**：`roots` 列表、`tracemalloc` 快照里保留的 frame 局部变量、日志里的
   `repr(obj)`。demo 第 6 步里 `del roots` 之前，8 MiB 死活不释放：
   ```
   释放 : 8.0 MiB     # del roots + 清空业务引用之后
   ```
2. **`sys.getsizeof` 只是自身**：`sys.getsizeof([b'x'*1024])` ≈ 64 B，不含元素。要深尺寸必须自己走图。
3. **`gc.get_objects()` 不含原子类型**：实测本轮遍历里 `str` / `bytearray` 实例数都是 0。
   按类型 census 会系统性少算；只能靠「容器可达 → `sys.getsizeof` 补算」的方式拿回来。

---

## 5. 什么时候必须上外部工具

- **numpy / PIL / mmap / C 扩展的大块内存**：`tracemalloc` 语义上就看不到纯 C 的 `malloc`，
  上 `memray`（含 `--leaks`）或 `filprofiler`（峰值行级归因）。
- **生产环境长期开启**：`Austin` / `py-spy` 采样式（低开销），或 `tracemalloc(frames=1)`
  只保留分配点不保留栈，再配 RSS 阈值 dump。
- **只要一个数**：`psutil` 的 RSS 定时打点，比任何对象级工具都便宜且不会骗你。

安装（本项目实测环境里这些都还没装）：

```bash
pip install pympler objgraph guppy3 memory_profiler
pip install memray filprofiler scalene    # 原生工具，需要编译
```

---

## 6. 本目录文件

| 文件 | 作用 |
| --- | --- |
| `memprobe.py` | 探针：分配点差分 / 变量持有量 / LocalProxy 解引用 / 类型普查 / 对象增长差分 / owner 链 |
| `b.py` | 被依赖模块，只暴露 `LocalProxy` + `Local` + `LocalStack` |
| `a.py` | 依赖方，只持有 proxy，不持有真实对象 |
| `demo_run.py` | 复现上面所有实测结论的脚本 |

```bash
python3 demo_run.py
```

实测输出见本文第 3、4 节引用的片段。
