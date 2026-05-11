"""Path-resolution audit tests.

Run with: uv run python tests/test_paths.py

Verifies that DATA_DIR + DB_PATH resolve correctly across platforms via
platformdirs, and that the Linux-only legacy migration path is properly
guarded by sys.platform check.

No pytest dependency.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import platformdirs

from app import config


# ---------------------------------------------------------------------------
# Platformdirs resolution sanity
# ---------------------------------------------------------------------------

def test_data_dir_resolves_via_platformdirs():
    """DATA_DIR (set at import time) must match a fresh platformdirs call."""
    expected = Path(platformdirs.user_data_dir("gamepile", appauthor=False))
    assert config.DATA_DIR == expected, (
        f"DATA_DIR={config.DATA_DIR!r} but platformdirs returns {expected!r}"
    )


def test_data_dir_ends_with_gamepile():
    """Cross-platform invariant: the leaf directory is always 'gamepile'.
    Catches accidental appauthor=True / wrong app name."""
    assert config.DATA_DIR.name == "gamepile", config.DATA_DIR


def test_data_dir_exists_after_import():
    """app.config creates DATA_DIR on import (mkdir parents=True exist_ok=True).
    Verifies the side effect ran."""
    assert config.DATA_DIR.is_dir(), f"DATA_DIR not created: {config.DATA_DIR}"


def test_data_dir_writable():
    """The directory must be writable so the SQLite DB can land there."""
    probe = config.DATA_DIR / ".write_probe"
    try:
        probe.write_text("x")
        assert probe.read_text() == "x"
    finally:
        if probe.exists():
            probe.unlink()


def test_db_path_is_under_data_dir():
    assert config.DB_PATH.parent == config.DATA_DIR
    assert config.DB_PATH.name == "gamepile.db"


# ---------------------------------------------------------------------------
# Linux-only legacy migration guard
# ---------------------------------------------------------------------------

def test_legacy_migration_skipped_on_non_linux():
    """_migrate_legacy_data must early-return on Windows / macOS regardless
    of whether the target dir exists. Belt-and-suspenders: legacy dirs
    can't exist on those platforms, but we shouldn't even probe."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        target = td / "gamepile-fresh"  # doesn't exist
        legacy = td / "tonights-pick"
        legacy.mkdir()
        (legacy / "tonights-pick.db").write_text("legacy")

        with patch.object(sys, "platform", "win32"):
            config._migrate_legacy_data(target)
            # Target still doesn't exist; legacy untouched.
            assert not target.exists()
            assert legacy.exists()
            assert (legacy / "tonights-pick.db").exists()

        with patch.object(sys, "platform", "darwin"):
            config._migrate_legacy_data(target)
            assert not target.exists()


def test_legacy_migration_runs_on_linux():
    """When the target dir is absent and a legacy dir exists, Linux runs
    the rename. Sanity check that the guard isn't blocking the actual
    behavior we still want on Linux."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        target = td / "gamepile-fresh"
        legacy = td / "tonights-pick"
        legacy.mkdir()
        (legacy / "tonights-pick.db").write_text("legacy")

        with patch.object(sys, "platform", "linux"):
            config._migrate_legacy_data(target)
            # Legacy renamed to target; DB file renamed too.
            assert target.exists()
            assert not legacy.exists()
            assert (target / "gamepile.db").exists()
            assert (target / "gamepile.db").read_text() == "legacy"


# ---------------------------------------------------------------------------
# Credentials data-dir helper resolves via platformdirs too
# ---------------------------------------------------------------------------

def test_credentials_data_dir_env_uses_platformdirs():
    from app import credentials
    expected = Path(platformdirs.user_data_dir("gamepile", appauthor=False)) / ".env"
    assert credentials._data_dir_env() == expected


# ---------------------------------------------------------------------------
# No hardcoded Linux paths in production code
# ---------------------------------------------------------------------------

def test_no_hardcoded_xdg_in_production_modules():
    """Catch regressions that reintroduce a hardcoded Linux path-resolution
    pattern in production code. Looks for specific code constructs
    (not docstring mentions of '.local/share' which are fine for
    documenting behavior). Strips '#' comments and triple-quoted blocks
    before scanning to avoid false positives on explanatory prose."""
    import re

    forbidden_patterns = [
        r'os\.environ\.get\(["\']XDG_DATA_HOME',
        r'Path\.home\(\)\s*/\s*["\']\.local["\']',
    ]
    app_dir = Path(__file__).parent.parent / "app"
    offenders: list[tuple[Path, str]] = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text()
        # Strip triple-quoted strings (docstrings) and # comments — we're
        # looking for executable code, not explanatory text.
        text = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        text = re.sub(r"'''.*?'''", "", text, flags=re.DOTALL)
        text = re.sub(r"#.*", "", text)
        for pattern in forbidden_patterns:
            if re.search(pattern, text):
                offenders.append((path.relative_to(app_dir.parent), pattern))
    assert not offenders, (
        f"Hardcoded Linux paths in production code: {offenders}. "
        "Use platformdirs.user_data_dir instead."
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    test_funcs = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    print(f"Running {len(test_funcs)} test(s)…")
    failures = []
    for fn in test_funcs:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as exc:
            failures.append((fn.__name__, exc))
            print(f"  ✗ {fn.__name__}: {exc}")
        except Exception as exc:
            failures.append((fn.__name__, exc))
            print(f"  ✗ {fn.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print(f"\nAll {len(test_funcs)} tests passed.")


if __name__ == "__main__":
    main()
