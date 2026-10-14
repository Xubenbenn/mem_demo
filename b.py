"""被依赖的模块 b：对外只暴露 LocalProxy，不暴露真实对象。"""

from werkzeug.local import Local, LocalProxy, LocalStack

_locals = Local()
_stack = LocalStack()

# 两种典型写法：属性后端 与 callable 后端（后者会带 __wrapped__）
payload_proxy = LocalProxy(_locals, "payload")
stack_proxy = LocalProxy(_stack, "top")
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
