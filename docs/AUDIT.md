# mem_demo v2 审计、修复与验证记录

日期：2026-10-14。审计基线：`2f07533fd8be77db8386d7f7b64bb2d9844e8c1a`。目标：低入侵检查点接入，输出有明确统计口径和覆盖状态的内存明细。用户授权代码修改与推送。

## 1. 结论与设计

原实现把对象可达性、先遍历到的根、分配点和进程内存混为可相互解释的记账依据，尤其共享对象和代理会产生误导。本次替换为一个有预算的身份图，统一生成根可达量、别名、代理目标并集、类型与原生资源明细。

**核心不变量：** 每个已观察对象在物理对象总量/类型汇总中只出现一次；不同根行可以重叠，但不谎称独占；预算或错误不静默变成完整报告；映射大小不冒充 RSS；未识别的 C 分配不编造估计。

公开 API 与采集/输出放在 memwatch.py，根图/计量放在 _memwatch_core.py。两者同目录部署。旧 memprobe API 复用引擎，保留旧 tuple 形式时通过 warning 提示截断；Growth 改为类型净变化，不按可能复用的 id 判定新对象。

## 2. 用户指出的六类问题

| 问题 | 根因 | 实施的修正 | 仍有的边界 |
| --- | --- | --- | --- |
| import 链/模块全局变量归属 | 根按 id 丢弃别名；先到先得贪心分配 | 保存模块与帧绑定；身份组别名；图上传播可达关系，输出 shared 与去重 totals | 名称不是分配所有者；不计算完整支配树 |
| LocalProxy 遍历截断 | 不同阶段重复遍历，预算共享不清，目标截断状态丢失 | 同 wrapper 解析一次；复用同一图；对象/边/传播/根链接/时间预算；保留部分行和完整性 | 业务 callable、C 调用不能强制抢占 |
| 多代理共享目标 | 提示 shared 后仍需人工推算 | 同目标分组；不同目标交叉子图按身份取并集；wrapper/alias 分开统计 | 不自动删除业务引用 |
| getsizeof 只给自身 | 递归引用和浅尺寸口径不分 | 可达图遍历，字典键值补齐；只算代理壳；类型按唯一对象计量 | 未知扩展的 __sizeof__ 仍可能不完整 |
| census 漏 NumPy/mmap | 仅用 gc.get_objects；GC 不能枚举全部对象 | 可达非 GC 对象并入类型；NumPy owner/view、object dtype、memoryview、mmap 适配 | 原生覆盖明确 incomplete；映射另列，不能等同驻留页 |
| mark_all 默认缺少关键阶段 | API 强制 standard；全局 profile 被覆盖 | all_profile=deep；standard 也提供可达类型；deep 提供 census/sites 状态 | tracer 关闭或预算不足时报告 unavailable/skipped，不伪造分配点 |

## 3. 额外发现并修正

**计量与语义。** 旧版 proxy bound None 与 unbound 无法区分；未绑定代理可误归一组；getsizeof/preview 可能转发到业务对象或执行自定义方法。新版区分状态，默认避免 Python 自定义 sizeof/repr/len/__class__，原生描述符仍是协作调用。NumPy owner 的 sizeof 已含 payload 时不再次加 nbytes；mmap 用 len(mapping)，不把 file size 当映射大小。类型跨节点变化改为净计数/字节变化，并仅在相同范围的完整 census 上推进基线。

**触发与生命周期。** 原 RLock 允许 sink 重入采集；busy 前消耗配额；worker 的 stop/安装状态与钩子恢复存在风险；后台 flush 清除用户 track。新版非重入锁、busy 不记配额、有限标签容量、生命周期 generation 与独立停止事件；后台不清 track；不强制 GC；weak track 显式可选。已有信号、debugger/profiler 不被覆盖。协程装饰器围绕 await 的实际执行，而不是协程对象创建。

**输出与配置。** 单次 output/enabled/sink 配置过去未完全生效；写文件错误静默；最新报告依赖文件名字典序；node delta 在 sink 之后补写。新版使用有效单次配置、输出错误字段、原子单文件写入、session 防同 PID 文件冲突、timestamp/seq/pid 查询，node delta 在 sink/落盘之前生成。label_prefix 清洗防止路径越界。JSON+TXT 每个文件原子更新，但两文件不是事务。

**系统与接入。** RSS 降级单位和 USS API 修正；resource 值标记历史峰值。CLI 与提前加载的 memwatch 共用状态，shutdown 时保留目标 __main__ 全局。sitecustomize 改为显式/环境选择加入，保留首个其他启动钩子，禁用监控不禁用应用钩子。原 b.py 的 LocalStack 代理用法由 LocalProxy(stack, 'top') 改为 LocalProxy(stack)。

## 4. 验证证据与可复现命令

```bash
MEMWATCH_AUTO=0 python test_modes.py
MEMWATCH_AUTO=0 python -m unittest discover -s tests -v
```

本地环境 CPython 3.13.5、NumPy 2.3.5；psutil 可用，Werkzeug 未安装。最终拆分版本输出：

```text
Ran 75 tests in 33.256s
OK (skipped=3)
CHILD_EXIT 0
```

因此是 **72 passed / 3 skipped / 0 failed**，不是 75 全部实际执行通过。跳过的两个 Werkzeug 测试分别验证 ContextVar 和 Local/LocalStack/callable 真实后端；第三个测试检测到平台优先加载 /opt/python-hooks/sitecustomize.py，未执行仓库 PYTHONPATH 注入。文件/信号触发、显式预加载+CLI、启动钩子链式加载已独立执行验证。

回归范围包括：手工字节对照、根顺序反转、共享交叉子图、循环、别名、代理状态/禁用/上下文、原生缓冲区和视图、对象/边/传播/链接/根/census 预算、错误注入、配置与文件、弱引用回收、并发重入、worker 生命周期、asyncio、CLI/call/line/退出码、纯标准库降级、旧 API 兼容。

CI 工作流安装真实可选依赖，覆盖 Python 3.9（Werkzeug 2.0.3）、3.11/3.13（Werkzeug 3.x）。**配置存在不等于运行通过；CI 实际结果查对应固定提交的 Actions 记录。** 原 v1 characterization 测试/文档按原 blob 原样归档，没有宣称旧顺序敏感断言仍然适用。

## 5. 风险与后续边界

本次没有声称：全 C malloc/GPU 可归因、全上下文代理枚举、stop-the-world 一致性、支配树删除收益、硬实时预算、零观察者开销或低开销生产验证。full graph/census 本身可能较重；业务量级的暂停时间、峰值探针内存、定期运行成本需要在目标进程独立基准测试。

报告覆盖信息必须先于数值使用。特别是 GC 容器可能已经包含探针对象，选定 roots 之外的非 GC 对象仍会遗漏。root_paths 自动拓宽可纳入意料外路径；对严格范围使用 auto_widen=False。不同映射可能对应同一物理页，不能按映射字节解释 RSS。未知扩展 sizeof 值仍是扩展自身声明，不是原生分配器完整账本。

## 6. 原始技术依据

- Python sys.getsizeof：https://docs.python.org/3/library/sys.html#sys.getsizeof
- Python GC 对象范围：https://docs.python.org/3/library/gc.html#gc.get_objects
- Python tracemalloc：https://docs.python.org/3/library/tracemalloc.html
- NumPy ndarray.nbytes：https://numpy.org/doc/stable/reference/generated/numpy.ndarray.nbytes.html
- NumPy 内存管理及 tracemalloc 注册：https://numpy.org/doc/stable/reference/c-api/data_memory.html
- Werkzeug 上下文局部变量与代理：https://werkzeug.palletsprojects.com/en/stable/local/
- psutil 进程内存信息：https://psutil.readthedocs.io/en/latest/#psutil.Process.memory_full_info

报告中的设计选择和测试结论来自本仓库代码/实际运行；上述资料用于界定 API 能力，不能替代本项目验收证据。
