"""Demo target for mempeak: the same job, before and after a memory optimisation.

    python -m mempeak run --out before.json -- python examples/peak_demo.py --mode before
    python -m mempeak run --out after.json  -- python examples/peak_demo.py --mode after
    python -m mempeak compare before.json after.json

The phase markers are optional; delete them and everything still works.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mempeak  # noqa: E402  (only needed for the optional phase markers)

CHUNK = 1024 * 1024


def load(n_mb, mode):
    if mode == "before":
        return [bytearray(CHUNK) for _ in range(n_mb)]          # everything resident at once
    return (bytearray(CHUNK) for _ in range(n_mb))              # streamed one chunk at a time


def transform(rows, mode):
    if mode == "before":
        copies = [bytes(r) for r in rows]                       # second full copy
        return sum(len(c) for c in copies)
    return sum(len(r) for r in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["before", "after"], default="before")
    ap.add_argument("--mb", type=int, default=200)
    args = ap.parse_args()
    with mempeak.phase("load"):
        rows = load(args.mb, args.mode)
    with mempeak.phase("transform"):
        total = transform(rows, args.mode)
    del rows
    print("processed %d MiB" % (total // CHUNK))


if __name__ == "__main__":
    main()
