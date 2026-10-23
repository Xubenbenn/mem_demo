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


if __name__ == "__main__":
    unittest.main()
