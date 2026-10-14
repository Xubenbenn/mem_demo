"""模块 A：通过 b 暴露的 LocalProxy 间接持有数据。"""

import b

# A 自己不知道真实对象在哪，只拿到一个 proxy 对象
proxy = b.payload_proxy
lazy = b.lazy_proxy
local_ref = None


def bind(mb: int = 8):
    """模拟业务节点：数据装进 b 的 Local，A 只留下 proxy。"""
    global local_ref
    local_ref = b.install([bytearray(1024 * 1024) for _ in range(mb)])
    return proxy
