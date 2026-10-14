"""易用性示例：业务代码里只加 `memwatch.mark()` 一行。

对比 app_demo.py（零改动、但需要外部触发），这个文件展示「允许加一行代码」时
最省心的用法：不用起名字、不用配作用域、不用去翻报告文件。
"""

import time
import tracemalloc

import b
import memwatch

memwatch.install(out_dir="./memwatch-reports")   # 可选；不写也会在第一次 mark() 时自动装
tracemalloc.start(5)                             # 想拿到分配点就早开，mark(profile="deep") 才有用

CACHE = {}


def load_config():
    CACHE["cfg"] = {f"k{i}": f"{i:04d}" * 64 for i in range(2000)}
    return CACHE["cfg"]


def charge(order_id):
    order_items = [{"sku": f"s{i}", "blob": bytes(256)} for i in range(800)]
    memwatch.mark()                      # ← 一行：标签自动 = app_mark.py:行号:charge
    time.sleep(0.01)
    return len(order_items)


def main():
    load_config()
    charge("A-1001")
    CACHE["rows"] = [{"id": i, "blob": bytes(128)} for i in range(5000)]

    memwatch.mark("缓存填充后")            # ← 想给个名字就给
    memwatch.mark("全进程视角", profile="standard", roots_scope="all")  # ← 想全量扫描就加参数

    b.uninstall()
    CACHE.clear()
    memwatch.mark("清理后")
    print("app_mark 结束")


if __name__ == "__main__":
    main()
