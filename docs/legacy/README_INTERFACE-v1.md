# memwatch 接入说明

面向「**允许在监控节点加调用代码，但要低入侵、要好用**」的场景。
主推一个接口：`memwatch.mark()` —— 一行，不用起名字、不用配作用域、不用去翻报告文件。

四种接入方式都验证过：`python test_modes.py` → **50 项全过**。

---

## 0. 依赖与 Python 版本

**目标环境：CPython 3.9**（已在 3.9.12 干净 venv 中 `pip install -r requirements.txt` 后跑通全部 50 项）。

| 场景 | 需要装什么 |
| --- | --- |
| **把 `memwatch.py` 复制进你的项目** | **什么都不用装**（单文件纯标准库，`psutil`/`werkzeug` 都是可选的，缺失时自动降级） |
| 跑本仓库的 demo 与测试 | `pip install -r requirements.txt` → `psutil` + `werkzeug` |
| 想要可复现的确切版本 | `pip install -r requirements-py39.lock.txt` |
| 要更深的 C 层/行级分析 | `pip install -r requirements-analyzers.txt`（可选，memwatch 不需要） |

```bash
# 在 3.9 环境里装（实测通过的组合）
python -m pip install -r requirements.txt
# 解析结果：psutil 7.2.2 + Werkzeug 3.1.9（MarkupSafe 3.0.4）→ 44/44 通过
```

可选依赖的降级路径（`test_modes.py` 的 L 组用例真的把 `psutil`/`werkzeug` import 全部屏蔽后验证过）：

| 可选包 | 缺失时的行为 |
| --- | --- |
| `psutil` | 改用标准库 `resource.getrusage()` 拿 RSS（拿不到 USS，精度略低，`counters.rss_is_peak` 会标出来） |
| `werkzeug` | 用鸭子类型识别 LocalProxy：任何在类里定义了 `_get_current_object` 的代理都能被解引用 |

Python 版本：**>= 3.7**。三个环境实测矩阵（`python test_modes.py`）：

| 解释器 | werkzeug | psutil | 结果 |
| --- | --- | --- | --- |
| **3.9.12**（目标环境，干净 venv 按 `requirements.txt` 装） | 3.1.9 | 7.2.2 | **50/50** |
| 3.9.12（conda 自带） | 2.0.3 | 5.8.0 | **50/50** |
| 3.14.2 | 3.1.9 | 7.2.2 | **50/50** |

跨版本适配点（都已处理并覆盖测试）：
`asyncio.all_tasks()` 在 3.10+ 改签名、3.12+ 的 `Task.tp_traverse` 会走到 event loop
（会让其它任务的变量被算到当前任务头上）、`threading.gettrace` 是 3.10+ 才有、
`werkzeug.__version__` 在 3.x 被移除（本项目不依赖它）。

---

## 1. 最快上手

```python
import memwatch                      # 文件头
...
def charge(order_id):
    items = build_items(order_id)
    memwatch.mark()                  # ← 就这一行
    return len(items)
```

跑起来，控制台直接出结论（stderr）：

```
[memwatch] #2 「缓存填充后」 app_mark.py:36:main   253.4 ms   RSS 35.72 MB (+3.7 MB)   traced 3.32 MB
       2.80 MB     +2.0MB  <module>.CACHE               dict(len=2)
       0.15 MB             work.items                   list(len=400)
   完整报告: ./memwatch-reports/71321_0002_缓存填充后.txt
```

它自动帮你做掉四件事：

1. **自动命名** —— 标签 = `文件:行号:函数名`，想改名就 `mark("支付流程")`；
2. **自动定界** —— 只看当前调用栈的局部变量（`roots_scope="locals"`），几十毫秒，不扫全堆；
3. **自动打简报** —— 一行头 + 最多 5 个变量，附带**相对上次 mark 的增长**；
4. **自动落盘** —— 完整报告（JSON + TXT）写到 `./memwatch-reports/`。

> 建议在可疑代码段的**首尾各放一个 mark**，第二个的 `(+x MB)` 就是这段代码的净增长。

想要全进程视角（所有模块全局变量 + 所有线程帧）时加一个参数：

```python
memwatch.mark("全进程视角", roots_scope="all")     # 等价于 memwatch.mark_all()
memwatch.mark("拿到分配点", profile="deep")         # 加分配点统计/类型普查/增长差分
```

---

## 2. 遇到「没有可归因的变量」怎么办

这是唯一需要你动手排查的提示，含义是「这个检查点一个根都没收到」。三步搞定。

### 第一步：跑体检

```python
memwatch.diagnose()      # 放在 mark() 旁边；或 python -c "import memwatch; memwatch.install(); memwatch.diagnose()"
```

它会逐帧打印判定过程和修复建议：

```
==========================================================================
memwatch 体检报告
==========================================================================
root_paths          : ['/srv/launcher/bin', '/']
被认定为业务模块     : 0 个；被过滤 156 个
调用栈中可用帧       : 1 / 3（放宽过滤后可用 3 个）

调用栈逐帧判定（从 mark() 处向上）：
  [收     ] do_work() biz.py:7  局部变量 2 个  <- 命中 root_paths
  [放宽后收] probe() biz.py:17  局部变量 1 个  <- 不在 root_paths 里
             /srv/app/biz.py
```

### 第二步：按成因对症下药

| 成因 | 现象 | 修法 |
| --- | --- | --- |
| **业务目录不在 `root_paths`** | 启动器在 `/usr/local/bin`、cwd 是 `/`（gunicorn / console_script / systemd / k8s 常见） | 现在会**从已加载模块反推**业务目录，多数情况已自动解决；仍不准就显式指定 |
| **软链部署** | `/app -> /srv/releases/xxx`，两侧 realpath 不一致 | 已修（路径比较两侧统一 realpath），无需处理 |
| **`mark()` 位置不对** | 写在模块顶层 / 类体 / 没有局部变量的地方 | 挪进函数体，或用 `memwatch.track("名字", obj)` 显式登记 |

显式指定 `root_paths` 的三种方式：

```python
memwatch.install(root_paths=["/srv/app", "/srv/lib"])     # 代码里，最准确
memwatch.mark(roots_scope="all")                          # 只这一处改成全进程扫描
memwatch.mark(include_third_party=True)                    # 这一处不做路径过滤
```

```bash
# 零改动部署（PYTHONPATH 注入）时改不了代码，用环境变量
MEMWATCH_ROOT_PATHS=/srv/app:/srv/lib python app.py
```

### 第三步：确认不是「对象不在当前栈上」

`mark()` 默认只看**当前调用栈的局部变量**，所以下面这些它天然看不到：

- 对象只被**模块级单例**持有（缓存、连接池、注册表）→ 用 `mark_all()` 或 `mark(roots_scope="all")`
- 对象只被**别的线程**持有 → `mark_all()`（会遍历所有线程的帧）
- 对象只被 **asyncio 挂起任务**持有 → `mark(roots_scope="all")`（已支持，会自动沿任务找）
- 对象只被 **C 扩展 / 闭包 / 生成器**内部持有 → `memwatch.track("名字", obj)` 显式登记

### 兜底行为

`auto_widen=True`（默认）时，如果宽松过滤后一个变量都收不到，工具会**自动放宽路径过滤重试一次**，
并在简报里标注 `已自动放宽过滤重试`。也就是说正常情况下你不会再拿到一份空的、什么线索都没有的报告。

---

## 3. 四种接入方式

| 方式 | 改动 | 触发 | 适用 |
| --- | --- | --- | --- |
| **A. `memwatch.mark()`**（主推） | 每个节点 1 行 | 代码执行到即抓 | 允许加代码时的默认选择 |
| B. 包装启动 | **0 行** | `python -m memwatch run app.py` + `kill -USR1` | 不想动代码、或线上临时排查 |
| C. PYTHONPATH 注入 | **0 行** | `kill -USR1` / 触发文件 / `MEMWATCH_RSS_MB` / `MEMWATCH_INTERVAL` | gunicorn、celery、k8s |
| D. 到点自动抓 | **0 行** | `MEMWATCH_AT=app.py:handle_request` | 精确抓某环节又不想加代码 |

`mark()` 在热路径上也是安全的：默认 `fail_silent=True` 永不向上抛异常；上一次没跑完时返回
`{"skipped": "busy"}` 而不是排队堆积；同一个标签名最多抓 50 次（防止 `mark()` 被写进循环里爆盘，
一次性提示后自动跳过，可用 `max_marks_per_label=0` 取消）；参数拼错会明确警告
（`忽略未知参数: tops, profle`）而不是静默忽略。

### 其余接口（需要时再用）

```python
memwatch.install(out_dir="/var/log/memwatch")   # 可选；不写也会在第一次 mark() 时自动装
memwatch.mark_all()                             # 全进程扫描版 mark
memwatch.snapshot("节点名")                      # 显式检查点，不打简报
with memwatch.node("支付流程"): ...              # 进出各一次，带 node_delta_mb
@memwatch.watch("handle_request")               # 装饰器
memwatch.track("cache", CACHE)                  # 关注点登记（不在栈上也能进报告）
memwatch.flush()                                # 释放探针内部持有个的大对象
```

### 零改动方式（B/C/D）

```bash
python -m memwatch run app.py                    # 自动 boot/shutdown 各抓一次
PYTHONPATH=/path/to/mem_demo python app.py       # 配合 sitecustomize.py 注入
MEMWATCH_AT=app.py:handle_request python -m memwatch run app.py   # 调用级到点
MEMWATCH_AT=app.py:123            python -m memwatch run app.py   # 行级到点（慢 3-10 倍）
kill -USR1 <pid>                                 # 随时手工抓一次
touch $MEMWATCH_WATCH_FILE                       # 出现该文件即抓
MEMWATCH_RSS_MB=500                              # 越过 500MB 台阶自动抓
```

看报告：`python -m memwatch list [目录]` / `python -m memwatch report <json 或 txt>`。

---

## 4. 完整报告长什么样（真实跑出来的）

```
==============================================================================
memwatch 检查点 #2  at:app_demo.py:finish_request   profile=deep
时间 2026-10-13 14:40:20  进程 57973  耗时 615.6 ms
==============================================================================
RSS 34.89 MB   峰值RSS - MB   tracemalloc 12.09 MB (峰值 12.09)   gc 对象 24229
!! 检测到 1 组 LocalProxy 共享同一目标，持有量不可直接相加（最大一组 16.0 MB）

-- 分配点（绝对，41865 条 trace，take 27.9 ms）
       8192.9 KiB       18 处  /srv/app/app_demo.py:21     <- 8MB payload 的来源行
       2350.1 KiB    24730 处  /srv/app/app_demo.py:23     <- rows 列表
-- 变量维度持有量（贪心保留大小，扫描 20040 个对象）
       8192.7 KiB      11 obj  [module] b.payload_proxy  <LocalProxy:bound>
       2431.8 KiB   20004 obj  [module] __main__.CACHE
-- LocalProxy 明细（共享目标不能相加）
   自身   168 B x3  解引用后    8192.6 KiB  builtins.list [bound]  <== 3 个 proxy 共享，直接相加会多算 16.0 MB
      -> b.payload_proxy, b.stack_proxy, b.lazy_proxy
-- 类型普查
       1454.6 KiB     3328 个  builtins.dict
```

持有者列表里每个变量都带类型/长度/短预览（`list(len=400)`、`dict(len=2)`、`'A-1001'`），
这样你在简报里就能确认「这是哪个变量」，不用再回去看代码。

JSON 里同样的数据，可以直接进日志/监控：

```python
rep = json.load(open("/var/log/memwatch/12345_0002_signal.json"))
rep["counters"]["rss_mb"]                     # RSS
rep["holders"][0]["root"], rep["holders"][0]["bytes"]   # 最大变量
rep["holders"][0]["preview"]                  # 类型/长度/短值
[p["target_bytes"] for p in rep["proxies"]]   # proxy 解引用后大小
rep["sites"]["rows"][0]["where"]              # 最大分配点
rep["elapsed_ms"], rep["sites"]["take_snapshot_ms"]     # 单次开销自证
```

---

## 5. 开销（62 万 gc 对象 / 150 万条分配记录实测，CPython 3.9，macOS）

| profile | 内容 | 单次耗时 | 建议 |
| --- | --- | --- | --- |
| `counters` | RSS/USS/峰值 + tracemalloc 总量（配 `count_objects=False`） | **0.3 ms** | 每请求、循环内 |
| `counters`（默认） | 上面 + `len(gc.get_objects())` | 45 ms | 低频打点 |
| `standard` | + 变量持有量 + LocalProxy 表 | 140 ms | **`mark()` 默认档** |
| `deep` | + 分配点统计/差分 + 类型普查 + 增长差分 | 0.3 ~ 7 s | 可疑节点、按需 |

`mark()` 默认 `roots_scope="locals"`（只看当前调用栈），比上面的「全进程扫描」还便宜，
实测在示例业务里是 **几十到一百多毫秒**。`roots_scope="all"` 才走全堆。

`deep` 档成本分布（决定怎么调优）：

| 环节 | 成本 |
| --- | --- |
| `take_snapshot()` | 0.5 s 底价，随 trace 条数涨 |
| `statistics()` / `compare_to()` | **3-7 s**（真正的大头） |
| `Snapshot.filter_traces()` | 14 s，**别用**（Python 层遍历 + 复制快照） |
| 类型普查（带类型缓存） | 0.44 s / 62 万对象（不缓存是 2.6 s） |
| 持有者扫描 | 71 ms / 62 万对象 |
| `Growth` id 差分（基线 / diff） | 1.0 s / 0.6 s |

线上建议：常态 `mark()`，用 `MEMWATCH_RSS_MB` 或 `kill -USR1` 在可疑时刻拉 `deep`。

---

## 6. 配置项

`install(**kwargs)` / `mark(**opts)` 都接受这些键（同名环境变量可覆盖）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `out_dir` | `./memwatch-reports` | 落盘目录（文件名含 pid + 序号 + 标签） |
| `enabled` | `True` | `MEMWATCH_ENABLED=0` 远程关闭 |
| `profile` | `standard` | `counters` / `standard` / `deep` |
| `roots_scope` | `all`（`mark` 用 `locals`） | `locals`=只看调用栈；`all`=全进程 |
| `top` | `20` | 每节条数 |
| `brief` | `None`（mark 打、snapshot 不打） | 控制台简报开关 |
| `brief_top` | `5` | 简报里列几个变量 |
| `preview` | `True` | 变量带类型/长度/短值预览 |
| `max_marks_per_label` | `50` | 同名标签上限，0=不限 |
| `auto_widen` | `True` | 作用域内一个变量都收不到时，自动放宽路径过滤重试一次 |
| `trace` | `True` | 安装时开启 tracemalloc（**越早越好**） |
| `frames` | `5` | tracemalloc 栈深度，实测对成本几乎无影响 |
| `root_paths` | 自动 | 只把自己项目的模块/帧当根；自动取 `__main__` 目录 / `argv[0]` / cwd |
| `include_third_party` | `False` | 是否把 site-packages 的模块也当根 |
| `include_threads` / `include_asyncio` | `True` | 线程帧 / asyncio 协程帧 |
| `resolve_proxies` | `True` | LocalProxy 解引用（关键） |
| `skip_infra` | `True` | 不展开 module/function/frame，避免一个根吃掉全世界 |
| `max_objects` | `500000` | 单次持有者扫描对象上限 |
| `max_seconds` | `5.0` | 协作式时间预算，超了截断并把原因写进 `warnings` |
| `count_objects` | `True` | 是否统计 `len(gc.get_objects())` |
| `write_report` / `print_report` | `True` / `False` | 落盘 / 同时打完整报告 |
| `on_signal` / `watch_file` / `at` / `rss_threshold_mb` / `interval` | — | 五种零改动触发器 |
| `sink` | `None` | `callable(report)`，用于上报日志/APM |
| `fail_silent` | `True` | 异常吞掉并记进 `report["errors"]` |
| `label_prefix` | `""` | 报告文件名前缀 |

环境变量：`MEMWATCH_OUT` `MEMWATCH_PROFILE` `MEMWATCH_TOP` `MEMWATCH_AT` `MEMWATCH_WATCH_FILE`
`MEMWATCH_FRAMES` `MEMWATCH_RSS_MB` `MEMWATCH_INTERVAL` `MEMWATCH_RESOLVE_PROXY` `MEMWATCH_ENABLED`
`MEMWATCH_ROOT_PATHS`（`:` 分隔，零改动部署时指定业务代码目录）`MEMWATCH_AUTO`（=0 则不注入）。

---

## 7. 明确保证 / 明确限制

**保证（都有对应测试）**

1. 不向上抛异常：去掉业务里的 `try` 也不会因为测量而失败。
2. 不改变被测对象生命周期：快照后释放业务引用，6 MB payload 实测真回收。
3. 并发调用不排队堆积：上一次未完成时后来者返回 `skipped="busy"`。
4. `flush()` 释放内部持有的 tracemalloc 快照与 `track()` 引用。
5. 报告里不出现探针自己的行号（分配点先于扫描采集 + 按文件过滤自身帧）。
6. `python -c` / REPL / Jupyter（帧文件名为 `<string>`、`<ipython-input-N>`）也能正常归因。
7. 作用域收不到变量时**不会只给一句「没有」**：会打诊断、给出修法，并自动放宽过滤重试一次。
8. `root_paths` 未显式指定时，每次快照都从**已加载模块**反推业务目录（业务模块是逐步 import 的，只在 install 时算一次会漏）。

**限制（务必知道）**

1. **「每个变量占多少」没有唯一答案**。内存是图不是树，同一份数据可被多个变量引用。
   本工具用**贪心约定**：每个对象只算给「第一个到达它的根」，报告里 `objects` 列是这一项包含的对象数。
   要别的口径得换支配树（guppy3）。
2. **LocalProxy 必须解引用**：proxy 本体只有 56 B，数据挂在 `Local` 的 `ContextVar` 上，
   纯 `gc.get_referents` 遍历走不到（实测链是 `LocalProxy -> Local -> ContextVar -> str`）。
   多个 proxy 可能指向同一目标，报告单列「共享」警告，**不要把 `target_bytes` 相加**。
   未绑定上下文时 `_get_current_object()` 抛 `RuntimeError`，按 `unbound` 记录并跳过。
3. **`gc.get_referents(dict)` 会跳过 `str` 类型的键**（CPython 认为 str 不参与循环引用）。
   `{字符串: 值}` 是现实代码里最常见的结构，不补回来会系统性漏算字典键的内存
   —— 本工具已用 `_children()` 显式补回，2000 个不同 str 键的字典实测覆盖率 100%。
4. **`str` / `bytes` / `bytearray` / `ndarray` 不被 gc 追踪**：不会出现在类型普查里
   （普查基于 `gc.get_objects()`，是**下界**），但通过容器可达时其大小会计入持有量；
   `Growth` 差分专门有 `untracked_bytes` 列捞这部分。
5. **tracemalloc 必须早启动**：晚了报告会 warning 说分配点数据不完整。
   想拿分配点就把 `tracemalloc.start()` 或 `memwatch.install()` 放在程序入口。
6. **纯 C 库的大块内存看不到**：numpy / PIL / mmap / C 扩展的 `malloc` 不在 tracemalloc 视野，
   只能靠 RSS 对照，或换 memray / filprofiler。
7. **行级触发慢 3-10 倍**（`sys.settrace` 每行回调）；调用级快（`sys.setprofile` 只在 call 回调）。
   两者都会和 debugger / coverage 抢钩子，命中后会还原原钩子。
8. `max_seconds` 是**协作式**预算，纯 Python 调用无法硬中断；超时截断并在 `warnings` 说明。

---

## 8. 自测

```bash
python test_modes.py     # 50 项
```

覆盖：包装启动、boot/shutdown、调用级/行级到点、sitecustomize + `SIGUSR1`、触发文件、
`mark()` 自动标签/自动作用域/预览/跨 mark 增量/参数拼错警告/同名标签限流/全量切换、
一行 + 装饰器 + 上下文 + track、CLI `list`/`report`、关闭开关与异常路径、不泄漏、并发跳过、
字典 str 键完整性（100% 覆盖）、asyncio 挂起任务归因、
作用域解析（启动器/cwd 无关、软链路径、root_paths 写错、真空作用域、`diagnose()` 输出）、
屏蔽 `psutil`/`werkzeug` 后仍可用（可选依赖验证）、单次开销。

---

## 9. 文件

| 文件 | 作用 |
| --- | --- |
| `memwatch.py` | **接口本体**。单文件、零第三方依赖（psutil 可选，没有就用 `resource` 兜底）。复制进项目即可 |
| `requirements.txt` | 跑 demo/测试所需：`psutil` + `werkzeug`（生产接入本身不需要装任何东西） |
| `requirements-py39.lock.txt` | Python 3.9 上实测通过的确切版本组合 |
| `requirements-analyzers.txt` | 可选：memray / filprofiler / scalene / pympler / objgraph / guppy3 / memory_profiler |
| `app_mark.py` | **易用性示例**：只加 `memwatch.mark()` |
| `sitecustomize.py` | 零改动注入入口，放 `PYTHONPATH` 上 |
| `test_modes.py` | 50 项接入验证 |
| `app_demo.py` / `app_inline.py` | 零改动示例 / 显式接口示例 |
| `b.py` / `a.py` | A 依赖 b 的 LocalProxy 场景 |
| `README.md` | 原理与工具选型 |
| `memprobe.py` | 实验室版探针（探索用，生产用 `memwatch.py`） |
