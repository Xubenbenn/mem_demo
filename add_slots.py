"""add_slots: give an existing @dataclass __slots__ on Python 3.9 (what slots=True does on 3.10+).

    from add_slots import add_slots

    @add_slots                    # must sit ABOVE @dataclass (it runs after it)
    @dataclass
    class Item:
        sku: str
        qty: int = 1
        tags: list = field(default_factory=list)

    @add_slots(weakref=True)      # keep weakref.ref(obj) working
    @dataclass(frozen=True)
    class Key:
        a: int

Defaults, default_factory, __post_init__, frozen, is_dataclass/fields/asdict/replace keep working.
The class is re-created, so inside its methods zero-arg super() raises TypeError and __class__
names the discarded class, on every Python version; decorators applied before it and
__init_subclass__ hooks also see that discarded class. Run slots_audit to find those.
"""
from dataclasses import fields, is_dataclass

__all__ = ["add_slots"]


def _slot_names(cls):
    slots = cls.__dict__.get("__slots__", ())
    return (slots,) if isinstance(slots, str) else tuple(slots)


def _setstate(self, state):
    # copy/pickle of a slots object hand over (dict_state, slot_state); pickles written before the
    # class had slots hand over the old instance __dict__. object.__setattr__ also works when frozen.
    if isinstance(state, tuple) and len(state) == 2:
        merged = dict(state[0] or {})
        merged.update(state[1] or {})
        state = merged
    for name, value in state.items():
        object.__setattr__(self, name, value)


def add_slots(cls=None, *, weakref=False):
    def wrap(cls):
        if not is_dataclass(cls):
            raise TypeError("add_slots must be applied on top of @dataclass")
        if "__slots__" in cls.__dict__:
            raise TypeError("%s already defines __slots__" % cls.__name__)
        inherited = set()
        for base in cls.__mro__[1:-1]:
            inherited.update(_slot_names(base))
        names = tuple(f.name for f in fields(cls) if f.name not in inherited)
        if weakref and "__weakref__" not in inherited:
            names += ("__weakref__",)
        d = dict(cls.__dict__)
        d["__slots__"] = names
        for name in names:
            d.pop(name, None)       # class-level defaults; the generated __init__ already has them
        d.pop("__dict__", None)
        d.pop("__weakref__", None)
        d.setdefault("__setstate__", _setstate)
        return type(cls)(cls.__name__, cls.__bases__, d)

    return wrap if cls is None else wrap(cls)
