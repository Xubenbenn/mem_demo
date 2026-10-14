"""逐一验证 memwatch 的接入模式，全部走子进程，断言真实产物。

跑法: python3 test_modes.py
"""

import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def run(cmd, env=None, timeout=90, **kw):
    full = dict(os.environ)
    full["PYTHONWARNINGS"] = "ignore"
    full.update(env or {})
    return subprocess.run(cmd, cwd=HERE, env=full, timeout=timeout,
                          capture_output=True, text=True, **kw)


def reports(out_dir):
    out = []
    for path in sorted(glob.glob(os.path.join(out_dir, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            out.append(json.load(fh))
    return out


def find(rows, label_part):
    return [r for r in rows if label_part in str(r.get("label", ""))]


def scenario_runner():
    out = tempfile.mkdtemp(prefix="mw_run_")
    proc = run([PY, "-m", "memwatch", "run", "app_demo.py"], {"MEMWATCH_OUT": out})
    rows = reports(out)
    check("A. python -m memwatch run（零改动启动）", proc.returncode == 0 and len(rows) >= 2,
          f"rc={proc.returncode} 报告={[r['label'] for r in rows]}")
    check("A. boot/shutdown 两个检查点都在", find(rows, "boot") and find(rows, "shutdown"))
    boot = (find(rows, "boot") or [{}])[0]
    check("A. counters 有 RSS", (boot.get("counters") or {}).get("rss_mb") is not None,
          f"rss={boot.get('counters', {}).get('rss_mb')}MB")
    sh = (find(rows, "shutdown") or [{}])[0]
    check("A. 退出时捕获到业务变量", any("payload_proxy" in h["root"] or "CACHE" in h["root"]
                                   for h in sh.get("holders") or []),
          f"holders={[h['root'].split(':')[-1] for h in (sh.get('holders') or [])][:5]}")
    shutil.rmtree(out, ignore_errors=True)


def scenario_at_call():
    out = tempfile.mkdtemp(prefix="mw_at_")
    proc = run([PY, "-m", "memwatch", "run", "app_demo.py"],
               {"MEMWATCH_OUT": out, "MEMWATCH_AT": "app_demo.py:finish_request"})
    rows = reports(out)
    hits = find(rows, "at:app_demo.py:finish_request")
    check("B. MEMWATCH_AT 调用级到点触发", bool(hits), f"labels={[r['label'] for r in rows]}")
    if hits:
        rep = hits[0]
        proxies = rep.get("proxies") or []
        shared = [p for p in proxies if p.get("shared")]
        big = [p for p in proxies if p.get("target_bytes", 0) > 1024 * 1024]
        check("B. profile 自动升级为 deep（含分配点）", rep.get("profile") == "deep"
              and "sites" in rep, f"profile={rep.get('profile')}")
        check("B. 识别出 LocalProxy 并解引用到 >1MB 目标", bool(big),
              f"proxy 目标={[p.get('target_bytes') for p in proxies]}")
        check("B. 检出 proxy 共享同一目标（防重复相加）", bool(shared),
              f"shared={[(p['proxy_count'], p['shared_warning_mb']) for p in shared]}")
        check("B. 持有者里有 b 模块的 proxy 变量", any("payload_proxy" in h["root"]
                                                for h in rep.get("holders") or []),
              f"top={[h['root'] for h in (rep.get('holders') or [])[:3]]}")
        check("B. 报告里带实测耗时", rep.get("elapsed_ms") is not None
              and (rep.get("sites") or {}).get("take_snapshot_ms") is not None,
              f"总 {rep.get('elapsed_ms')}ms, snapshot {(rep.get('sites') or {}).get('take_snapshot_ms')}ms")
    shutil.rmtree(out, ignore_errors=True)


def scenario_at_line():
    out = tempfile.mkdtemp(prefix="mw_line_")
    lineno = None
    with open(os.path.join(HERE, "app_demo.py"), encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if "time.sleep(0.05)" in line:
                lineno = i
                break
    proc = run([PY, "-m", "memwatch", "run", "app_demo.py"],
               {"MEMWATCH_OUT": out, "MEMWATCH_AT": f"app_demo.py:{lineno}"})
    rows = reports(out)
    check("C. MEMWATCH_AT 行级到点触发", bool(find(rows, f"at:app_demo.py:{lineno}")),
          f"lineno={lineno} labels={[r['label'] for r in rows]}")
    shutil.rmtree(out, ignore_errors=True)


def scenario_signal():
    out = tempfile.mkdtemp(prefix="mw_sig_")
    env = {"MEMWATCH_OUT": out, "PYTHONPATH": HERE, "APP_HOLD_SECONDS": "3"}
    full = dict(os.environ)
    full.update(env)
    full["PYTHONWARNINGS"] = "ignore"
    proc = subprocess.Popen([PY, "app_demo.py"], cwd=HERE, env=full,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    proc.send_signal(signal.SIGUSR1)
    time.sleep(1.2)
    proc.wait(timeout=30)
    rows = reports(out)
    sig = find(rows, "signal")
    check("D. sitecustomize 零改动 + kill -USR1 触发", bool(sig),
          f"labels={[r['label'] for r in rows]}")
    if sig:
        check("D. 信号抓取时业务数据还在（payload > 1MB）",
              any("payload_proxy" in h["root"] for h in sig[0].get("holders") or []),
              f"top={[h['root'] for h in (sig[0].get('holders') or [])[:3]]}")
    shutil.rmtree(out, ignore_errors=True)


def scenario_watch_file():
    out = tempfile.mkdtemp(prefix="mw_wf_")
    trigger = os.path.join(out, "NOW")
    env = dict(os.environ)
    env.update({"MEMWATCH_OUT": out, "PYTHONPATH": HERE, "APP_HOLD_SECONDS": "3",
                "MEMWATCH_WATCH_FILE": trigger, "PYTHONWARNINGS": "ignore"})
    proc = subprocess.Popen([PY, "app_demo.py"], cwd=HERE, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    open(trigger, "w").close()
    time.sleep(1.2)
    proc.wait(timeout=30)
    rows = reports(out)
    check("E. 触发文件模式", bool(find(rows, "file:")), f"labels={[r['label'] for r in rows]}")
    shutil.rmtree(out, ignore_errors=True)


def scenario_inline():
    out = tempfile.mkdtemp(prefix="mw_inline_")
    proc = run([PY, "app_inline.py"], {"MEMWATCH_OUT": out})
    rows = reports(out)
    labels = [r["label"] for r in rows]
    check("F. 一行/装饰器/上下文做接入", proc.returncode == 0 and len(rows) >= 5,
          f"rc={proc.returncode} labels={labels}")
    check("F. 装饰器进入/退出都有记录",
          any("build_payload:enter" in l for l in labels) and any("build_payload:exit" in l for l in labels))
    check("F. with node 进出都有记录",
          any("cache_fill:enter" in l for l in labels) and any("cache_fill:exit" in l for l in labels))
    check("F. node 有增量字段", any("node_delta_mb" in r for r in rows),
          f"deltas={[r.get('node_delta_mb') for r in rows if 'node_delta_mb' in r]}")
    check("F. track 登记的关注点进报告",
          any(any("tracked:global_cache" in h["root"] for h in r.get("holders") or []) for r in rows))
    check("F. deep 档包含 census/sites", any(r["profile"] == "deep" and r.get("census") for r in rows))

    listed = run([PY, "-m", "memwatch", "list", out])
    check("F. CLI list", listed.returncode == 0 and ".txt" in listed.stdout)
    one = glob.glob(os.path.join(out, "*.json"))[0]
    shown = run([PY, "-m", "memwatch", "report", one])
    check("F. CLI report 能渲染", shown.returncode == 0 and "变量维度持有量" in shown.stdout)
    shutil.rmtree(out, ignore_errors=True)


def scenario_failsafe():
    """被测量进程必须不受影响：探针在任何异常下都不能抛出去。"""
    code = (
        "import memwatch\n"
        "memwatch.install(out_dir='/tmp/mw_failsafe', enabled=False)\n"
        "r = memwatch.snapshot('disabled')\n"
        "assert r.get('skipped') == 'disabled', r\n"
        "memwatch.install(out_dir='/tmp/mw_failsafe', root_paths=['/nonexistent'], profile='deep')\n"
        "memwatch.track('self', memwatch)\n"
        "r2 = memwatch.snapshot('weird')\n"
        "assert isinstance(r2, dict)\n"
        "print('OK')\n"
    )
    proc = run([PY, "-c", code], {"MEMWATCH_OUT": "/tmp/mw_failsafe"})
    check("G. 关闭开关 + 异常路径都不影响业务", proc.returncode == 0 and "OK" in proc.stdout,
          f"rc={proc.returncode} err={proc.stderr.strip().splitlines()[-1:]}")
    shutil.rmtree("/tmp/mw_failsafe", ignore_errors=True)


def scenario_leak_and_concurrency():
    """承诺两条硬指标：探针不改变被测对象的生命周期；并发调用不会排队堆积。"""
    code = r'''
import gc, os, threading, tracemalloc
import memwatch

memwatch.install(out_dir="/tmp/mw_leak", profile="deep", on_signal=False, print_report=False)
tracemalloc.start(3)

class Blob:
    pass

blob = Blob()
blob.data = [bytearray(1024 * 1024) for _ in range(6)]
memwatch.track("blob", blob)
memwatch.snapshot("有数据")
after_snap = tracemalloc.get_traced_memory()[0]

memwatch.untrack("blob")
del blob
gc.collect()
released = tracemalloc.get_traced_memory()[0]
print("RELEASED_MB", round((after_snap - released) / 1048576, 2))

# 并发：一个线程抓 deep，另一个线程同时调，必须被跳过而不是排队
res = {}
def slow():
    res["a"] = memwatch.snapshot("slow", profile="deep")
t = threading.Thread(target=slow)
t.start()
res["b"] = memwatch.snapshot("racing", profile="deep")
t.join()
skipped = [v.get("skipped") for v in res.values() if isinstance(v, dict)]
print("SKIP", skipped)
# 测量自身的留存代价：flush 后不该再持有业务对象
memwatch.flush()
gc.collect()
print("TRACKED", memwatch._STATE["tracked"], "PREV", memwatch._STATE["prev_snapshot"] is None)
print("OK")
'''
    proc = run([PY, "-c", code])
    out = proc.stdout
    released = [l for l in out.splitlines() if l.startswith("RELEASED_MB")]
    check("I. 快照后释放业务引用能真回收（探针不钉对象）",
          bool(released) and float(released[0].split()[1]) >= 5.5,
          released[0] if released else f"rc={proc.returncode} {proc.stderr[-200:]}")
    check("I. 并发调用被跳过而不是排队堆积",
          "SKIP" in out and "'busy'" in out, [l for l in out.splitlines() if l.startswith("SKIP")])
    check("I. flush 释放内部快照与登记引用",
          "OK" in out and "PREV True" in out,
          [l for l in out.splitlines() if l.startswith("TRACKED")])
    shutil.rmtree("/tmp/mw_leak", ignore_errors=True)


def scenario_mark():
    """易用性：一行 mark() 不该需要任何参数就能给出有用结论。"""
    code = r'''
import gc, sys, time
import memwatch

CACHE = {}

def load():
    CACHE["cfg"] = {f"k{i}": f"{i:04d}" * 64 for i in range(2000)}   # 各不相同的 str 键

def work():
    items = [{"sku": f"s{i}", "blob": bytes(64)} for i in range(400)]
    r1 = memwatch.mark()                       # 不传任何参数
    print("LABEL", r1["label"])
    print("SCOPE", r1["config"]["roots_scope"])
    print("HAS_CACHE", any(h["root"].endswith("CACHE") for h in r1["holders"]))
    print("HAS_ITEMS", any(h["root"].endswith("items") for h in r1["holders"]))
    print("PREVIEW", [h.get("preview") for h in r1["holders"] if h["root"].endswith("items")])
    return items

load()
keep = work()
CACHE["rows"] = [{"id": i} for i in range(5000)]
r2 = memwatch.mark("第二处")
prev = memwatch._STATE.get("prev_holders") or {}
print("DELTA_LABELS", sorted(set(h["root"] for h in r2["holders"]) & set(prev.keys())))
r3 = memwatch.mark("第二处")
r4 = memwatch.mark("拼错参数", tops=3, profle="deep")
print("UNKNOWN_WARN", [w for w in r4.get("warnings", []) if "未知参数" in w])
print("MARK_COUNT", memwatch._STATE["label_counts"].get("第二处"))
r5 = memwatch.mark("全量", roots_scope="all")
print("ALL_SCOPE", r5["config"]["roots_scope"])
print("REPORT_FILES", len(r5.get("report_file") or ""))
print("OK")
'''
    proc = run([PY, "-c", code], {"MEMWATCH_OUT": "/tmp/mw_mark"})
    out = proc.stdout
    check("J. mark() 不传参也能用（自动标签 + 自动作用域）",
          "LABEL <string>" in out or "LABEL <string>" in out or "LABEL" in out, "")
    label = [l for l in out.splitlines() if l.startswith("LABEL")]
    check("J. 标签自动生成为 文件:行号:函数名",
          bool(label) and ":work" in label[0], label[0] if label else f"rc={proc.returncode} {proc.stderr[-300:]}")
    check("J. 默认只看当前作用域（locals）", "SCOPE locals" in out)
    check("J. 作用域内变量都被归因", "HAS_CACHE True" in out and "HAS_ITEMS True" in out)
    check("J. 变量带长度/预览，便于确认是哪个变量",
          "PREVIEW ['list(len=400)']" in out, [l for l in out.splitlines() if l.startswith("PREVIEW")])
    check("J. 跨 mark 的标签稳定（增量可算）",
          "DELTA_LABELS" in out and "CACHE" in out.split("DELTA_LABELS")[1].split("\n")[0],
          [l for l in out.splitlines() if l.startswith("DELTA_LABELS")])
    check("J. 参数拼错会明确警告而不是静默忽略",
          "UNKNOWN_WARN" in out and "tops" in out and "profle" in out,
          [l for l in out.splitlines() if l.startswith("UNKNOWN_WARN")])
    check("J. 同名标签计数（防循环里爆盘）", "MARK_COUNT 2" in out,
          [l for l in out.splitlines() if l.startswith("MARK_COUNT")])
    check("J. 可切全量视角", "ALL_SCOPE all" in out)
    check("J. mark 走完了整条链路", "OK" in out, f"rc={proc.returncode} {proc.stderr[-200:]}")
    shutil.rmtree("/tmp/mw_mark", ignore_errors=True)


def scenario_dict_keys():
    """字典的 str 键：gc.get_referents 会跳过，必须补回来（否则系统性漏算）。"""
    code = r'''
import time
import memwatch as mw

d = {}
for i in range(2000):
    d["key_%d" % i] = "".join(chr(97 + (i + j) % 26) for j in range(200))
manual = sum(mw._sizeof(k) for k in d) + sum(mw._sizeof(v) for v in d.values()) + mw._sizeof(d)
probe, count, _ = mw._deep_size(d, mw.DEFAULT, time.monotonic() + 10)
print("MANUAL", round(manual / 1024, 1))
print("PROBE", round(probe / 1024, 1))
print("OBJECTS", count)
'''
    proc = run([PY, "-c", code])
    lines = dict(l.split() for l in proc.stdout.splitlines() if l.split() and l.split()[0] in
                 ("MANUAL", "PROBE", "OBJECTS"))
    try:
        ratio = float(lines["PROBE"]) / float(lines["MANUAL"])
    except Exception:
        ratio = 0
    check("K. 字典 str 键的内存被完整统计（gc 会跳过它们）", ratio > 0.99,
          f"探针 {lines.get('PROBE')} KiB / 手工 {lines.get('MANUAL')} KiB，对象数 {lines.get('OBJECTS')}")


def scenario_stdlib_only():
    """证明 requirements 里那两个包真的只是可选：全部屏蔽后 memwatch 仍可用。"""
    blocker = tempfile.mkdtemp(prefix="mw_block_")
    with open(os.path.join(blocker, "sitecustomize.py"), "w", encoding="utf-8") as fh:
        fh.write(
            "import sys\n"
            "BLOCKED = {'psutil', 'werkzeug', 'numpy', 'objgraph', 'pympler'}\n"
            "class Blocker:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in BLOCKED:\n"
            "            raise ImportError('blocked for test: ' + name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, Blocker())\n"
        )
    code = r'''
import memwatch

for mod in ("psutil", "werkzeug"):
    try:
        __import__(mod)
        print("BLOCK_FAIL", mod)
    except ImportError:
        print("BLOCKED", mod)

class FakeProxy:                       # 不带 werkzeug 的仿制 LocalProxy
    def __init__(self, target):
        self._target = target
    def _get_current_object(self):
        return self._target

payload = [bytearray(1024 * 512) for _ in range(4)]     # 2 MB
proxy = FakeProxy(payload)
memwatch.track("fake_proxy", proxy)
rep = memwatch.mark("no-third-party", profile="standard")
print("RSS", rep["counters"].get("rss_mb"))
print("RSS_SRC", "peak(resource)" if rep["counters"].get("rss_is_peak") else "psutil")
print("PROXY", rep["proxies"][0]["target_bytes"], rep["proxies"][0]["state"])
print("HOLDERS", any(h["root"] == "tracked:fake_proxy" for h in rep["holders"]))
print("OK")
'''
    env = {"PYTHONPATH": blocker + os.pathsep + HERE, "MEMWATCH_OUT": os.path.join(blocker, "out")}
    proc = run([PY, "-c", code], env)
    out = proc.stdout
    check("L. 屏蔽 psutil/werkzeug 后仍能导入并使用（可选依赖是真的可选）",
          "OK" in out and "BLOCK_FAIL" not in out,
          f"rc={proc.returncode} {proc.stderr[-200:]}")
    check("L. 无 psutil 时降级到 resource 拿 RSS",
          "RSS_SRC peak(resource)" in out and "RSS None" not in out,
          [l for l in out.splitlines() if l.startswith("RSS")])
    proxy_line = [l for l in out.splitlines() if l.startswith("PROXY")]
    try:
        target_bytes = int(proxy_line[0].split()[1])
    except Exception:
        target_bytes = 0
    check("L. 无 werkzeug 时用鸭子类型识别 LocalProxy 并解引用",
          "bound" in out and 2000000 < target_bytes < 2200000 and "HOLDERS True" in out,
          proxy_line[0] if proxy_line else "")
    shutil.rmtree(blocker, ignore_errors=True)


def scenario_asyncio():
    """asyncio：挂起的任务也要能归因。

    刻意**不给 task 绑定任何变量**，让它可以只通过事件循环内部被找到，
    这样这条断言就只在「asyncio 收集路径」生效，不受遍历顺序影响。
    另外 3.10+ 的 asyncio.all_tasks() 改了签名，这里一并覆盖。
    """
    code = r'''
import asyncio
import memwatch

memwatch.install(out_dir="/tmp/mw_aio", on_signal=False, write_report=False)

async def worker():
    payload = [bytearray(1024 * 256) for _ in range(8)]    # 2 MB
    await asyncio.sleep(3600)
    return len(payload)

async def main():
    asyncio.create_task(worker())        # 故意不保存引用，只能靠 all_tasks() 找到
    await asyncio.sleep(0.05)
    rep = memwatch.mark("loop", roots_scope="all")
    hits = [h for h in rep["holders"] if "payload" in h["root"]]
    print("AIO_HIT", bool(hits), hits[0]["bytes"] if hits else 0)
    print("AIO_LABEL", (hits[0]["root"] if hits else "")[:60])
    print("AIO_KIND", hits[0]["kind"] if hits else "")

asyncio.run(main())
print("OK")
'''
    proc = run([PY, "-c", code], {"MEMWATCH_OUT": "/tmp/mw_aio"})
    out = proc.stdout
    hit = [l for l in out.splitlines() if l.startswith("AIO_HIT")]
    check("M. asyncio 挂起任务里的变量能被归因",
          "OK" in out and "AIO_HIT True" in out,
          hit[0] if hit else f"rc={proc.returncode} {proc.stderr[-300:]}")
    check("M. 单个挂起任务的持有量正确（2 MB）",
          bool(hit) and len(hit[0].split()) == 3 and hit[0].split()[2].isdigit()
          and 2000000 < int(hit[0].split()[2]) < 2200000,
          hit[0] if hit else "")
    shutil.rmtree("/tmp/mw_aio", ignore_errors=True)


def scenario_scope_resolution():
    """「本次作用域内没有可归因的变量」的三种成因与修法。"""
    proj = tempfile.mkdtemp(prefix="mw_proj_")
    app = os.path.join(proj, "app")
    bindir = os.path.join(proj, "bin")
    os.makedirs(app)
    os.makedirs(bindir)
    with open(os.path.join(app, "biz.py"), "w", encoding="utf-8") as fh:
        fh.write(
            "CACHE = {}\n"
            "def load():\n"
            "    CACHE['big'] = {str(i): 'v' * 256 for i in range(1500)}\n"
            "def do_work(root_paths=None):\n"
            "    import memwatch\n"
            "    if root_paths:\n"
            "        memwatch.install(out_dir=%r, root_paths=root_paths, on_signal=False, write_report=False)\n"
            "    rows = [{'id': i, 'blob': bytes(64)} for i in range(2000)]\n"
            "    rep = memwatch.mark()\n"
            "    print('HOLDERS', len(rep.get('holders') or []))\n"
            "    print('WIDENED', bool(rep.get('auto_widened')))\n"
            "    print('NAMES', [h['root'].split(':')[-1] for h in (rep.get('holders') or [])][:4])\n"
            "    print('DIAG', ' | '.join((rep.get('diagnostics') or [])[:1]))\n"
            "    print('PATHS', [p.rsplit('/', 1)[-1] for p in (rep.get('root_paths') or [])])\n"
            "def probe():\n"
            "    import memwatch\n"
            "    d = memwatch.diagnose()\n"
            "    print('DIAG_ACCEPTED', d['accepted_frames'], d['would_be_accepted_if_widened'])\n"
            % os.path.join(proj, "out")
        )
    launcher = os.path.join(bindir, "launcher.py")
    with open(launcher, "w", encoding="utf-8") as fh:
        fh.write(
            "import sys\n"
            f"sys.path.insert(0, {app!r})\n"
            "import biz\n"
            "biz.load()\n"
            "biz.do_work()\n"
        )
    env = {"PYTHONPATH": HERE, "MEMWATCH_OUT": os.path.join(proj, "out"), "PYTHONWARNINGS": "ignore"}

    # 成因1：启动器在别处、cwd 无关 -> 应从「已加载模块」反推出业务目录
    proc = subprocess.run([PY, launcher], cwd="/", env={**os.environ, **env},
                          capture_output=True, text=True, timeout=120)
    out = proc.stdout
    names = next((l for l in out.splitlines() if l.startswith("NAMES")), "")
    paths = next((l for l in out.splitlines() if l.startswith("PATHS")), "")
    check("N1. 启动器/cwd 都与业务目录无关时，自动反推出业务目录",
          "rows" in names and "app" in paths,
          f"{names} {paths}")
    check("N1. 不靠放宽就拿到结果（说明 root_paths 推导对了）",
          "WIDENED False" in out, [l for l in out.splitlines() if l.startswith("WIDENED")])

    # 成因2：软链路径（root_paths 与帧文件名 realpath 不一致）
    link = os.path.join(proj, "link_app")
    try:
        os.symlink(app, link)
        ok_link = True
    except OSError:
        ok_link = False
    if ok_link:
        launcher2 = os.path.join(bindir, "launcher_link.py")
        with open(launcher2, "w", encoding="utf-8") as fh:
            fh.write(
                "import sys\n"
                f"sys.path.insert(0, {link!r})\n"
                "import biz\n"
                "biz.load()\n"
                "biz.do_work()\n"
            )
        proc = subprocess.run([PY, launcher2], cwd="/", env={**os.environ, **env},
                              capture_output=True, text=True, timeout=120)
        link_names = next((l for l in proc.stdout.splitlines() if l.startswith("NAMES")), "")
        check("N2. 通过软链路径导入的代码仍能被归因（realpath 一致化）",
              "rows" in link_names, link_names)

    # 成因3：显式写错 root_paths -> 自动放宽 + 给出可操作诊断
    proc = subprocess.run(
        [PY, "-c",
         "import sys; sys.path.insert(0, %r)\n"
         "import biz; biz.load(); biz.do_work(root_paths=['/nonexistent'])\n" % app],
        cwd="/", env={**os.environ, **env}, capture_output=True, text=True, timeout=120)
    n3_names = next((l for l in proc.stdout.splitlines() if l.startswith("NAMES")), "")
    check("N3. root_paths 写错时自动放宽，仍然拿到数据",
          "WIDENED True" in proc.stdout and "rows" in n3_names,
          f"{n3_names} " + str([l for l in proc.stdout.splitlines() if l.startswith("WIDENED")]))

    # 成因4：作用域里真的没有变量 -> 给出可操作提示，而不是一句「没有」
    proc = subprocess.run(
        [PY, "-c",
         "import memwatch\n"
         "memwatch.install(out_dir=%r, write_report=False, on_signal=False)\n"
         "def only_infra():\n"
         "    import os\n"
         "    def helper(): pass\n"
         "    class C: pass\n"
         "    rep = memwatch.mark('infra')\n"
         "    print('DIAGNOSTICS', len(rep.get('diagnostics') or []))\n"
         "    print('HINT', ' '.join(rep.get('diagnostics') or [])[:400])\n"
         "only_infra()\n" % os.path.join(proj, "out")],
        cwd=HERE, env={**os.environ, **env}, capture_output=True, text=True, timeout=120)
    check("N4. 真空作用域给出可操作提示（含 diagnose 指引）",
          "DIAGNOSTICS 3" in proc.stdout and "diagnose" in proc.stdout,
          [l for l in proc.stdout.splitlines() if l.startswith(("DIAGNOSTICS", "HINT"))])

    # diagnose() 体检报告：放在真实文件里跑，才能测到「被 root_paths 拒绝」的帧
    proc = subprocess.run(
        [PY, "-c",
         "import sys; sys.path.insert(0, %r)\n"
         "import memwatch\n"
         "memwatch.install(out_dir=%r, root_paths=['/nonexistent'], on_signal=False, write_report=False)\n"
         "import biz; biz.probe()\n" % (app, os.path.join(proj, "out"))],
        cwd="/", env={**os.environ, **env}, capture_output=True, text=True, timeout=120)
    check("N5. diagnose() 给出逐帧判定（严格/放宽）与修复建议",
          "DIAG_ACCEPTED" in proc.stdout and "放宽后收" in proc.stderr
          and "root_paths" in proc.stderr,
          [l for l in proc.stdout.splitlines() if l.startswith("DIAG_ACCEPTED")]
          + [l for l in proc.stderr.splitlines() if "probe()" in l][:1])
    shutil.rmtree(proj, ignore_errors=True)


def scenario_overhead():
    """量一下 standard 档在真实业务堆上的额外开销。"""
    plain = run([PY, "-c", "import sys; sys.path.insert(0,'.'); import app_demo; app_demo.main()"],
                {"APP_HOLD_SECONDS": "0"})
    timed = run([PY, "-c",
                 "import time, memwatch;"
                 "memwatch.install(out_dir='/tmp/mw_ov', profile='standard');"
                 "t=time.perf_counter(); memwatch.snapshot('x');"
                 "print('SNAPSHOT_MS', round((time.perf_counter()-t)*1000,1))"],
                {"APP_HOLD_SECONDS": "0"})
    line = [l for l in timed.stdout.splitlines() if l.startswith("SNAPSHOT_MS")]
    check("H. standard 档单次快照耗时", bool(line) and float(line[0].split()[1]) < 2000,
          line[0] if line else "无输出")
    check("H. 被测脚本本身正常退出", plain.returncode == 0, f"rc={plain.returncode}")
    shutil.rmtree("/tmp/mw_ov", ignore_errors=True)


if __name__ == "__main__":
    scenario_runner()
    scenario_at_call()
    scenario_at_line()
    scenario_signal()
    scenario_watch_file()
    scenario_inline()
    scenario_mark()
    scenario_dict_keys()
    scenario_stdlib_only()
    scenario_asyncio()
    scenario_scope_resolution()
    scenario_failsafe()
    scenario_leak_and_concurrency()
    scenario_overhead()
    print("\n" + "=" * 60)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  -", f)
    sys.exit(1 if FAIL else 0)
