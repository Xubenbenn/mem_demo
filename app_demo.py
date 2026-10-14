"""模拟正式业务脚本：**完全不 import memwatch**，用于验证零改动接入。

场景与你的描述一致：A（本模块）依赖 b 模块定义的 LocalProxy 拿数据。
"""

import os
import time

import b

CACHE = {}


def load_config():
    CACHE["cfg"] = {f"k{i}": "v" * 256 for i in range(2000)}
    return CACHE["cfg"]


def build_payload(mb=8):
    """业务真实占内存的地方。"""
    payload = [bytearray(1024 * 1024) for _ in range(mb)]
    b.install(payload)
    CACHE["rows"] = [{"id": i, "name": f"u{i}", "blob": bytes(128)} for i in range(5000)]
    return payload


def finish_request():
    """检查点：此刻想看「内存都花在哪些变量上」。"""
    observed = len(b.payload_proxy)      # 通过 b 的 LocalProxy 访问
    time.sleep(0.05)
    return observed


def cleanup():
    b.uninstall()
    CACHE.clear()


def main():
    load_config()
    build_payload(8)
    observed = finish_request()
    print(f"业务输出: payload 元素 = {observed}")
    time.sleep(float(os.environ.get("APP_HOLD_SECONDS", "0.3")))  # 模拟业务处理耗时
    cleanup()
    time.sleep(0.1)
    print("业务结束")


if __name__ == "__main__":
    main()
