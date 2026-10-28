"""Regression tests for slots_audit (static __slots__ risk scanner)."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

import slots_audit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FILES = {
    "app/models.py": 'from dataclasses import dataclass, field\nfrom functools import cached_property\n\n\nclass Plain:\n    pass\n\n\n@dataclass(slots=True)\nclass Order:\n    id: int\n    name: str\n    tags: list = field(default_factory=list)\n\n    def clone(self):\n        return isinstance(self, __class__)\n\n    def label(self):\n        return super().__repr__()\n\n    def mark(self):\n        self.seen = True          # undeclared -> AttributeError\n\n    @cached_property\n    def total(self):\n        return 1\n\n\n@dataclass\nclass VipOrder(Order):        # subclass not slotted -> silent\n    level: int = 0\n\n\n@dataclass(slots=True)\nclass GoodChild(Order):\n    level: int = 0\n\n\n@dataclass(slots=True)\nclass Item(Plain):            # base without slots -> silent\n    sku: str\n\n\nclass Manual:\n    __slots__ = ("a",)\n    a: int = 0                # default conflicts with slot -> ValueError\n',
    "app/service.py": 'import contextlib\nimport pickle\nimport weakref\nfrom app.models import Order, Item\n\n_registry = []\n\n\ndef to_dict(o):\n    return getattr(o, "__dict__", {})\n\n\ndef describe(o: Order):\n    if hasattr(o, "__dict__"):\n        return dict(o.__dict__)\n    return {}\n\n\ndef remember(o):\n    try:\n        _registry.append(weakref.ref(o))\n    except TypeError:\n        _registry.append(o)\n\n\ndef tag(order: Order):\n    try:\n        order.flag = 1\n    except AttributeError:\n        pass\n    with contextlib.suppress(AttributeError):\n        setattr(order, "other", 2)\n\n\ndef safe(order: Order):\n    order.name = "x"           # declared field: fine\n    return vars(order)\n\n\ndef build():\n    o = Order(1, "a")\n    o.extra = 3                # loud\n    w = weakref.ref(o)         # loud\n    Order.name = "default"     # silent: replaces the slot descriptor\n    return w\n\n\ndef load(p):\n    with open(p, "rb") as f:\n        return pickle.load(f)\n',
    "app/unrelated.py": 'def dump(x):\n    return x.__dict__\n',
    "tests/test_x.py": 'from unittest import mock\nfrom app.models import Order\n\n\ndef test_patch():\n    with mock.patch.object(Order, "name", "p"):\n        pass\n    with mock.patch("app.models.Order.name", "p"):\n        pass\n    with mock.patch.object(Order, "label", lambda self: "x"):   # method: fine\n        pass\n',
}


class SlotsAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        for rel, src in FILES.items():
            path = os.path.join(cls.tmp.name, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
        cwd = os.getcwd()
        os.chdir(cls.tmp.name)
        try:
            cls.findings, cls.stats = slots_audit.audit(["app", "tests"], ["Order", "Item", "Manual"],
                                                          python=(3, 12))
        finally:
            os.chdir(cwd)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def hits(self, code, path=None):
        return [(f["path"].replace(os.sep, "/"), f["line"]) for f in self.findings
                if f["code"] == code and (path is None or f["path"].replace(os.sep, "/") == path)]

    def line_of(self, path, needle):
        for i, line in enumerate(FILES[path].splitlines(), 1):
            if needle in line:
                return i
        raise AssertionError(needle)

    def test_inheritance_problems_are_silent(self):
        self.assertEqual(self.hits("S03"), [("app/models.py", self.line_of("app/models.py", "class VipOrder"))])
        self.assertEqual(self.hits("S02"), [("app/models.py", self.line_of("app/models.py", "class Item("))])
        self.assertEqual(self.stats["subclasses"], ["GoodChild", "VipOrder"])

    def test_silent_usage_patterns(self):
        svc = "app/service.py"
        a01 = self.hits("A01", svc)
        self.assertIn((svc, self.line_of(svc, 'getattr(o, "__dict__", {})')), a01)
        self.assertIn((svc, self.line_of(svc, 'hasattr(o, "__dict__")')), a01)
        self.assertEqual(self.hits("A03"), [(svc, self.line_of(svc, "_registry.append(weakref.ref(o))"))])
        self.assertEqual(len(self.hits("A02", svc)), 2)
        a04 = self.hits("A04")
        self.assertIn((svc, self.line_of(svc, 'Order.name = "default"')), a04)
        self.assertEqual(len([h for h in a04 if h[0] == "tests/test_x.py"]), 2)  # method patch not flagged

    def test_loud_patterns(self):
        svc, mod = "app/service.py", "app/models.py"
        self.assertIn((svc, self.line_of(svc, "o.extra = 3")), self.hits("L01"))
        self.assertIn((mod, self.line_of(mod, "self.seen = True")), self.hits("L01"))
        self.assertIn((svc, self.line_of(svc, "w = weakref.ref(o)")), self.hits("L03"))
        self.assertIn((mod, self.line_of(mod, "a: int = 0")), self.hits("L04"))
        self.assertTrue(self.hits("L05"))
        self.assertTrue(self.hits("L06"))

    def test_declared_fields_are_not_flagged(self):
        svc = "app/service.py"
        flagged = {f["line"] for f in self.findings if f["path"].replace(os.sep, "/") == svc}
        self.assertNotIn(self.line_of(svc, 'order.name = "x"'), flagged)
        self.assertFalse([f for f in self.findings if "GoodChild" in f["message"]])

    def test_unrelated_files_are_marked(self):
        f = [f for f in self.findings if f["path"].replace(os.sep, "/") == "app/unrelated.py"]
        self.assertEqual(len(f), 1)
        self.assertTrue(f[0]["confidence"].startswith("possible ("))

    def test_python39_target(self):
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            findings, stats = slots_audit.audit(["app"], ["Order"], python=(3, 9))
        finally:
            os.chdir(cwd)
        codes = [f["code"] for f in findings]
        self.assertEqual(stats["python"], "3.9")
        self.assertIn("L07", codes)                      # dataclass(slots=True) does not exist on 3.9
        self.assertNotIn("L06", codes)
        self.assertNotIn("A05", codes)
        self.assertNotIn("L07", [f["code"] for f in self.findings])   # the 3.12 run

    def test_cli_exit_code_and_missing_class(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "slots_audit.py"), "app", "-c", "Nope",
                            "--related-only"],
                           cwd=self.tmp.name, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("I00", r.stdout)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "slots_audit.py"), "app", "tests", "-c", "Order"],
                           cwd=self.tmp.name, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)



SHOP = {"shop/models.py": 'from dataclasses import dataclass\nfrom typing import Dict, List, Optional\n\n\n@dataclass\nclass Order:                      # the only converted class\n    __slots__ = ("id", "items")\n    id: int\n    items: list\n\n\n@dataclass\nclass Customer:                   # untouched\n    name: str\n\n\n@dataclass\nclass Cart:\n    order: Order                  # attribute annotated -> cart.order is an Order\n    history: List[Order]\n    by_id: Dict[int, Order]\n    owner: Customer\n\n\ndef find_order(oid) -> Optional[Order]:\n    return None\n\n\ndef all_orders() -> List[Order]:\n    return []\n', "shop/service.py": 'import weakref\nfrom shop.models import Cart, Customer, all_orders, find_order\n\n_seen = weakref.WeakSet()\n\n\ndef dump(obj):                     # generic helper: type unknown\n    return getattr(obj, "__dict__", {})\n\n\ndef touch_customer(c: Customer):\n    try:\n        c.vip = True               # Customer is not converted: must NOT be reported\n    except AttributeError:\n        pass\n\n\ndef via_return(oid):\n    o = find_order(oid)            # return annotation\n    try:\n        o.cached = 1\n    except AttributeError:\n        pass\n\n\ndef via_loop():\n    for o in all_orders():         # List[Order] return\n        o.note = "x"\n    return [vars(x) for x in all_orders()]\n\n\ndef via_attr(cart: Cart):\n    weakref.ref(cart.order)        # attribute annotation\n    for i, o in enumerate(cart.history):\n        o.flag = 1\n    for k, o in cart.by_id.items():\n        hasattr(o, "__dict__")\n\n\ndef via_isinstance(x):\n    from shop.models import Order\n    if isinstance(x, Order):\n        return x.__dict__\n    return x.__dict__              # outside the branch: unknown\n'}


class TypeInferenceTests(unittest.TestCase):
    """Only the converted class's impacts are attributed to it; other classes stay out."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        for rel, src in SHOP.items():
            path = os.path.join(cls.tmp.name, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
        cwd = os.getcwd()
        os.chdir(cls.tmp.name)
        try:
            cls.findings, cls.stats = slots_audit.audit(["shop"], ["Order"], python=(3, 9))
        finally:
            os.chdir(cwd)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def specific_lines(self):
        return {(f["code"], f["line"]) for f in slots_audit.select(self.findings)}

    def line_of(self, needle):
        for i, line in enumerate(SHOP["shop/service.py"].splitlines(), 1):
            if needle in line:
                return i
        raise AssertionError(needle)

    def test_each_inference_path_is_attributed_to_order(self):
        got = self.specific_lines()
        expected = {("A02", self.line_of("o.cached = 1")),             # return annotation
                    ("L01", self.line_of('o.note = "x"')),             # List[Order] return + for loop
                    ("L02", self.line_of("vars(x) for x")),            # comprehension
                    ("L03", self.line_of("weakref.ref(cart.order)")),  # attribute annotation
                    ("L01", self.line_of("o.flag = 1")),               # enumerate(List[Order] attribute)
                    ("A01", self.line_of('hasattr(o, "__dict__")')),   # Dict[int, Order].items()
                    ("L02", self.line_of("return x.__dict__"))}         # isinstance narrowing
        self.assertEqual(expected - got, set())
        for f in slots_audit.select(self.findings):
            self.assertEqual(f["target"], "Order")

    def test_unrelated_and_unknown_objects_are_not_attributed(self):
        got = self.specific_lines()
        self.assertNotIn(self.line_of("c.vip = True"), {line for _, line in got})
        self.assertNotIn(self.line_of('getattr(obj, "__dict__", {})'), {line for _, line in got})
        self.assertNotIn(self.line_of("outside the branch"), {line for _, line in got})
        # annotated with an unrelated class: dropped entirely, even from --all
        self.assertFalse([f for f in self.findings if f["line"] == self.line_of("c.vip = True")])
        # unknown type: kept as a generic finding for --all
        self.assertTrue([f for f in self.findings if f["line"] == self.line_of("outside the branch")])

    def test_default_text_groups_by_class_and_counts_hidden(self):
        text = slots_audit.format_text(self.findings, self.stats)
        self.assertIn("### Order", text)
        self.assertIn("--all", text)
        self.assertNotIn("getattr(obj", text)
        self.assertIn("getattr(obj", slots_audit.format_text(self.findings, self.stats, show_all=True))

DECO = {"app/models.py": 'from dataclasses import dataclass, field\nfrom add_slots import add_slots\nfrom app.reg import register, slotify\n\n\nclass Registered:\n    def __init_subclass__(cls, **kw):\n        pass\n\n\n@add_slots\n@dataclass\nclass Order:\n    id: int\n    tags: list = field(default_factory=list)\n\n    def label(self):\n        return super().__repr__()\n\n    def same(self, o):\n        return isinstance(o, __class__)\n\n\n@dataclass\nclass VipOrder(Order):\n    level: int = 0\n\n\n@add_slots(weakref=True)\n@register\n@dataclass\nclass Item(Registered):\n    sku: str = ""\n\n\n@slotify\n@dataclass\nclass Custom:\n    a: int = 0\n', "app/service.py": 'import weakref\nfrom app.models import Item, Order\n\n\ndef tag(o: Order):\n    o.extra = 1\n\n\ndef keep(i: Item):\n    return weakref.ref(i)\n'}


class AddSlotsDecoratorTests(unittest.TestCase):
    """@add_slots (and --slots-decorator names) count as slotted and get the class re-creation rules."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        for rel, src in DECO.items():
            path = os.path.join(cls.tmp.name, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_audit(self, classes, python, **kw):
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            return slots_audit.audit(["app"], classes, python=python, **kw)[0]
        finally:
            os.chdir(cwd)

    def codes(self, findings, target):
        return sorted(f["code"] for f in findings if f["target"] == target)

    def line_of(self, needle):
        for i, line in enumerate(DECO["app/models.py"].splitlines(), 1):
            if needle in line:
                return i
        raise AssertionError(needle)

    def test_add_slots_is_slotted_and_recreation_rules_apply_on_39(self):
        findings = self.run_audit(["Order", "Item"], (3, 9))
        self.assertEqual(self.codes(findings, "Order"), ["A05", "L01", "L06", "S03"])  # no S01
        a06 = [f for f in findings if f["code"] == "A06"]
        self.assertEqual([(f["target"], f["line"]) for f in a06], [("Item", self.line_of("@register"))])
        self.assertEqual([f["target"] for f in findings if f["code"] == "A07"], ["Item"])
        self.assertNotIn("L03", self.codes(findings, "Item"))          # add_slots(weakref=True)

    def test_custom_decorator_name(self):
        self.assertIn("S01", self.codes(self.run_audit(["Custom"], (3, 9)), "Custom"))
        self.assertNotIn("S01", self.codes(self.run_audit(["Custom"], (3, 9), slots_decorators=["slotify"]),
                                           "Custom"))
        r = subprocess.run([sys.executable, os.path.join(ROOT, "slots_audit.py"), "app", "-c", "Custom",
                            "--slots-decorator", "@slotify", "--python", "3.9"],
                           cwd=self.tmp.name, capture_output=True, text=True)
        self.assertNotIn("S01", r.stdout)


if __name__ == "__main__":
    unittest.main()
