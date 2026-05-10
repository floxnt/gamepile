"""Credential storage for v4+ — keyring with .env fallback.

Public API:
  - get_steam_api_key() / get_steam_id() — read accessors
  - set_steam_api_key(value) / set_steam_id(value) — keyring writes
  - clear_credentials() — wipe keyring entries (settings reset, tests)
  - has_complete_credentials() — used by the first-run middleware
  - keyring_available() — cached probe result
  - using_env_fallback() — true when keyring is unavailable AND .env supplies values
  - dotenv_path_for_migration() — the data-dir .env, or None if absent
  - migration_already_handled() — checks the migration_done keyring key
  - mark_migration_done(action) — writes timestamp + action ("migrated" | "declined")
  - migrate_env_to_keyring() — the actual migration action

Read precedence:
  1. Keyring (when available)
  2. .env file (project-root → data-dir, in that order)

Write precedence:
  - Always keyring when available; .env is read-only fallback, never written.

Migration:
  - Detected when: keyring available + keyring empty for these keys +
    data-dir .env exists with credentials.
  - Project-root .env never triggers migration (it's the dev workspace).
  - Migration writes values to keyring then marks migration_done with the
    user's chosen action so subsequent launches don't re-prompt.

Threading:
  - Single-user desktop app. Module-level cache for keyring_available
    probe result is safe (one process, one window).
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

# Keyring service identifier — appears in OS credential store as the
# "service" or "application" the credentials belong to.
SERVICE_NAME = "gamepile"
KEY_API_KEY = "steam_api_key"
KEY_STEAM_ID = "steam_id"
KEY_MIGRATION_DONE = "migration_done"


# ---------------------------------------------------------------------------
# Keyring availability probe (cached)
# ---------------------------------------------------------------------------

_keyring_probe_result: Optional[bool] = None


def _probe_keyring() -> bool:
    """Return True when a working keyring backend is available.

    A no-op get_password call is the cheapest way to surface backend
    failure (no backend, secret service not running, KWallet locked,
    etc.) without writing anything. Catches every exception type rather
    than discriminating — the response is the same regardless of why
    the backend is unhappy. Result is cached at module level
    (single-process desktop app; backend doesn't change mid-run)."""
    try:
        import keyring
    except ImportError:
        return False
    try:
        keyring.get_password(SERVICE_NAME, "_probe_no_value")
    except Exception:
        return False
    return True


def keyring_available() -> bool:
    global _keyring_probe_result
    if _keyring_probe_result is None:
        _keyring_probe_result = _probe_keyring()
    return _keyring_probe_result


def _reset_probe_cache() -> None:
    """Test helper. Production code never resets — backend doesn't change."""
    global _keyring_probe_result
    _keyring_probe_result = None


# ---------------------------------------------------------------------------
# .env discovery
# ---------------------------------------------------------------------------

def _xdg_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))


def _project_root_env() -> Path:
    """Project-root .env — the dev convention. Never migrated."""
    return Path(__file__).parent.parent / ".env"


def _data_dir_env() -> Path:
    """Data-dir .env — the legacy bundled-binary location. The migration
    target when keyring is available and has no creds yet."""
    return _xdg_data_dir() / SERVICE_NAME / ".env"


def _read_env_file(path: Path) -> dict:
    """Return {KEY: value, ...} from a .env file, or empty dict if missing
    / unreadable. Uses dotenv_values rather than os.environ so we read
    the file directly instead of the merged process environment (avoids
    test pollution)."""
    if not path.exists():
        return {}
    try:
        return {k: v for k, v in dotenv_values(path).items() if v}
    except Exception:
        return {}


def _env_value(env_key: str) -> Optional[str]:
    """Read a value from the first .env that has it. Project-root wins."""
    for path in (_project_root_env(), _data_dir_env()):
        values = _read_env_file(path)
        if env_key in values:
            return values[env_key]
    return None


def dotenv_path_for_migration() -> Optional[Path]:
    """Return the data-dir .env path IF it exists with credentials and
    project-root .env doesn't (project-root is the dev workspace and is
    never a migration target). None means no migration is offered."""
    if _project_root_env().exists():
        return None
    p = _data_dir_env()
    if not p.exists():
        return None
    values = _read_env_file(p)
    if "STEAM_API_KEY" in values and "STEAM_ID" in values:
        return p
    return None


# ---------------------------------------------------------------------------
# Read accessors
# ---------------------------------------------------------------------------

def get_steam_api_key() -> Optional[str]:
    return _read_credential(KEY_API_KEY, "STEAM_API_KEY")


def get_steam_id() -> Optional[str]:
    return _read_credential(KEY_STEAM_ID, "STEAM_ID")


def _read_credential(keyring_key: str, env_key: str) -> Optional[str]:
    """Keyring first (when available), .env second. None when neither has it."""
    if keyring_available():
        try:
            import keyring
            value = keyring.get_password(SERVICE_NAME, keyring_key)
            if value:
                return value
        except Exception:
            # Defensive: unlikely now that the probe passed, but if a read
            # fails mid-session, fall through to .env rather than crashing.
            pass
    return _env_value(env_key)


def has_complete_credentials() -> bool:
    """True iff BOTH credentials are available from any source. Drives the
    first-run-middleware redirect: missing-or-incomplete → setup wizard."""
    return bool(get_steam_api_key()) and bool(get_steam_id())


def using_env_fallback() -> bool:
    """True when we're reading creds from .env because keyring isn't
    available. Drives the warning banner on /settings ('your credentials
    aren't in the OS keychain — install gnome-keyring / KWallet for
    secure storage'). False when keyring works, regardless of whether
    a .env happens to also exist."""
    if keyring_available():
        return False
    return bool(_env_value("STEAM_API_KEY")) and bool(_env_value("STEAM_ID"))


# ---------------------------------------------------------------------------
# Write accessors (keyring only — .env is never written)
# ---------------------------------------------------------------------------

def set_steam_api_key(value: str) -> None:
    _write_credential(KEY_API_KEY, value)


def set_steam_id(value: str) -> None:
    _write_credential(KEY_STEAM_ID, value)


def _write_credential(keyring_key: str, value: str) -> None:
    """Persist via keyring. Raises RuntimeError if keyring unavailable —
    callers should check keyring_available() before attempting writes
    (the wizard does; the settings page does)."""
    if not keyring_available():
        raise RuntimeError(
            "Keyring unavailable; cannot write credentials. "
            "Use .env file as a temporary workaround."
        )
    import keyring
    keyring.set_password(SERVICE_NAME, keyring_key, value)


def clear_credentials() -> None:
    """Remove stored credentials from keyring. Used by tests and a
    future settings-reset action. Silently no-ops on entries that
    don't exist (keyring backends raise PasswordDeleteError when the
    key is absent — caught with the broad except along with any other
    backend-specific error type)."""
    if not keyring_available():
        return
    import keyring
    for key in (KEY_API_KEY, KEY_STEAM_ID, KEY_MIGRATION_DONE):
        try:
            keyring.delete_password(SERVICE_NAME, key)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migration_already_handled() -> bool:
    """True when the migration prompt has already been answered (either
    migrated or declined). Drives the wizard middleware: skip the
    /setup/migrate step on subsequent launches."""
    if not keyring_available():
        return False
    try:
        import keyring
        return bool(keyring.get_password(SERVICE_NAME, KEY_MIGRATION_DONE))
    except Exception:
        return False


def mark_migration_done(action: str) -> None:
    """Record that the migration prompt was answered. action is
    'migrated' or 'declined' — stored alongside an ISO timestamp so the
    user (or a future debug session) can see what happened.

    No-op when keyring is unavailable; the prompt only fires when
    keyring is available, so we never reach this with an unavailable
    backend in normal flow."""
    if not keyring_available():
        return
    import keyring
    timestamp = datetime.utcnow().isoformat()
    keyring.set_password(SERVICE_NAME, KEY_MIGRATION_DONE, f"{action}:{timestamp}")


def migrate_env_to_keyring() -> bool:
    """Copy credentials from data-dir .env into keyring. Returns True
    on success, False when the env file doesn't have both credentials
    or the write fails. Does NOT mark migration_done — caller decides
    when to mark (covers the 'user clicked migrate' case)."""
    if not keyring_available():
        return False
    path = dotenv_path_for_migration()
    if path is None:
        return False
    values = _read_env_file(path)
    api_key = values.get("STEAM_API_KEY")
    steam_id = values.get("STEAM_ID")
    if not api_key or not steam_id:
        return False
    try:
        set_steam_api_key(api_key)
        set_steam_id(steam_id)
    except Exception:
        return False
    return True
