"""Tests for add_slots (dataclass __slots__ on Python 3.9)."""
import copy
import inspect
import dataclasses
import pickle
import unittest
import weakref
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace

from add_slots import add_slots


@add_slots
@dataclass
class Inner:
    v: int = 1


@add_slots
@dataclass
class Item:
    sku: str
    qty: int = 1
    inner: Inner = field(default_factory=Inner)

    def __post_init__(self):
        self.qty = max(self.qty, 0)


@add_slots
@dataclass(frozen=True)
class Key:
    a: int
    b: str = "x"


@add_slots(weakref=True)
@dataclass
class Ref:
    a: int = 0


@add_slots
@dataclass
class Child(Item):
    level: int = 0


@add_slots
@dataclass
class Totals:
    id: int
    total: int = field(default=0, init=False)
    seen: list = field(default_factory=list, init=False)

    def __post_init__(self):
        self.total += 1


@add_slots
@dataclass
class Sup:
    x: int = 0

    def name(self):
        return super().__repr__()


class AddSlotsTests(unittest.TestCase):
    def test_dataclass_behaviour_is_kept(self):
        a = Item("A", -3)
        self.assertFalse(hasattr(a, "__dict__"))
        self.assertTrue(is_dataclass(Item) and is_dataclass(a))
        self.assertEqual(a.qty, 0)                                   # __post_init__ ran
        self.assertIsNot(Item("B").inner, Item("C").inner)           # default_factory
        self.assertEqual([f.default for f in fields(Item)][1], 1)
        self.assertEqual(asdict(a), {"sku": "A", "qty": 0, "inner": {"v": 1}})
        self.assertEqual(replace(a, qty=2), Item("A", 2))
        with self.assertRaises(AttributeError):
            a.extra = 1

    def test_frozen_copy_and_pickle(self):
        k = Key(1)
        self.assertEqual({k: 1}[Key(1)], 1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            k.a = 2
        for clone in (copy.copy(k), copy.deepcopy(k), pickle.loads(pickle.dumps(k))):
            self.assertEqual(clone, k)

    def test_pickles_written_before_slots_still_load(self):
        # an old pickle carries the instance __dict__ as its state
        for cls, state, expected in ((Key, {"a": 1, "b": "y"}, Key(1, "y")),
                                     (Item, {"sku": "A", "qty": 2, "inner": Inner()}, Item("A", 2))):
            obj = cls.__new__(cls)
            obj.__setstate__(state)
            self.assertEqual(obj, expected)

    def test_weakref_option(self):
        r = Ref()
        self.assertIs(weakref.ref(r)(), r)
        with self.assertRaises(TypeError):
            weakref.ref(Item("A"))

    def test_subclass_does_not_duplicate_base_slots(self):
        self.assertEqual(Child.__slots__, ("level",))
        c = Child("A", level=2)
        self.assertFalse(hasattr(c, "__dict__"))
        self.assertEqual(pickle.loads(pickle.dumps(c)), c)

    def test_init_false_fields_with_defaults_are_set(self):
        t = Totals(1)
        self.assertEqual((t.total, t.seen), (1, []))            # default set before __post_init__
        self.assertEqual(repr(t), "Totals(id=1, total=1, seen=[])")
        self.assertEqual(str(inspect.signature(Totals)), "(id: int) -> None")

    def test_misuse_is_rejected(self):
        with self.assertRaises(TypeError):
            add_slots(type("NotDC", (), {}))
        with self.assertRaises(TypeError):
            @add_slots
            @dataclass
            class Twice:
                __slots__ = ("a",)
                a: int

    def test_known_limitation_zero_arg_super(self):
        with self.assertRaises(TypeError):        # slots_audit reports this as L06
            Sup().name()


if __name__ == "__main__":
    unittest.main()
