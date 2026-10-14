"""零改动注入入口：把这个目录放进 PYTHONPATH 即可，业务代码一行不改。

    PYTHONPATH=/path/to/mem_demo MEMWATCH_OUT=/tmp/mw python app.py

触发方式（都不需要改业务代码）：
    kill -USR1 <pid>                     立刻抓一次
    touch $MEMWATCH_WATCH_FILE           出现该文件就抓一次
    MEMWATCH_AT=app.py:handle_request    到点自动抓（调用级，开销小）
    MEMWATCH_AT=app.py:123               到点自动抓（行级，慢，仅短流程用）
    MEMWATCH_RSS_MB=500                  越过 500MB 台阶自动抓
    MEMWATCH_INTERVAL=30                 每 30 秒抓一次
"""

import os
import sys as _sys


def _chain_load_other_sitecustomize():
    """如果别的目录里还有 sitecustomize.py，先把它跑掉，避免被我们挡住。"""
    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    for entry in list(_sys.path):
        if not entry:
            continue
        try:
            if os.path.abspath(entry) == here:
                continue
            candidate = os.path.join(entry, "sitecustomize.py")
            if not os.path.isfile(candidate):
                continue
            spec = importlib.util.spec_from_file_location(
                "_memwatch_chained_sitecustomize", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            continue


if os.environ.get("MEMWATCH_AUTO", "1") not in ("0", "false", "False"):
    _chain_load_other_sitecustomize()
    try:
        import memwatch

        memwatch.install()
    except Exception:
        # 探针永远不能影响被测进程启动
        pass
