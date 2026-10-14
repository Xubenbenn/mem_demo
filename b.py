"""被依赖的模块 b：对外暴露三种后端的 LocalProxy。"""
from werkzeug.local import Local, LocalProxy, LocalStack

_locals = Local()
_stack = LocalStack()

payload_proxy = LocalProxy(_locals, "payload")
stack_proxy = _stack()  # 公共工厂同时兼容 Werkzeug 2.0 与 2.2+ 的栈顶代理
lazy_proxy = LocalProxy(lambda: _locals.payload)


def install(payload):
    _locals.payload = payload
    _stack.push(payload)
    return payload


def uninstall():
    _stack.pop()
    try:
        del _locals.payload
    except AttributeError:
        pass
