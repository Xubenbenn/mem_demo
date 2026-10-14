"""Opt-in startup injection. Existing sitecustomize is chained once, not all files.

PYTHONPATH=/path/to/mem_demo MEMWATCH_AUTO=1 python app.py
MEMWATCH_OUT / AT / WATCH_FILE / INTERVAL / RSS_MB also opt in for compatibility.
MEMWATCH_AUTO=0 disables this probe but does not disable the application's hook.
"""
import os
import sys as _sys


def _chain_load_other_sitecustomize():
    import importlib.util
    here = os.path.realpath(__file__)
    seen = _sys.__dict__.setdefault("_memwatch_sitecustomize_seen", set())
    if here in seen:
        return
    seen.add(here)
    active = _sys.modules.get("sitecustomize")
    active_path = getattr(active, "__file__", None)
    if active_path:
        seen.add(os.path.realpath(active_path))  # do not run an already loaded hook twice
    for entry in list(_sys.path):
        candidate = os.path.realpath(os.path.join(entry or os.getcwd(), "sitecustomize.py"))
        if candidate in seen or not os.path.isfile(candidate):
            continue
        seen.add(candidate)
        try:
            spec = importlib.util.spec_from_file_location("_memwatch_chained_sitecustomize", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            print("[memwatch] chained sitecustomize failed: " + type(exc).__name__, file=_sys.stderr)
        break  # Python normally imports the first matching hook, not every file.


_chain_load_other_sitecustomize()
_auto = os.environ.get("MEMWATCH_AUTO")
_enabled = (_auto.lower() not in ("0", "false", "no")) if _auto is not None else any(
    os.environ.get(k) for k in ("MEMWATCH_OUT", "MEMWATCH_AT", "MEMWATCH_WATCH_FILE", "MEMWATCH_INTERVAL", "MEMWATCH_RSS_MB"))
if _enabled:
    try:
        import memwatch
        memwatch.install()
    except Exception as exc:
        print("[memwatch] startup probe failed: " + type(exc).__name__, file=_sys.stderr)
