# memwatch v2 接口与统计契约

## 1. 部署、兼容性

把 `memwatch.py` 与 `_memwatch_core.py` 放在同一可导入目录。`memprobe.py` 兼容旧入口，需要这两个文件。核心只依赖标准库，按 CPython 对象模型实现；不保证其他解释器或自由线程构建上的同等行为。

这是 schema_version=2 的统计语义升级，不是保持 v1 数值不变的补丁。原来的首根贪心记账改为可达关系和去重总量，不能用旧版根排名作精确验收。原测试保留在 `docs/legacy/test_modes_v1.py`，新测试验证 v2 契约。

## 2. 公开 API

```python
install(**config)                         # 安装/更新配置；重复同配置不重复启动 worker
mark(label=None, profile=None, **opts)    # 调用栈视角，默认控制台简报
mark_all(label=None, profile=None, **opts) # all scope，默认 deep
snapshot(label="checkpoint", profile=None, **opts) # 不主动打简报
with node("phase", profile="deep"):
    ...
@watch("function", profile="standard")
def function(): ...
track("cache", obj, weak=False)
untrack("cache")
flush(clear_tracked=True)
uninstall()
is_installed()
report(path=None, seq=None, pid=None)
diagnose(frame=None, write=True)
```

同步函数、协程函数和同步生成器的 watch 覆盖实际运行期间；异步生成器不支持自动包装，应在生成器内 mark。探针默认采集失败只报告错误，不吞业务异常。显式 `install()` 的无效配置会抛 `ValueError`，便于启动时发现问题。

`flush()` 清理探针基线，默认也清除显式 track（兼容旧调用）；传 `clear_tracked=False` 保留登记。后台信号/文件/定时触发不会自动 flush，不会清掉用户关注对象。没有强制 `gc.collect()`。

## 3. 主要配置

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| profile / all_profile | standard / deep | mark、snapshot 的默认档位 / mark_all 默认档位 |
| roots_scope | all | all、locals、tracked；mark 自动设 locals，mark_all 自动设 all |
| root_paths | 自动推断 | 首方模块/帧目录；可显式传字符串或列表 |
| include_third_party | false | 纳入库模块/帧作为根；可能显著增加开销 |
| include_threads / include_asyncio | true / true | all 模式补充线程帧 / 当前运行事件循环的任务帧 |
| resolve_proxies | true | 解析代理；可能执行业务 callable；不需要时关闭 |
| skip_infra | true | 不展开 module/function/class/frame 整个内部图 |
| max_objects / max_edges | 500000 / 2000000 | 图节点数 / 检查引用边数 |
| max_roots / max_proxy_groups | 4096 / 1024 | 根绑定采集上限 / 代理目标组上限 |
| max_propagations / max_root_links | 2000000 / 2000000 | 可达关系传播 / 展开到根明细的操作预算 |
| max_census_objects | 500000 | deep 中 GC 对象检查预算 |
| max_seconds | 5.0 | 协作式采集时间预算，不是强制超时 |
| top / brief_top | 20 / 5 | 每节显示行数 / 简报行数 |
| trace / frames | true / 5 | install 时配置 tracemalloc / 追踪栈深 |
| count_objects | true | 可调用 gc.get_objects 计数；counters 热路径建议关闭 |
| max_marks_per_label / max_labels | 50 / 1024 | 标签调用限额 / 不同标签容量；0 为取消对应限额 |
| allow_custom_sizeof | false | 默认不执行 Python 自定义 __sizeof__ |
| write_report / print_report | true / false | 原子写 JSON+TXT / 输出全文 |
| preview / brief | true / None | 安全小值预览 / mark 默认简报；禁用可减少数据暴露 |
| auto_widen | true | 无根时放宽到非库目录，并警告；严格范围设 false |
| on_signal | true | 安装可用的 SIGUSR1 触发；不替换已有业务 handler |
| watch_file / at / interval / rss_threshold_mb | None | 文件触发 / 到点触发 / 定时 / 当前 RSS 阶梯 |
| sink / fail_silent | None / true | 结果回调 / 采集错误默认转报告 |

预算值 0 是零预算；仅标签两个限额的 0 表示不限制。类型超过内部 2048 类归并到 `<other_types>`，并标记分组。引用路径最多 64 条/代理目标组，另有截断字段。

单次 snapshot/mark 参数覆盖测量、输出、sink、enabled 和限额配置，不改变全局配置。`trace/frames/on_signal/at/watch_file/interval/rss_threshold_mb` 属于 install 生命周期设置；在单次调用传入并不会重装钩子或追溯历史。调用点需要配置它们时先 install。

环境变量支持：`MEMWATCH_OUT`、`MEMWATCH_PROFILE`、`MEMWATCH_ALL_PROFILE`、`MEMWATCH_TOP`、`MEMWATCH_AT`、`MEMWATCH_WATCH_FILE`、`MEMWATCH_FRAMES`、`MEMWATCH_RSS_MB`、`MEMWATCH_INTERVAL`、`MEMWATCH_ROOT_PATHS`（os.pathsep 分隔）、`MEMWATCH_RESOLVE_PROXY`、`MEMWATCH_ENABLED`。显式 install 参数优先。

## 4. 去重、代理与类型

一次采集构造一个身份去重图。相同根对象保留多个名称；不同根的交叉可达关系不再按先到先得归属。`holders[].bytes` 是已观察可达字节，不能相加。总量看 `totals.accounted_bytes`；类型看 `types` / `census_details`。

代理解析同一 wrapper 一次；`proxy_count` 是不同 wrapper 数，`alias_count` 是保留路径数（截断时非总数）。相同目标合组，不同目标共享的子图在 `proxy_totals.targets_union_bytes` 再次去重。目标为 None 仍是 bound；unbound 代理不误归为同一目标。`target_truncated` 和 coverage 共同判断可信范围。

代理只在当前线程/任务上下文内解析。其他线程/挂起任务的根不会越过当前上下文解析边冒领 payload；同一代理在多个上下文可有不同目标，不提供跨上下文全量枚举。报告中的 resolution_context_available 是采集上下文信息，不是业务所有权证明。

## 5. 内存单位、原生缓冲区与覆盖

JSON `*_bytes` 为字节；界面 KiB/MiB 用 1024 进制，历史兼容 `*_mb` 实际也是 MiB。`python_bytes` 是 sizeof 口径剔除已识别 NumPy 自有数据后的部分，并不等同于 CPython 分配器账本。未知扩展对象的 C __sizeof__ 可能含自身原生分配，也可能完全不报告。

NumPy 自有 ndarray 的数据可能已经包含在 __sizeof__ 中，因此拆分 nbytes 而不是再次相加。视图沿 base、memoryview 沿 exporter 找共享资源。object dtype 数组还遍历其 Python 元素。未知 capsule/外部 owner 的内存不猜测。

mmap 使用当前映射 len 而不是整个文件 size；mapped_bytes 与 logical_bytes 不等于驻留内存，映射不加入 accounted_bytes。相同物理页的不同映射不作物理页去重。

GC 列表不能发现所有 Python 对象。deep census 是捕获到的 GC 对象与选定根图的去重并集，仍可能漏掉不可达于这些根的非 GC 对象、扩展内部资源。`complete_for_scope` 只说明该明确范围内未截断，不代表全进程完整。

tracemalloc 只覆盖开启后记录的分配；部分扩展会主动注册原生分配。它既不是全部 malloc，也不能简单说永远看不到 NumPy。RSS/USS/PSS 单独列示；没有 psutil 时的 resource fallback 明确标记历史峰值，不冒充实时 RSS。

## 6. 完整性、低入侵与限制

检查 `coverage.graph.complete`、`coverage.attribution.complete`、`census_meta`、`proxy_totals.truncated`、`sites.status` 和 `errors`。预算不足保留部分行，exclusive_bytes 置 null。phase completed 不表示覆盖完整，必须同时看 coverage。

已识别图内的计数去重不代表大小估计绝对准确。并发变化、被跳过的基础设施、不可检查的扩展所有者都可能影响覆盖。报告不是 stop-the-world 原子堆快照，普通 mark_all 不适合无测量地部署到每请求热路径。

预算不能中断运行中的 C 方法、gc.get_objects、get_referents、代理 callable、tracemalloc statistics、文件 I/O 和 sink。`elapsed_ms` 仅采集阶段；落盘/sink 的成本另计。对象图本身可能消耗大量内存。census 可包含探针自身存活对象；分配点剔除直接落在探针源码的记录，但不是完全无观察者效应。

不会自动修改业务对象或删除共享引用。自动修复的是监控记账，不是任意业务生命周期。已有 debugger/profiler 被保留；在冲突时禁用到点触发并报告。外部 tracer 启停不能被完全识别（特别是相同 frames 的重启），不应并发争夺 tracemalloc 所有权。

跨进程 fork / GPU 内存 / 所有线程的历史 ContextVar 值 / 支配树 retained size 不在本次实现保证内。显式 track 是强引用；报告只有标量/容器元数据，但采集期间临时根图必然会暂时持有目标。
