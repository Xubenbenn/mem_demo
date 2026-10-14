"""一行/装饰器接入示例：业务代码里只需要出现 `import memwatch` 和几个调用点。

适合「我不介意在几个关键节点加一行」的项目；完全不想动代码就用 app_demo.py + PYTHONPATH。
"""

import os
import time

import b
import memwatch

CACHE = {}

memwatch.install(
    out_dir=os.environ.get("MEMWATCH_OUT", "./memwatch-reports"),
    profile="standard",          # counters / standard / deep
    label_prefix="inline_",
)


@memwatch.watch("build_payload")          # 方式二：装饰器，进出各抓一次
def build_payload(mb=4):
    payload = [bytearray(1024 * 1024) for _ in range(mb)]
    b.install(payload)
    return payload


def main():
    with memwatch.node("cache_fill"):      # 方式一：上下文管理器
        CACHE["d"] = {f"k{i}": "v" * 128 for i in range(3000)}

    memwatch.track("global_cache", CACHE)  # 关注点登记：不靠帧/模块也能进报告
    build_payload(4)

    memwatch.snapshot("关键节点", profile="deep")   # 方式三：一行调用，要最全的

    time.sleep(0.05)
    b.uninstall()
    CACHE.clear()
    memwatch.snapshot("清理后", profile="standard")
    print("inline 模式结束")


if __name__ == "__main__":
    main()
