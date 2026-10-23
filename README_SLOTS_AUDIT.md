# slots_audit：扫描 `__slots__` 整改的风险点

给出目录和整改了的类名，全局扫描所有 `.py` 文件，找出 `__slots__` 化之后**不报错但行为/内存效果会变**的地方，以及会抛异常的地方。只依赖标准库，基于 AST（不是 grep），能识别 try/except 包裹、变量类型注解、继承链。

```bash
python slots_audit.py src/ tests/ --classes Order,OrderItem
python slots_audit.py src/ tests/ -c Order --related-only          # 隐藏与整改类无关文件中的泛化命中
python slots_audit.py src/ -c Order --also .venv/lib/python3.11/site-packages/somelib   # 额外扫第三方库
python slots_audit.py src/ -c Order --json audit.json
```

退出码：有 SILENT 项时为 1，可直接放进 CI。默认跳过 `.git`、`.venv`、`venv`、`site-packages`、`build` 等目录，`--exclude 目录名` 追加。

## 分级

| 级别 | 含义 |
| --- | --- |
| **SILENT** | 不报错，但行为或内存效果改变——测试很可能发现不了，**必须人工看** |
| **LOUD** | 执行到会抛异常；测试只有覆盖到那一行才能发现，清单可用来补测试 |
| **INFO** | 人工确认：旧 pickle、扫描范围外的父类、弱引用容器 |

可信度：`confirmed` = 能确定对象是整改类（参数/变量注解为该类、由该类构造、类自身方法里的 `self`）；`possible` = 通用模式命中、类型未知；`possible (file does not mention ...)` = 该文件从未提到整改类及其子类，多半无关。

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
| A05 | SILENT | `dataclass(slots=True)` 方法里使用 `__class__`：Python 3.10–3.12 上指向旧类，`isinstance` 静默为 False |
| L01 | LOUD | 给整改类实例设置未声明的属性 |
| L02 | LOUD | `o.__dict__` / `vars(o)` / `getattr(o, "__dict__")` |
| L03 | LOUD | `weakref.ref/proxy/finalize`，而类没有 `__weakref__` 槽（dataclass 用 `weakref_slot=True`） |
| L04 | LOUD | `__slots__` 与 `dataclass(slots=True)` 重复；手写 `__slots__` 的字段带默认值 |
| L05 | LOUD | `functools.cached_property`（需要 `__dict__`） |
| L06 | LOUD | `dataclass(slots=True)` 里无参 `super()`：Python 3.10–3.12 抛 TypeError |
| I00 | INFO | 没找到类定义（拼写，或不在扫描目录） |
| I01 | INFO | `pickle/joblib/dill/torch.load` 等：整改前保存的 pickle 加载会失败 |
| I02 | INFO | 父类在扫描范围外，无法确认 |
| I03 | INFO | `WeakValueDictionary/WeakKeyDictionary/WeakSet`：放入的对象必须支持弱引用 |

支持的类定义形式：手写 `__slots__`、`@dataclass(slots=True)`（含 `weakref_slot`）、`attrs` 的 `@define/@frozen/@mutable` 与 `@attr.s(slots=True)`。

## 局限

- 静态分析，没有完整类型推断：不知道类型的对象只能按通用模式报 `possible`，会有误报；也会漏掉经过多层传递、容器、`**kwargs` 后的对象。SILENT 项建议逐条看，LOUD 项建议用测试兜底。
- 按类名（不含模块）匹配；不同模块同名类会合并处理。
- 第三方库只在 `--also` 指定时扫描，且只做用法检查。
- 只看 `.py` 源码；C 扩展、`exec`/字符串拼出的属性名、Cython 不在范围内。
- 运行时再加一道保险：对每个整改类断言 `assert not hasattr(obj, "__dict__")`，并用 `python -m mempeak` 对比前后峰值（见 README_MEMPEAK.md）。
