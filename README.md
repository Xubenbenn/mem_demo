# memwatch：检查点式运行时内存诊断

在指定业务节点观察：哪些变量可达大量对象、哪些类型占多少已识别内存、哪些 LocalProxy 共享目标，以及哪些分配点发生变化。

**v2 部署必须同时复制 `memwatch.py` 和 `_memwatch_core.py`，或使用整个仓库。** 运行核心无强制第三方依赖。`memprobe.py` 是旧 API 的兼容入口，复用同一统计引擎。

## 最小接入

```python
import memwatch

# 尽量在业务大对象分配前开启，分配点记录无法追溯开启之前的历史。
memwatch.install(trace=True, on_signal=False)
payload = [bytearray(1024 * 1024) for _ in range(8)]
rep = memwatch.mark_all("加载完成")
```

`mark()` 默认扫描当前调用栈，使用 `profile="standard"`，包含持有者、代理和可达对象类型统计；尊重 `install(profile=...)`。`mark_all()` 默认 `all_profile="deep"`，增加 GC 普查、分配点状态和类型净变化。两者均可显式传 `profile`。

定点缩小范围：

```python
memwatch.track("payload", payload)
try:
    rep = memwatch.mark_all("指定对象", roots_scope="tracked", max_seconds=10)
finally:
    memwatch.untrack("payload")
```

`track()` 默认强引用，会延长对象生命周期；可弱引用的对象可用 `track(name, obj, weak=True)`。不要把业务引用的自动删除交给监控器。

## 如何读报告

| 字段 | 口径 |
| --- | --- |
| `holders[].bytes` / `reachable_bytes` | 此根可达的已观察对象大小；各行不可相加，不代表分配者或删除变量后的释放量 |
| `holders[].aliases` / `references` | 同一对象的多个绑定及其作用域；主显示名称不是所有权判定 |
| `totals.accounted_bytes` | 选定根图按对象身份去重后的估计总量 = `python_bytes + native_bytes` |
| `totals.mapped_bytes` | 已识别映射的虚拟长度；独立列示，不等于 RSS，不加入上述总量 |
| `types` / `type_totals` | 选定根图内的去重类型明细及总量 |
| `census_details` / `census_meta` | standard 为可达图；deep 为 GC 对象与可达图的去重并集 |
| `proxy_totals.targets_union_bytes` | 已识别代理目标图的去重并集，包括不同目标之间共享的子对象 |
| `buffers` | NumPy / memoryview / mmap 的来源、逻辑长度、对象/缓冲区/映射字节 |
| `sites` | 已追踪分配点或 unavailable / skipped / error；不是所有原生分配 |
| `coverage` / `holders_truncated` | 预算、错误、作用域和完整性；不能把部分结果当作完整报告 |

`top` 只限制显示行数；总量单独计算，报告包含 omitted 字段。`exclusive_bytes` 只指选定根身份组内观察到的非共享部分，不是支配树 retained size；截断时为 `null`。

## 零改动启动与到点触发

```bash
MEMWATCH_AUTO=0 python -m memwatch run app.py
MEMWATCH_AT=/absolute/path/app.py:handle_request python -m memwatch run app.py
MEMWATCH_AT=/absolute/path/app.py:123 python -m memwatch run app.py
PYTHONPATH=/path/to/mem_demo MEMWATCH_AUTO=1 MEMWATCH_OUT=./reports python app.py
```

调用触发发生在函数入口；行触发发生在该行执行前。触发一次后撤销自己的钩子。不覆盖应用已有 debugger/profiler 或自定义 SIGUSR1 handler。第三方启动环境可能优先载入自己的 `sitecustomize`，此时应使用 CLI 或显式 `install()`，不能假定 PYTHONPATH 注入成功。

## 范围与代价

对象可达图、分配记录、进程 RSS 是三个不同视角，不构成可无条件相加的账本。NumPy 自有数据计一次，视图沿 base 回溯；memoryview 沿 exporter 回溯；mmap 只给映射长度。没有通用手段从任意 C malloc、GPU 内存或私有分配器反查全部 Python 持有者，本实现明确报告原生覆盖不完整。

预算是协作式的。大堆图仍可能带来明显内存/延时开销，`gc.get_objects()`、扩展方法、代理 callable、tracemalloc 统计和输出无法被该预算强制打断。生产热路径使用 counters 或缩小 roots，deep 用于选定诊断节点。其他线程/任务的 ContextVar 代理不能用当前上下文伪造解析结果；在实际请求上下文内 `mark()` 更可靠。

## 验证与资料

```bash
python -m pip install psutil werkzeug numpy   # 测试/演示用；不是运行核心的强制依赖
MEMWATCH_AUTO=0 python test_modes.py
# 等价：MEMWATCH_AUTO=0 python -m unittest discover -s tests -v
```

本次本地验证：CPython 3.13.5 / NumPy 2.3.5；75 tests，72 passed，3 skipped，退出码 0。跳过项是缺少 Werkzeug 的两个真实代理测试，以及被平台启动钩子遮蔽的 PYTHONPATH 注入测试；不是失败转为通过。CI 配置另覆盖 Python 3.9 / 3.11 / 3.13，结果以 GitHub Actions 实际运行记录为准。

详细配置见 [README_INTERFACE.md](README_INTERFACE.md)，问题、设计与验证证据见 [docs/AUDIT.md](docs/AUDIT.md)。v1 原说明和顺序敏感的测试逐字保存在 `docs/legacy/`，只作历史参考，不是 v2 契约。
