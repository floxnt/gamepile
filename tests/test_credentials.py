"""v4 credentials module tests.

Run with: uv run python tests/test_credentials.py

Covers:
  - Keyring round-trip with mocked backend (set / get / clear)
  - Read precedence: keyring > .env (project-root > data-dir)
  - Keyring-unavailable fallback to .env
  - using_env_fallback() reflects backend availability
  - Migration helper: env → keyring + idempotence flag
  - Migration scope: project-root .env never offered for migration
  - has_complete_credentials gates the first-run middleware

No pytest dependency. Mocks the keyring module to avoid touching the
real OS keychain in tests; mocks file paths to isolate from any real
.env files in the user's environment.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the module under test before any patch decorators below need to
# refer to its name.
from app import credentials


def _reset_module_state():
    """Reset cached probe + module-level state between tests."""
    credentials._reset_probe_cache()


# ---------------------------------------------------------------------------
# Mock keyring backend — in-memory dict
# ---------------------------------------------------------------------------

class _FakeKeyring:
    """Drop-in for the real keyring module. In-memory storage; tracks
    set / get / delete calls for assertion."""

    def __init__(self):
        self.store: dict = {}
        self.set_calls: list = []
        self.get_calls: list = []
        self.delete_calls: list = []

    def get_password(self, service: str, key: str):
        self.get_calls.append((service, key))
        return self.store.get((service, key))

    def set_password(self, service: str, key: str, value: str):
        self.set_calls.append((service, key, value))
        self.store[(service, key)] = value

    def delete_password(self, service: str, key: str):
        self.delete_calls.append((service, key))
        if (service, key) in self.store:
            del self.store[(service, key)]
        else:
            # Mirrors real keyring's behavior on missing key. The exception
            # type doesn't matter for credentials.clear_credentials — that
            # function catches Exception broadly.
            raise Exception("not found")


def _patch_keyring_available():
    """Returns a context manager replacing the keyring module with a
    fresh _FakeKeyring instance and forcing the probe to return True."""
    fake = _FakeKeyring()
    return fake, patch.dict(sys.modules, {"keyring": fake})


def _patch_keyring_unavailable():
    """Force the probe to return False (keyring backend missing)."""
    return patch.object(credentials, "_probe_keyring", return_value=False)


# ---------------------------------------------------------------------------
# Keyring round-trip
# ---------------------------------------------------------------------------

def test_set_and_get_steam_api_key():
    fake, patcher = _patch_keyring_available()
    with patcher:
        _reset_module_state()
        credentials.set_steam_api_key("ABC123XYZ")
        assert credentials.get_steam_api_key() == "ABC123XYZ"
        # Stored under the expected service+key pair.
        assert (credentials.SERVICE_NAME, credentials.KEY_API_KEY) in fake.store


def test_set_and_get_steam_id():
    fake, patcher = _patch_keyring_available()
    with patcher:
        _reset_module_state()
        credentials.set_steam_id("76561198000000000")
        assert credentials.get_steam_id() == "76561198000000000"


def test_clear_credentials_removes_keyring_entries():
    # Patch env paths to nonexistent so .env fallback can't shadow the
    # post-clear None result. (The real project's .env file exists in the
    # dev environment; without patching, get_* would return that value
    # after the keyring is cleared.)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
            with patch.object(credentials, "_project_root_env", return_value=td / "nonexistent"):
                fake, patcher = _patch_keyring_available()
                with patcher:
                    _reset_module_state()
                    credentials.set_steam_api_key("API_KEY")
                    credentials.set_steam_id("STEAM_ID_VAL")
                    credentials.mark_migration_done("migrated")
                    assert credentials.get_steam_api_key() == "API_KEY"

                    credentials.clear_credentials()
                    assert credentials.get_steam_api_key() is None
                    assert credentials.get_steam_id() is None
                    assert credentials.migration_already_handled() is False


def test_set_when_keyring_unavailable_raises():
    with _patch_keyring_unavailable():
        _reset_module_state()
        try:
            credentials.set_steam_api_key("X")
        except RuntimeError as exc:
            assert "Keyring unavailable" in str(exc)
        else:
            raise AssertionError("Expected RuntimeError, none raised")


# ---------------------------------------------------------------------------
# Read precedence: keyring > .env (project-root > data-dir)
# ---------------------------------------------------------------------------

def test_keyring_value_wins_over_env():
    # Write the env files with one value; keyring with another. Keyring wins.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_env = td / "data" / "gamepile" / ".env"
        data_env.parent.mkdir(parents=True)
        data_env.write_text("STEAM_API_KEY=ENV_KEY\nSTEAM_ID=ENV_ID\n")

        with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
            with patch.object(credentials, "_project_root_env", return_value=td / "nonexistent"):
                fake, patcher = _patch_keyring_available()
                with patcher:
                    _reset_module_state()
                    credentials.set_steam_api_key("KEYRING_KEY")
                    credentials.set_steam_id("KEYRING_ID")
                    assert credentials.get_steam_api_key() == "KEYRING_KEY"
                    assert credentials.get_steam_id() == "KEYRING_ID"


def test_env_value_used_when_keyring_empty():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_env = td / "data" / "gamepile" / ".env"
        data_env.parent.mkdir(parents=True)
        data_env.write_text("STEAM_API_KEY=ENV_KEY\nSTEAM_ID=ENV_ID\n")

        with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
            with patch.object(credentials, "_project_root_env", return_value=td / "nonexistent"):
                fake, patcher = _patch_keyring_available()
                with patcher:
                    _reset_module_state()
                    # Keyring available but empty → falls through to env.
                    assert credentials.get_steam_api_key() == "ENV_KEY"
                    assert credentials.get_steam_id() == "ENV_ID"


def test_project_root_env_wins_over_data_dir_env():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        proj = td / "project.env"
        proj.write_text("STEAM_API_KEY=PROJ_KEY\nSTEAM_ID=PROJ_ID\n")
        data_env = td / "data" / "gamepile" / ".env"
        data_env.parent.mkdir(parents=True)
        data_env.write_text("STEAM_API_KEY=DATA_KEY\nSTEAM_ID=DATA_ID\n")

        with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
            with patch.object(credentials, "_project_root_env", return_value=proj):
                with _patch_keyring_unavailable():
                    _reset_module_state()
                    assert credentials.get_steam_api_key() == "PROJ_KEY"
                    assert credentials.get_steam_id() == "PROJ_ID"


# ---------------------------------------------------------------------------
# Keyring unavailable → .env fallback
# ---------------------------------------------------------------------------

def test_keyring_unavailable_uses_env_fallback():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_env = td / "data" / "gamepile" / ".env"
        data_env.parent.mkdir(parents=True)
        data_env.write_text("STEAM_API_KEY=FALLBACK_KEY\nSTEAM_ID=FALLBACK_ID\n")

        with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
            with patch.object(credentials, "_project_root_env", return_value=td / "nonexistent"):
                with _patch_keyring_unavailable():
                    _reset_module_state()
                    assert credentials.get_steam_api_key() == "FALLBACK_KEY"
                    assert credentials.get_steam_id() == "FALLBACK_ID"
                    assert credentials.using_env_fallback() is True


def test_using_env_fallback_false_when_keyring_works():
    fake, patcher = _patch_keyring_available()
    with patcher:
        _reset_module_state()
        # Even with .env values present, fallback is False because keyring works.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            data_env = td / "data" / "gamepile" / ".env"
            data_env.parent.mkdir(parents=True)
            data_env.write_text("STEAM_API_KEY=X\nSTEAM_ID=Y\n")
            with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
                with patch.object(credentials, "_project_root_env", return_value=td / "nonexistent"):
                    assert credentials.using_env_fallback() is False


def test_using_env_fallback_false_when_no_env_and_no_keyring():
    with _patch_keyring_unavailable():
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
                with patch.object(credentials, "_project_root_env", return_value=td / "nonexistent"):
                    _reset_module_state()
                    # Genuinely no creds anywhere — not "fallback", just "not configured".
                    assert credentials.using_env_fallback() is False


# ---------------------------------------------------------------------------
# has_complete_credentials — gates the first-run middleware
# ---------------------------------------------------------------------------

def test_has_complete_requires_both_credentials():
    fake, patcher = _patch_keyring_available()
    with patcher:
        _reset_module_state()
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
                with patch.object(credentials, "_project_root_env", return_value=td / "nonexistent"):
                    assert credentials.has_complete_credentials() is False
                    credentials.set_steam_api_key("X")
                    assert credentials.has_complete_credentials() is False  # still missing steam_id
                    credentials.set_steam_id("Y")
                    assert credentials.has_complete_credentials() is True


# ---------------------------------------------------------------------------
# Migration scope + idempotence
# ---------------------------------------------------------------------------

def test_migration_target_is_data_dir_env_only():
    """Project-root .env never triggers migration — it's the dev workspace."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        proj = td / "project.env"
        proj.write_text("STEAM_API_KEY=K\nSTEAM_ID=S\n")

        with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
            with patch.object(credentials, "_project_root_env", return_value=proj):
                fake, patcher = _patch_keyring_available()
                with patcher:
                    _reset_module_state()
                    # Project-root .env exists → no migration offer.
                    assert credentials.dotenv_path_for_migration() is None


def test_migration_target_data_dir_only():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_env = td / "data" / "gamepile" / ".env"
        data_env.parent.mkdir(parents=True)
        data_env.write_text("STEAM_API_KEY=K\nSTEAM_ID=S\n")

        with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
            with patch.object(credentials, "_project_root_env", return_value=td / "nonexistent"):
                fake, patcher = _patch_keyring_available()
                with patcher:
                    _reset_module_state()
                    assert credentials.dotenv_path_for_migration() == data_env


def test_migrate_env_to_keyring_copies_values():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_env = td / "data" / "gamepile" / ".env"
        data_env.parent.mkdir(parents=True)
        data_env.write_text("STEAM_API_KEY=ENV_K\nSTEAM_ID=ENV_S\n")

        with patch.object(credentials, "_data_dir_env", return_value=td / "data" / "gamepile" / ".env"):
            with patch.object(credentials, "_project_root_env", return_value=td / "nonexistent"):
                fake, patcher = _patch_keyring_available()
                with patcher:
                    _reset_module_state()
                    assert credentials.migrate_env_to_keyring() is True
                    assert credentials.get_steam_api_key() == "ENV_K"
                    assert credentials.get_steam_id() == "ENV_S"


def test_migration_idempotence_via_marker():
    fake, patcher = _patch_keyring_available()
    with patcher:
        _reset_module_state()
        assert credentials.migration_already_handled() is False
        credentials.mark_migration_done("migrated")
        assert credentials.migration_already_handled() is True
        # Second call doesn't unflip.
        credentials.mark_migration_done("migrated")
        assert credentials.migration_already_handled() is True


def test_mark_migration_done_records_action_and_timestamp():
    fake, patcher = _patch_keyring_available()
    with patcher:
        _reset_module_state()
        credentials.mark_migration_done("declined")
        stored = fake.store[(credentials.SERVICE_NAME, credentials.KEY_MIGRATION_DONE)]
        assert stored.startswith("declined:")
        # Timestamp format is ISO; just verify it parses.
        from datetime import datetime
        datetime.fromisoformat(stored.split(":", 1)[1])


def test_migration_when_keyring_unavailable_returns_false():
    with _patch_keyring_unavailable():
        _reset_module_state()
        assert credentials.migrate_env_to_keyring() is False
        # And mark_migration_done is no-op (no keyring to write to).
        credentials.mark_migration_done("migrated")
        assert credentials.migration_already_handled() is False


# ---------------------------------------------------------------------------
# Probe caching
# ---------------------------------------------------------------------------

def test_probe_result_is_cached():
    """Repeated keyring_available() calls must not re-probe — backend
    doesn't change mid-process; re-probing is wasted work + would
    introduce subtle ordering dependencies in tests."""
    call_count = [0]
    def counting_probe():
        call_count[0] += 1
        return True
    with patch.object(credentials, "_probe_keyring", side_effect=counting_probe):
        _reset_module_state()
        for _ in range(5):
            assert credentials.keyring_available() is True
        assert call_count[0] == 1


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
        finally:
            _reset_module_state()
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print(f"\nAll {len(test_funcs)} tests passed.")


if __name__ == "__main__":
    main()
