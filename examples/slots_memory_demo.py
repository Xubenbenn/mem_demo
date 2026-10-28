"""How much memory does replacing a @dataclass with a plain __slots__ class save?

    python examples/slots_memory_demo.py                 # 1,000,000 objects per variant
    python examples/slots_memory_demo.py -n 200000

Each variant runs in its own fresh interpreter so the RSS numbers do not affect each other:

    dataclass          @dataclass, instances keep a per-object __dict__
    dataclass+dict     same class, but something touched obj.__dict__ (vars(), pickle, copy, ...)
    slots              plain class with __slots__ and a hand-written __init__/__repr__/__eq__

Two numbers per variant, each from its own run:
    objects   exact bytes of the record objects (tracemalloc); the list that holds them and the
              field values are excluded, since they cost the same in every variant
    RSS +     how much the process RSS grew while building them, measured in a run without
              tracemalloc (tracemalloc's own bookkeeping would inflate RSS several times over)
"""
import argparse
import json
import os
import subprocess
import sys
import tracemalloc
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mempeak  # noqa: E402  (only for the RSS readings)


@dataclass
class ItemDC:
    order_id: int
    sku: str
    qty: int
    price: float
    status: str


class ItemSlots:
    __slots__ = ("order_id", "sku", "qty", "price", "status")

    def __init__(self, order_id, sku, qty, price, status):
        self.order_id = order_id
        self.sku = sku
        self.qty = qty
        self.price = price
        self.status = status

    def __repr__(self):
        return "ItemSlots(order_id=%r, sku=%r, qty=%r, price=%r, status=%r)" % (
            self.order_id, self.sku, self.qty, self.price, self.status)

    def __eq__(self, other):
        if other.__class__ is not self.__class__:
            return NotImplemented
        return ((self.order_id, self.sku, self.qty, self.price, self.status) ==
                (other.order_id, other.sku, other.qty, other.price, other.status))

    __hash__ = None          # same as @dataclass(eq=True): mutable, so unhashable


VARIANTS = {"dataclass": ItemDC, "dataclass+dict": ItemDC, "slots": ItemSlots}


def build(variant, n, trace):
    cls = VARIANTS[variant]
    # Field values are shared objects, so only the record objects themselves differ.
    skus = ["SKU-%04d" % i for i in range(1000)]
    prices = [i / 100 for i in range(1000)]
    statuses = ["new", "paid", "shipped"]
    objs = [None] * n

    if trace:
        tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0] if trace else mempeak.rss_bytes()
    for i in range(n):
        objs[i] = cls(i % 256, skus[i % 1000], i % 10, prices[i % 1000], statuses[i % 3])
    if variant == "dataclass+dict":
        for o in objs:
            o.__dict__      # what vars(o), pickle, copy.copy(o) etc. do internally
    if trace:
        return tracemalloc.get_traced_memory()[0] - before
    after = mempeak.rss_bytes()
    return None if before is None or after is None else after - before


def mib(b):
    return "n/a" if b is None else "%.1f MiB" % (b / 1024.0 / 1024.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1000000, help="objects per variant")
    ap.add_argument("--child", choices=sorted(VARIANTS), help=argparse.SUPPRESS)
    ap.add_argument("--trace", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.child:
        print(json.dumps(build(args.child, args.n, args.trace)))
        return

    def child(variant, trace):
        cmd = [sys.executable, os.path.abspath(__file__), "-n", str(args.n), "--child", variant]
        out = subprocess.run(cmd + (["--trace"] if trace else []),
                             check=True, capture_output=True, text=True)
        return json.loads(out.stdout)

    res = {v: (child(v, True), child(v, False)) for v in VARIANTS}

    print("Python %s, %s objects with 5 fields\n" % (sys.version.split()[0], format(args.n, ",")))
    print("%-16s %12s %12s %12s" % ("variant", "objects", "per object", "RSS +"))
    for v, (objects, rss) in res.items():
        print("%-16s %12s %10d B %12s" % (v, mib(objects), objects // args.n, mib(rss)))
    slots = res["slots"][0]
    print()
    for v in ("dataclass", "dataclass+dict"):
        dc = res[v][0]
        print("slots vs %-16s saves %s (%.0f%% of the object memory)"
              % (v + ":", mib(dc - slots), 100.0 * (dc - slots) / dc))


if __name__ == "__main__":
    main()
