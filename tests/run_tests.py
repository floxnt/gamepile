"""Aggregate test runner. Discovers every tests/test_*.py file and runs
each via its own main(), aggregating pass/fail across all suites.

Usage: uv run python tests/run_tests.py

Exit code: 0 if all suites pass, 1 if any suite fails (or errors).
Used by CI on Linux to gate the PyInstaller build step.
"""

import importlib.util
import sys
import traceback
from pathlib import Path


TESTS_DIR = Path(__file__).parent


def _discover_test_modules() -> list[Path]:
    """Every tests/test_*.py file in alpha order (excluding this runner)."""
    return sorted(TESTS_DIR.glob("test_*.py"))


def _run_module(path: Path) -> bool:
    """Import the module and call its main(). Returns True on pass.
    Captures SystemExit (test runners call sys.exit(1) on failure)."""
    name = f"_test_suite_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        print(f"\n--- {path.name}: import failed ---")
        traceback.print_exc()
        return False

    main_fn = getattr(module, "main", None)
    if main_fn is None:
        print(f"\n--- {path.name}: no main() — skipping ---")
        return True

    try:
        main_fn()
        return True
    except SystemExit as exc:
        # Test files call sys.exit(1) on failure; treat that as a failed suite.
        return exc.code in (0, None)
    except Exception:
        print(f"\n--- {path.name}: unhandled exception ---")
        traceback.print_exc()
        return False


def main() -> None:
    modules = _discover_test_modules()
    if not modules:
        print("No test files found.")
        sys.exit(1)

    print(f"Running {len(modules)} test suite(s):")
    failed: list[str] = []
    for path in modules:
        print(f"\n=== {path.name} ===")
        ok = _run_module(path)
        if not ok:
            failed.append(path.name)

    print("\n" + "=" * 60)
    if failed:
        print(f"FAILED suites ({len(failed)}/{len(modules)}):")
        for name in failed:
            print(f"  - {name}")
        sys.exit(1)
    print(f"All {len(modules)} suite(s) passed.")


if __name__ == "__main__":
    main()
