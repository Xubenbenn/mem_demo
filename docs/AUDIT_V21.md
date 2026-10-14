# memwatch v2.1 独立复核与修复记录

日期：2026-10-14。复核基线：`86659e4a69a8b28f2806959b9c592208a133574c`（main，v2.0）。本轮以用户授权的“分析、修改、验证、推送”为范围；不修改业务对象、不自动删除业务引用。

## 1. 结论与原始六项诉求

接手时 main 已有 v2 重构及 75 项测试，不能将这些已有功能当成本轮新成果。本轮是在其单一身份去重图上修复遗漏，并保持 schema_version=2、双文件部署和旧 memprobe 兼容入口。

| 诉求 | 基线已具备 | 本轮补强 / 仍有边界 |
| --- | --- | --- |
| import 全局对象归属 | 根身份组保留 aliases/references，不按首个根认定所有权 | 补充函数/类的真实数据引用；归属仍是可达关系而非分配者或 retained size |
| 代理遍历被预算截断 | 对象、引用边、传播、时间等预算及 coverage | 解析失败、不可用上下文、大小读取失败不再伪报完整；不以取消预算“解决”大图问题 |
| 多代理共享对象 | 相同目标合组，目标子图并集自动去重 | 保持自动记账修复；不擅自改业务生命周期或 del 引用 |
| sizeof 只有自身 | 递归身份图计算可达字节 | 修复容器中的函数闭包、默认参数、函数缓存、类数据及 NumPy 子类额外引用 |
| C/native 不可见 | NumPy owner/view、memoryview、mmap 专项识别；python/native/mapped 分列 | 不夸称任意 malloc/GPU 可枚举；计量失败明确报告，映射长度仍不是 RSS |
| mark_all 缺类型与分配点 | all_profile=deep；standard 也提供根图类型统计 | 分配点统计提前，避免总被后续图遍历挤掉；deep 的 GC census 仍受预算限制 |

## 2. 独立复现的新缺陷与修复

| 缺陷 | 基线反例 | 修复方式 |
| --- | --- | --- |
| 基础设施剪枝误伤业务引用 | 容器内闭包持有约 1 MB bytearray，仅报告 393 B 且 complete=true | 函数仅保留真实默认参数/闭包/属性等边，不跟随 globals/code/builtins；类仅保留存储数据引用，不执行 property |
| NumPy 专用遍历漏子类属性 | ndarray 子类额外持有约 1 MB，仅报告 248 B 且 complete=true | 补充 gc.get_referents 的子类引用，与 base/元素同图去重 |
| 异常被伪装为完整统计 | 代理 ValueError / sizeof 异常仍 graph.complete=true，exclusive 非 null | 传播 proxy_resolution_error / measurement_error 等原因至 graph、census、holders |
| 外部上下文隐性不完整 | 同一代理既有当前又有其他线程根，当前目标不代表其他上下文目标 | 保留每个引用上下文，保守标记 proxy_context_unavailable，不伪造跨上下文解析 |
| 元类/模块属性产生观察副作用 | 自定义元类或 ModuleType 子类的属性可能被类型/模块元数据读取执行 | 使用基础类型/模块描述符读取解释器字段；增加“不得执行属性”的回归 |
| preview=False 仍暴露键值 | 字典键被写入 proxy/buffer 引用路径 | 无预览时以 entry#n 代替值；不声称隐藏文件路径/变量名等全部元数据 |
| report 目录易被非报告 JSON 干扰 | [] 或非法 ts 导致 AttributeError/排序失败 | 跳过非字典、非有限数值时间戳及溢出，不阻碍读取有效报告 |
| fork 子进程继承不可用状态 | 父线程持有锁时 fork，子进程持续 busy；锁/worker/基线不属于新进程 | at-fork 子回调重置锁与会话状态，保留配置/track，延迟到显式采集重装；释放实际获取的旧锁，覆盖回调内 fork |
| 检查点有大额预算外开销 | counters 内部耗时约 1.6–1.8 ms，总调用却约 1.45–1.48 s | 安装时缓存自动 root_paths，检查点复用；显式 None 或重新 install 刷新 |

`gc.get_referents` 不是任意 C 对象的全引用反射。这些修复扩大已支持的业务引用覆盖，不构成全进程完整性证明。根图/GC census 也不是原子快照。

## 3. 测试证据

新增 `tests/test_hardening.py` 29 项，旧 75 项保持不变。覆盖闭包、类/函数属性、NumPy 子类 dict/slots 与共享元素、引用释放、零/有限预算、计量失败、外部代理上下文、元类属性副作用、隐私、报告读取、路径缓存、阶段顺序、基准入口，以及三种实际 POSIX fork 场景。

本地：CPython 3.13.5、NumPy 2.3.5；`MEMWATCH_AUTO=0 python test_modes.py`，104 tests / 101 passed / 3 skipped，退出码 0。3 个跳过项为缺 Werkzeug 的两个真实 LocalProxy 测试及被平台启动钩子遮蔽的 PYTHONPATH 注入测试；新增 29 项全部实际通过。最终一次全套耗时 38.602 秒，不是性能保证。

反向验证：将 10 项新测试在独立目录的原始 v2.0 上运行，得到 **9 failures + 1 error**；对应修复后通过。不是只添加永远为绿的测试。

CI 矩阵保留 Python 3.9 / 3.11 / 3.13，分别安装真实 Werkzeug / NumPy / psutil。每次运行保存 test-results.txt、runtime-versions.txt、tested-source.zip（保留 7 天）。测试和 tee 使用 pipefail，不能因为保存日志而掩盖失败。远端验收必须以相应提交的实际 Actions 结果为准；本地结果不冒充 CI 结果。

复现命令：

```bash
MEMWATCH_AUTO=0 python test_modes.py
MEMWATCH_AUTO=0 python -m unittest discover -s tests -p test_hardening.py -v
python benchmarks/checkpoint_overhead.py --objects 1000 --trials 2 --trace --output benchmark.json
```

## 4. 开销验证：必须测调用总耗时

环境：Linux x86_64、CPython 3.13.5。每样本新进程，1000 个不同 bytearray，每个 256 B；只跟踪该列表，不写报告、无 sink、count_objects=False、预算 5 秒。可选 psutil 在开启追踪前预热。仅两个重复样本，不是统计显著性研究，也不代表用户业务。v2.0 基线来自上述固定提交；用同一基准的 --module-dir 指向独立基线目录。

| 版本 | profile | trace | 样本 | 总调用 ms | 内部采集 ms | 根图完整 | census 完整 | sites |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2.0 | counters | True | 1 | 1484.586 | 1.621 | None | None | None |
| 2.0 | counters | True | 2 | 1451.252 | 1.849 | None | None | None |
| 2.1 | counters | True | 1 | 2.387 | 1.15 | None | None | None |
| 2.1 | counters | True | 2 | 2.335 | 1.054 | None | None | None |
| 2.1 | standard | True | 1 | 200.043 | 198.773 | True | True | None |
| 2.1 | standard | True | 2 | 206.9 | 205.617 | True | True | None |
| 2.1 | deep | True | 1 | 5010.796 | 5009.314 | True | False | complete |
| 2.1 | deep | True | 2 | 5010.599 | 5009.297 | True | False | complete |
| 2.1 | counters | False | 1 | 0.502 | 0.431 | None | None | None |
| 2.1 | counters | False | 2 | 0.643 | 0.568 | None | None | None |
| 2.1 | standard | False | 1 | 7.477 | 7.401 | True | True | None |
| 2.1 | standard | False | 2 | 7.772 | 7.692 | True | True | None |
| 2.1 | deep | False | 1 | 501.493 | 501.41 | True | True | unavailable |
| 2.1 | deep | False | 2 | 505.606 | 505.511 | True | True | unavailable |

v2.0 的路径推断成本发生在采集计时/预算外，导致 counters 的“内部毫秒级”隐藏了调用秒级开销。v2.1 缓存配置修复这一点。trace=True 时 deep 仍可能耗尽 5 秒且 census 不完整，不能将低侵入理解为无开销。追踪本身会影响业务分配；表格不包含首次安装、报告输出或 sink 成本。RSS 高水位变化也不等于探针独占的峰值开销。

## 5. 接入与边界

先在目标大对象分配前 `install(trace=True, on_signal=False)`，在业务节点 `mark_all()`；查看 `types`、`census_details`、`sites.status` 与 coverage。严格目录范围应显式 root_paths 且 auto_widen=False；重点对象使用 track + roots_scope="tracked"，结束后 untrack。跟踪默认强引用会延长寿命。

自动路径在安装时缓存。已有项目目录中新导入模块仍参与扫描；新目录需显式配置、install(root_paths=None)，或单次 mark_all(root_paths=None)。增大预算不保证全覆盖，反而可能增加内存峰值与停顿。

不会自动删除或改写业务引用。代理共享目标是正常语义；自动消除统计重复，不自动“修复”应用。任意私有分配器、GPU、第三方 C malloc、所有 ContextVar 历史以及支配树 retained size 均不在本工具保证内。mmap 单列虚拟长度；NumPy 逻辑 nbytes 不代表物理驻留页数。tracemalloc 仅见开启后注册的分配，某些扩展会主动注册，不是所有原生内存，也不能说永远看不到 NumPy。

fork 修复仅隔离探针状态，不保证业务原生库、自由线程解释器或绕开 Python 钩子的 fork 安全。预算仍不能强制打断扩展方法、gc.get_objects、代理 callable、tracemalloc 统计与 I/O。后台采集无法替代在真实请求上下文内采集 ContextVar 代理。

## 6. 原始技术依据

- CPython sys.getsizeof：https://docs.python.org/3.13/library/sys.html#sys.getsizeof
- CPython GC 覆盖与 tp_traverse 边界：https://docs.python.org/3.13/library/gc.html
- tracemalloc 开启时机及覆盖：https://docs.python.org/3.13/library/tracemalloc.html
- POSIX fork 回调：https://docs.python.org/3.13/library/os.html#os.register_at_fork
- NumPy nbytes：https://numpy.org/doc/stable/reference/generated/numpy.ndarray.nbytes.html
- NumPy 原生分配与追踪域：https://numpy.org/doc/stable/reference/c-api/data_memory.html
- Werkzeug LocalProxy 上下文与解引用：https://werkzeug.palletsprojects.com/en/stable/local/
