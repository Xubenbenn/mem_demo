"""Isolated synthetic checkpoint benchmark; NOT a production latency guarantee.

    python benchmarks/checkpoint_overhead.py --objects 1000 --trials 2 --trace \
        --output /tmp/checkpoints.json

Each sample runs in a fresh subprocess. JSON records end-to-end capture time,
workload construction time, RSS and incremental process high-water mark. The
high-water change is NOT a precise measurement of temporary probe allocations.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


def peak_rss_bytes():
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    except (ImportError, AttributeError, OSError):
        return None


def child(args):
    sys.path.insert(0, str(args.module_dir.resolve()))
    import memwatch as mw
    # Warm optional psutil import before the measured workload and tracer start.
    mw._phase_counters(dict(mw.DEFAULT))
    mw.install(trace=args.trace, on_signal=False, profile=args.profile,
               roots_scope="tracked", count_objects=False, write_report=False,
               print_report=False, brief=False, include_threads=False,
               include_asyncio=False, max_seconds=args.max_seconds, preview=False)
    try:
        started = time.perf_counter()
        payload = [bytearray(args.payload_bytes) for _ in range(args.objects)]
        build_ms = (time.perf_counter() - started) * 1000
        mw.track("payload", payload)
        before = mw._phase_counters(dict(mw.DEFAULT))
        peak_before = peak_rss_bytes()
        started = time.perf_counter()
        report = mw.snapshot("benchmark")
        wall_ms = (time.perf_counter() - started) * 1000
        after = mw._phase_counters(dict(mw.DEFAULT))
        peak_after = peak_rss_bytes()
        return {
            "version": mw.__version__, "profile": args.profile, "trace": args.trace,
            "objects_requested": args.objects, "payload_bytes_each": args.payload_bytes,
            "payload_bytes_total": args.objects * args.payload_bytes,
            "max_seconds": args.max_seconds, "workload_build_ms": round(build_ms, 3),
            "snapshot_wall_ms": round(wall_ms, 3), "capture_ms": report.get("elapsed_ms"),
            "rss_before_bytes": before.get("rss_bytes"), "rss_after_bytes": after.get("rss_bytes"),
            "rss_source": after.get("rss_source"),
            "checkpoint_peak_rss_increase_bytes": max(0, peak_after - peak_before)
                if peak_before is not None and peak_after is not None else None,
            "accounted_bytes": report.get("totals", {}).get("accounted_bytes"),
            "objects_observed": report.get("totals", {}).get("objects"),
            "graph_complete": report.get("coverage", {}).get("graph", {}).get("complete"),
            "attribution_complete": report.get("coverage", {}).get("attribution", {}).get("complete"),
            "census_complete": report.get("census_meta", {}).get("complete_for_scope"),
            "sites_status": report.get("sites", {}).get("status"),
            "budget_overrun": report.get("budget_overrun"),
            "errors": report.get("errors", []),
        }
    finally:
        mw.uninstall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objects", type=int, default=1000)
    parser.add_argument("--payload-bytes", type=int, default=256)
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=5)
    parser.add_argument("--profile", choices=("counters", "standard", "deep"))
    parser.add_argument("--module-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.objects < 0 or args.payload_bytes < 0 or args.trials < 1:
        parser.error("objects/payload-bytes must be nonnegative; trials must be positive")
    import math
    if not math.isfinite(args.max_seconds) or args.max_seconds <= 0:
        parser.error("max-seconds must be finite and positive")
    if args.child:
        print(json.dumps(child(args), allow_nan=False))
        return
    samples = []
    for profile in ([args.profile] if args.profile else ["counters", "standard", "deep"]):
        for trial in range(args.trials):
            command = [sys.executable, str(Path(__file__).resolve()), "--child", "--profile", profile,
                       "--objects", str(args.objects), "--payload-bytes", str(args.payload_bytes),
                       "--max-seconds", str(args.max_seconds), "--module-dir", str(args.module_dir)]
            if args.trace:
                command.append("--trace")
            result = subprocess.run(command, text=True, capture_output=True,
                                    env=dict(os.environ, MEMWATCH_AUTO="0"), timeout=max(60, args.max_seconds * 4))
            if result.returncode:
                raise RuntimeError("benchmark child failed: " + result.stderr)
            sample = json.loads(result.stdout)
            sample["trial"] = trial + 1
            samples.append(sample)
    output = {
        "schema_version": 1, "python": platform.python_version(), "platform": platform.platform(),
        "workload": "list of distinct bytearrays; tracked roots only; no report file I/O or sink",
        "limits": "synthetic, isolated processes; not a business benchmark; peak-RSS change is not exact probe peak",
        "samples": samples,
    }
    text = json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
