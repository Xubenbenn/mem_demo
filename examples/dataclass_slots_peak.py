"""Peak memory of the same job with a @dataclass vs the same dataclass with __slots__.

    python examples/dataclass_slots_peak.py                 # runs every variant under mempeak
    python examples/dataclass_slots_peak.py -n 500000

Or measure one variant by hand, exactly as you would your own job:

    python -m mempeak run --out dc.json    -- python examples/dataclass_slots_peak.py --variant dataclass
    python -m mempeak run --out slots.json -- python examples/dataclass_slots_peak.py --variant slots
    python -m mempeak compare dc.json slots.json

Variants (the classes are identical apart from the slots):

    dataclass     @dataclass
    slots         @dataclass with a hand-written __slots__ (works on 3.9; fields cannot have defaults)
    slots=True    @dataclass(slots=True) (Python 3.10+, skipped on older versions)

The job peaks while two full generations of records are alive at once: load N records, then
build an updated copy of each with dataclasses.replace(), then aggregate.
"""
import argparse
import dataclasses
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mempeak  # noqa: E402


@dataclass
class ItemDC:
    order_id: int
    sku: str
    qty: int
    price: float
    status: str


@dataclass
class ItemSlots:
    __slots__ = ("order_id", "sku", "qty", "price", "status")
    order_id: int
    sku: str
    qty: int
    price: float
    status: str


VARIANTS = {"dataclass": ItemDC, "slots": ItemSlots}
if sys.version_info >= (3, 10):
    @dataclass(slots=True)
    class ItemSlotsParam:
        order_id: int
        sku: str
        qty: int
        price: float
        status: str

    VARIANTS["slots=True"] = ItemSlotsParam


def job(cls, n):
    skus = ["SKU-%04d" % i for i in range(1000)]
    prices = [i / 100 for i in range(1000)]
    with mempeak.phase("load"):
        items = [cls(i, skus[i % 1000], i % 10, prices[i % 1000], "new") for i in range(n)]
    with mempeak.phase("transform"):
        paid = [dataclasses.replace(it, status="paid") for it in items]     # peak: both lists alive
        del items
    with mempeak.phase("aggregate"):
        return sum(it.qty * it.price for it in paid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1000000, help="records per generation")
    ap.add_argument("--variant", choices=sorted(VARIANTS), help="run one variant (no comparison)")
    args = ap.parse_args()

    if args.variant:
        job(VARIANTS[args.variant], args.n)
        return

    tmp = tempfile.mkdtemp(prefix="dcslots-")
    reports = {}
    for v in VARIANTS:
        out = os.path.join(tmp, v.replace("=", "_") + ".json")
        subprocess.run([sys.executable, "-m", "mempeak", "run", "-q", "--out", out, "--",
                        sys.executable, os.path.abspath(__file__), "-n", str(args.n), "--variant", v],
                       check=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        reports[v] = mempeak.load(out)

    print("Python %s, %s records x 2 generations\n" % (sys.version.split()[0], format(args.n, ",")))
    print("%-12s %14s   %s" % ("variant", "peak RSS", "phase at peak"))
    base = reports["dataclass"].peak_bytes
    for v, r in reports.items():
        peak = r.peak_bytes
        diff = "" if v == "dataclass" else "   %+.0f%% vs dataclass" % (100.0 * (peak - base) / base)
        print("%-12s %10.1f MiB   %-13s%s" % (v, peak / 2.0 ** 20, r["peak"].get("phase_at_peak"), diff))
    print("\nreports: %s  (python -m mempeak compare A.json B.json for details)" % tmp)


if __name__ == "__main__":
    main()
