# slots_audit：扫描 `__slots__` 整改的风险点

给出目录和整改了的类名，全局扫描所有 `.py` 文件，找出 `__slots__` 化之后**不报错但行为/内存效果会变**的地方，以及会抛异常的地方。只依赖标准库，基于 AST（不是 grep），能识别 try/except 包裹、变量类型注解、继承链。

```bash
python slots_audit.py src/ tests/ --classes Order,OrderItem            # 只看这些类受到的影响（默认）
python slots_audit.py src/ tests/ -c Order --all                       # 另外列出对象类型无法确定的通用命中
python slots_audit.py src/ tests/ -c Order --all --related-only        # 同上，但跳过从未提到整改类的文件
python slots_audit.py src/ -c Order --also .venv/lib/python3.9/site-packages/somelib   # 额外扫第三方库
python slots_audit.py src/ -c Order --python 3.9 --json audit.json     # 按目标版本判断；JSON 里 shown 字段标记是否显示
python slots_audit.py src/ -c Order --slots-decorator my_slots          # 你自己的、作用同 add_slots 的装饰器
```

## 它做了什么

1. **类定义检查**：`--classes` 里的类及其父类、子类（扫描范围内任意层级）是否真的 slots 化、写法在目标版本上是否成立。这部分天然只针对你列出的类。
2. **用法检查**：找出对象上依赖 `__dict__` / 弱引用 / 动态属性的代码，并**推断对象的类型**。只有能推断出对象就是整改类（或其子类）的命中才算“影响”，按类分组输出；推断不出类型的命中只计数，`--all` 才列出。

类型推断来源（都在扫描范围内按名字解析）：

| 来源 | 例子 | 标记 |
| --- | --- | --- |
| 参数/变量注解 | `def f(o: Order)`、`o: Optional[Order] = ...` | confirmed |
| 构造、拷贝 | `o = Order(...)`、`copy.copy(o)`、`replace(o, ...)` | confirmed |
| 类方法里的 `self` | 整改类及子类的方法 | confirmed |
| `isinstance` 收窄 | `if isinstance(x, Order):` 分支内、`assert isinstance(x, Order)` 之后 | confirmed |
| 容器元素 | `orders: List[Order]` 后的 `for o in orders`、`enumerate`、`.values()`、`.items()`、推导式、`orders[0]` | confirmed |
| 函数返回注解 | `def find() -> Optional[Order]` 后的 `o = find()`、`for o in all_orders()` | inferred |
| 属性注解 | 某类里 `order: Order` / `self.order: Order` 后的 `x.order` | inferred |

`inferred` 按函数名/属性名全项目匹配：同名函数或属性只要有一处注解不同，就不使用该推断。注解为**无关具体类型**（如 `c: Customer`、`s: str`）的对象，相关命中直接丢弃；注解为整改类的父类、`Any`、TypeVar 或扫描范围外的类型时仍算“类型不确定”。

想让某处通用命中被归到正确的类下：给那个变量、参数或返回值加上类型注解即可。

退出码：显示出来的结果里有 SILENT 时为 1，可直接放进 CI。默认跳过 `.git`、`.venv`、`venv`、`site-packages`、`build` 等目录，`--exclude 目录名` 追加。

## 分级

| 级别 | 含义 |
| --- | --- |
| **SILENT** | 不报错，但行为或内存效果改变——测试很可能发现不了，**必须人工看** |
| **LOUD** | 执行到会抛异常；测试只有覆盖到那一行才能发现，清单可用来补测试 |
| **INFO** | 人工确认：旧 pickle、扫描范围外的父类、弱引用容器 |

可信度：`confirmed` / `inferred` 见上表；`possible` = 通用模式命中、类型未知（默认不显示）；`possible (file does not mention ...)` = 该文件从未提到整改类及其子类，多半无关。

## 检查项

| 代码 | 级别 | 检查内容 |
| --- | --- | --- |
| S01 | SILENT | 列出的类本身并没有 slots 化（漏加，或 `__slots__` 里含 `'__dict__'`） |
| S02 | SILENT | 父类没有 slots → 实例仍带 `__dict__`，优化失效 |
| S03 | SILENT | 子类（扫描范围内，任意层级）没有 slots → 子类实例仍带 `__dict__` |
| A01 | SILENT | `getattr(o, "__dict__", 默认)`、`hasattr(o, "__dict__"/"__weakref__")`、被 try/suppress 吞掉的 `o.__dict__` / `vars(o)` |
| A02 | SILENT | 在 `except AttributeError` / `except Exception` / 裸 except / `suppress(...)` 里的属性赋值或 `setattr`——赋值被静默吞掉 |
| A03 | SILENT | 弱引用失败被吞掉 → 常见是改存强引用，**对象释放不掉** |
| A04 | SILENT | `Cls.field = ...`、`mock.patch.object(Cls, "field")`、`patch("m.Cls.field")`、`setattr(Cls, "field", ...)`：替换 slot 描述符，已存在实例读到类上的值 |
| A05 | SILENT | 重建类的方法里使用 `__class__`，指向旧类，`isinstance` 静默为 False：`dataclass(slots=True)` 在 3.10–3.12 及 3.13 早期补丁版本上；`@add_slots` 在所有版本上 |
| A06 | SILENT | 写在 `@add_slots` / `@dataclass(slots=True)` **下面**的其他装饰器（如注册器）先执行，拿到的是被丢弃的旧类 |
| A07 | SILENT | 父类定义了 `__init_subclass__`：重建类时执行两次，第一次传入旧类 |
| L01 | LOUD | 给整改类实例设置未声明的属性 |
| L02 | LOUD | `o.__dict__` / `vars(o)` / `getattr(o, "__dict__")` |
| L03 | LOUD | `weakref.ref/proxy/finalize`，而类没有 `__weakref__` 槽（dataclass 用 `weakref_slot=True`） |
| L04 | LOUD | `__slots__` 与 `dataclass(slots=True)` 重复；手写 `__slots__` 的字段带默认值 |
| L05 | LOUD | `functools.cached_property`（需要 `__dict__`） |
| L06 | LOUD | 重建类里无参 `super()` 抛 TypeError：`dataclass(slots=True)` 在 3.10–3.12 及 3.13 早期补丁版本上；`@add_slots` 在所有版本上 |
| L07 | LOUD | 目标版本不支持：`dataclass(slots=True)` 需要 3.10+，`weakref_slot=True` 需要 3.11+ |
| I00 | INFO | 没找到类定义（拼写，或不在扫描目录） |
| I01 | INFO | `pickle/joblib/dill/torch.load` 等：整改前保存的 pickle 加载会失败 |
| I02 | INFO | 父类在扫描范围外，无法确认 |
| I03 | INFO | `WeakValueDictionary/WeakKeyDictionary/WeakSet`：放入的对象必须支持弱引用 |

支持的类定义形式：手写 `__slots__`、`@dataclass(slots=True)`（含 `weakref_slot`）、`@add_slots`（含 `weakref=True`；
自定义名字用 `--slots-decorator`）、`attrs` 的 `@define/@frozen/@mutable` 与 `@attr.s(slots=True)`。

## Python 3.9

3.9 的 `dataclass` 没有 `slots` 参数（L07）。可选写法：

```python
@dataclass
class Order:
    __slots__ = ("id", "name", "tags")   # 需要弱引用时加 "__weakref__"
    id: int
    name: str
    tags: list                           # 字段不能有默认值 / field(...)，否则类定义时 ValueError（L04）
```

需要默认值时有两种选择：

- 仓库里的 `add_slots.py`：在 `@dataclass` 之后重建一个带 `__slots__` 的类，相当于 3.10+ 的 `slots=True`。
  默认值、`default_factory`、`__post_init__`、`frozen`、`is_dataclass/fields/asdict/replace` 都照常；
  frozen 类的 copy/pickle、整改前保存的 pickle 都能正常加载。
  因为重建了类，L06/A05/A06/A07 在**所有版本**上都适用。

  ```python
  @add_slots                 # 必须写在 @dataclass 上面；其他装饰器也写在它上面
  @dataclass
  class Order:
      id: int
      tags: list = field(default_factory=list)
  ```

- `attrs` 的 `@define`（默认 slots，支持默认值，可用于 3.9）。

手写 `__slots__` 不会重建类，所以 L06/A05/A06/A07 不适用；其余检查项（继承链、`__dict__`、弱引用、patch 等）在 3.9 上同样成立。

版本实测：`super()` / `__class__` 问题在 3.10.20、3.11、3.12、3.13.0 上复现，3.13.16 已修复。

## 局限

- 静态分析，不是完整的类型检查器：没有注解、经过 `**kwargs`、`getattr` 动态取值、跨多层未注解函数传递的对象推断不出来，默认视图会漏掉它们——项目注解越完整，结果越准。不放心时用 `--all` 过一遍通用命中。
- 按类名（不含模块）匹配；不同模块同名类会合并处理。
- 第三方库只在 `--also` 指定时扫描，且只做用法检查。
- 只看 `.py` 源码；C 扩展、`exec`/字符串拼出的属性名、Cython 不在范围内。
- 运行时再加一道保险：对每个整改类断言 `assert not hasattr(obj, "__dict__")`，并用 `python -m mempeak` 对比前后峰值（见 README_MEMPEAK.md）。
