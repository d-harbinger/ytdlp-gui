"""Minimal zero-dependency test harness.

The project has no test framework and we don't want to add one. Each test file
imports `case` / `run`, registers checks, and is run directly:

    venv/bin/python tests/test_validation.py

`run()` prints PASS/FAIL per case and exits non-zero if any failed, so it works
as a CI-style gate without pytest.
"""
import sys
import traceback

_CASES = []


def case(fn):
    _CASES.append(fn)
    return fn


def run():
    failed = 0
    for fn in _CASES:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    total = len(_CASES)
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
