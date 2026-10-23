# mempeak：整段运行的内存峰值监控

`memwatch` 回答“某个检查点上内存花在哪”；`mempeak` 回答“整个运行过程中内存**最高**到过多少、在哪个阶段、离 OOM 还有多远”，用于验证内存优化是否生效。

单文件 `mempeak.py`，只依赖标准库（Linux 读 `/proc` 和 cgroup；其他平台装了 psutil 时尽量支持）。

## 1. 零改动：包一层命令（推荐）

```bash
python -m mempeak run --out before.json -- python train.py --epochs 1
# ……做内存优化……
python -m mempeak run --out after.json  -- python train.py --epochs 1
python -m mempeak compare before.json after.json
```

- 目标程序作为子进程运行，**不需要 import 任何东西**；任何命令都可以（`torchrun`、`bash run.sh`……），子进程/多进程会被一起统计。
- 目标即使被 OOM killer 杀掉（SIGKILL），监控进程仍然存活，照样输出报告，并标注 `likely_oom`。
- `--out` 的 JSON 在运行期间每秒刷新一次，监控进程本身被杀也能留下最后的峰值。

常用选项：

| 选项 | 作用 |
| --- | --- |
| `--interval 0.02` | 采样周期（秒）。采样间隙的短尖峰仍会被内核 HWM 捕获 |
| `--pss` | 多进程时额外采样 PSS（共享页按比例分摊，比 RSS 求和更接近真实占用） |
| `--kill-above 6G` | 超过阈值就杀掉目标：在大机器上模拟“小内存环境会不会 OOM”，而不必真的把机器打爆 |
| `--fail-above 6G` | 峰值超过阈值时退出码为 3，用作 CI 的内存回归门禁 |
| `--label <commit>` | 写入报告的自由标签 |

`compare --fail-if-higher 5` 在峰值增长超过 5% 时退出码为 3。`python -m mempeak show x.json` 重新打印报告摘要。

## 2. 进程内：一行包住要测的代码

```python
import mempeak

with mempeak.monitor(out="peak.json") as m:
    main()
print(m.report.peak_bytes)

# 或者
@mempeak.profile(out="peak.json")
def main(): ...

# 或者
mempeak.start(out="peak.json"); main(); rep = mempeak.stop()   # 忘记 stop 时 atexit 会收尾
```

`start()` / `monitor()` / `profile()` 参数：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `interval` | 0.01 | 采样周期（秒），后台守护线程，读一次 `/proc/self/statm` 约十几微秒 |
| `out` / `flush_every` | None / 1.0 | JSON 路径 / 运行中刷新间隔 |
| `python_heap` | False | 用 tracemalloc 统计 Python 堆峰值（会明显拖慢分配，按需开启） |
| `exact_phases` | False | Linux：在阶段边界重置 VmHWM，得到**精确**的每阶段峰值。副作用：其他读取 VmHWM / `ru_maxrss` 的工具会看到被重置的值 |
| `pss` | False | 同时采样 PSS |
| `print_summary` | True | stop 时把摘要打印到 stderr |

## 3. 可选：阶段标记（找出峰值出在哪一步）

```python
with mempeak.phase("load"):
    data = load()

@mempeak.phase("train")
def train(): ...
```

没有任何监控在运行时它是空操作（几微秒），可以长期留在代码里。进程内模式直接记录；`run` 包装模式下，目标进程（含 fork 出的子进程）把标记写入 `MEMPEAK_PHASE_FILE` 指向的临时文件，由外部监控在下一次采样时读取——因此包装模式的阶段峰值是采样值，不报告 Δ。

## 4. 如何读报告

```
[mempeak] external  status=exited  1.73s  samples=82 @ 20ms
  exit: code=0 signal=None
  PEAK            413.8 MiB   (at 1.69s, phase=transform)
  ├ kernel HWM    413.8 MiB   (exact peak RSS of the largest single process)
  ├ sampled RSS   410.7 MiB   (sum over 1 process(es) seen)
  └ baseline 3.9 MiB → final 410.7 MiB
  cgroup v1: peak usage 425.1 MiB = 3% of limit 13.4 GiB; oom_kill +0
  phase                         count   time(s)         peak Δ enter→exit
  load                              1      0.83    214.4 MiB          n/a
  transform                         1      0.82    410.7 MiB          n/a
```

| 字段 | 含义 |
| --- | --- |
| `peak.peak_bytes`（PEAK） | 头条数字 = max(采样峰值, 内核 HWM)。单进程时就是精确的峰值 RSS；多进程时是进程树 RSS 之和的下界 |
| `peak.hwm_bytes` | 内核记录的峰值 RSS（`VmHWM`，子进程退出后还会合并 `wait4` 的 `ru_maxrss`），不受采样间隔影响；多进程时取最大的单个进程 |
| `peak.sampled_bytes` / `sampled_at_s` / `phase_at_peak` | 采样到的最高值（多进程为求和）、发生时刻和当时所处阶段 |
| `cgroup` | 容器/cgroup 的内存使用峰值、上限、`oom_kill` 计数变化。**容器 OOM 看的就是它**；注意 cgroup 是同组所有进程共享的 |
| `exit.likely_oom` | 目标被 SIGKILL 且 cgroup `oom_kill` 计数增加（或无法读取） |
| `phases[]` | 每个阶段的次数、耗时、峰值；`exact: true` 表示来自 VmHWM 而非采样 |
| `timeline` | 降采样后的 `[t_s, rss, cgroup, phase]`，最多约 2000 点，可自行画图 |

## 5. 验证优化时的建议

1. 前后两次使用**相同输入、相同环境**，最好各跑 2–3 次看波动；`compare` 只比较单次结果。
2. 主要看 `PEAK` 与 `cgroup peak`；`final` 低不代表峰值低。
3. 优化前那次如果本来就 OOM，加 `--kill-above` 设一个安全阈值，或接受“峰值是下界”（报告会给出 WARNING）。
4. 峰值降了但想知道剩下的大头在哪，再用 `memwatch.mark_all()` 在 `phase_at_peak` 所示阶段打检查点。

## 6. 范围与限制

- 统计的是 RSS（常驻物理内存），不含已换出到 swap 的部分；不统计 GPU 显存。
- 采样会漏掉间隙中的尖峰：单进程有内核 HWM 兜底；多进程的“同时刻求和”只能靠采样，建议缩短 `--interval`。
- RSS 求和会重复计算 fork 共享页，用 `--pss` 修正。
- 未开启 `exact_phases` 时，阶段峰值是采样值；嵌套阶段的外层峰值包含内层。
- 非 Linux 平台：需要 psutil 才能采样当前 RSS 与子进程，cgroup 信息不可用。

演示：`examples/peak_demo.py`（同一任务的优化前/后两个版本）。测试：`tests/test_mempeak.py`。
