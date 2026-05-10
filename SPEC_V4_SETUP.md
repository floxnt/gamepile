# v4 Setup Wizard + Keychain Credential Storage

## Purpose

Required for friend-distribution per the v5 path. Replaces the
developer-only `.env`-editing onboarding with a first-run wizard a
non-developer can complete: get credentials, validate them against
Steam, store them in the OS keychain, kick off the initial library
sync. Plus a settings page for later changes and a migration path
for existing `.env`-based installs.

## Credential precedence

Read precedence (`app.credentials`):

1. **OS keychain** (when available) — `keyring` library: Windows
   Credential Manager / macOS Keychain / Linux Secret Service.
   Service name `"gamepile"`, keys `"steam_api_key"` / `"steam_id"`.
2. **`.env` file** — `dotenv_values()` reads directly from the file
   rather than relying on `os.environ` (avoids test pollution).
   - Project-root `.env` (next to `pyproject.toml`) — the dev workspace.
   - Data-dir `.env` (`~/.local/share/gamepile/.env`) — the legacy
     bundled-binary location.
   - Project-root wins over data-dir.

Write precedence: keyring only. The `.env` file is a read-only
fallback; we never write to it. A failed keyring write surfaces an
error to the user (in the wizard or on /settings) — the right
escalation is for the user to fix their keyring backend, not for us
to silently leak secrets to a `.env` file they didn't ask for.

`keyring_available()` probes once at first call (no-op `get_password`
catching all exceptions) and caches the result. The single-process
desktop app means the backend doesn't change mid-run.

## First-run detection

FastAPI middleware `first_run_redirect` in `app/main.py` checks
`credentials.has_complete_credentials()` on every request. Missing or
incomplete creds → 303 redirect to `/setup/welcome`.

Whitelist (always allowed regardless of credential state):

- `/setup/*` — the wizard itself
- `/static/*` — CSS / HTMX assets
- `/healthz` — server liveness probe used by `app.main.run()` before
  launching the webview
- `/refresh*` — the wizard's done page kicks off the existing
  refresh code path, then polls `/refresh/status` (and the
  setup-specific `/setup/sync-status`) until completion

## Wizard flow

```
/setup/welcome
     │
     ├─► /setup/migrate  (only when data-dir .env exists, keyring
     │     │              available, and migration not yet handled)
     │     ├─► [Migrate] → migrate_env_to_keyring()
     │     │              + mark_migration_done("migrated")
     │     │              + redirect /setup/done (creds populated)
     │     └─► [Skip]   → mark_migration_done("declined")
     │                    + redirect /setup/api-key
     │
     └─► /setup/api-key      ─► /setup/steam-id  ─► /setup/validate
              ▲                       ▲                  │
              └─── /setup/edit ◄──────┴── (failure) ◄────┘
                                                          │
                                                          ▼
                                                     /setup/done
                                                     (kicks off sync,
                                                      polls progress,
                                                      redirects /shortlist)
```

### Welcome page

Sets expectations: explains what's needed (API key + SteamID), what
the keychain is for, and that GamePile is local-only with no
telemetry. Single "Get started" button.

### Migrate page (conditional)

Only renders when:
- `dotenv_path_for_migration()` returns a path (data-dir `.env`
  exists with both credentials AND project-root `.env` does NOT exist)
- `keyring_available()` is True
- `migration_already_handled()` is False

Two buttons: **Migrate to keychain** (recommended) writes the values
into keyring and skips ahead to `/setup/done` (creds already populated).
**Keep .env** marks the prompt declined and proceeds through the
normal API-key + SteamID flow.

Either choice writes a `migration_done` keyring entry with the format
`"<action>:<ISO timestamp>"` so subsequent launches don't re-prompt.

### API key page

External link to `steamcommunity.com/dev/apikey` and a single text
input. Empty input → inline error.

### SteamID page

Accepts:
- 17-digit numeric SteamID64 (no resolution needed)
- Bare vanity name (`ike`)
- Full vanity URL (`steamcommunity.com/id/ike`)
- Full profile URL (`steamcommunity.com/profiles/76561198…`)

URL chrome stripped inline (`https://`, `www.`, `steamcommunity.com/`,
`/profiles/` or `/id/` prefix). Bare/extracted vanity names resolved
via the new `resolve_vanity_url()` fetcher hitting
`ISteamUser/ResolveVanityURL`.

### Validate page

Runs a test `GetOwnedGames` call with the in-flight credentials.
On success, persists via `set_steam_api_key` / `set_steam_id` and
redirects to `/setup/done`.

On failure, populates `validation_error` on the in-flight session and
redirects to `/setup/edit`.

### Edit page (validation-failure landing)

Combined edit page showing both fields with current values and the
validation error inline. User can fix whichever credential is wrong
without losing the other. Save re-runs vanity resolution +
validation; loops back to the edit page on persistent failure;
proceeds to `/setup/done` on success.

Steam's API errors don't always discriminate which credential is
wrong (a 401 could be either). Showing both fields together is
honest about that ambiguity.

### Done page

Renders `setup_done.html` immediately. Inline HTMX trigger POSTs
`/setup/start-sync`, which schedules `sync_module.run_refresh()` as
an `asyncio.create_task` background job and returns the first
sync-status fragment.

The fragment self-polls every 2s via `hx-trigger="load delay:2s"`
on `/setup/sync-status` until `progress.completed_at` is non-None.
At that point the fragment returns a `hx-trigger="load delay:1s"`
redirect to `/shortlist`, replacing the body.

Initial sync of a 600-game library runs ~10 minutes; user can leave
the window and come back. Progress fragment shows current phase,
game name + count, and a progress bar.

## In-flight wizard state

Module-level singleton dict in `app/routes/setup.py`:

```python
_setup_session = {
    "api_key": None,
    "steam_id": None,
    "validation_error": None,
}
```

Single-user desktop app, single window — this is safe and avoids
the FastAPI session-middleware overhead just to hold two strings
across page transitions. Cleared after successful keyring write.

## Settings page

`/settings` route. Per-credential edit-in-place forms. Save POST
handlers re-validate against `GetOwnedGames` before persisting;
failure renders inline without overwriting the stored value.

API key displayed as `·` × (length − 4) + last 4 chars (e.g.
`····················MNOP`) so the user can verify which key
they're looking at without exposing the full value to a
shoulder-surfer.

SteamID displayed verbatim — it's a public identifier, no privacy
concern.

`.env`-fallback banner surfaces at the top when
`using_env_fallback()` returns True (keyring unavailable AND `.env`
supplies values). Banner explains the situation and notes that
saving from the page won't work until a keychain is available.

Backend-status footer at the bottom: "Storage backend: OS keychain"
or ".env file fallback".

Reusing `_resolve_steam_id_input` and `_validate_credentials` from
`app.routes.setup` keeps the wizard and the settings page on a
single source of truth for resolution and validation behavior.

## Migration

Triggered when:
- `keyring_available()` → True
- Data-dir `.env` exists with both credentials
- Project-root `.env` does NOT exist (project-root is the dev
  workspace and never a migration target)
- `migration_already_handled()` → False

The migration page surfaces between Welcome and the credential-
entry pages, only when triggered. User picks Migrate or Keep .env;
either choice marks the prompt resolved.

After migration, the `.env` file remains in place as a backup, but
the read precedence (keyring first) means GamePile reads from the
keychain going forward. We don't delete the `.env` — keeps the user
in control and provides a fallback if they ever clear their
keychain.

## Tests

`tests/test_credentials.py` — 18 tests covering:
- Keyring round-trip with mocked backend (set / get / clear)
- Read precedence (keyring > env, project-root > data-dir)
- Keyring-unavailable fallback to `.env`
- `using_env_fallback()` reflects backend availability
- `has_complete_credentials()` gates the middleware
- Migration scope (project-root never offered) + idempotence
- Probe caching

Wizard routes + middleware are smoke-tested via FastAPI TestClient
during commit-time verification (not in the assertion-based test
suite — they exercise async state and would benefit from a proper
integration test harness, deferred).

## Cross-platform

The `keyring` library handles backend differences via abstraction.
Tests use a mocked backend; real-world Linux Secret Service is
exercised on the dev machine. Windows Credential Manager and macOS
Keychain backends get exercised during v5 friend-testing iteration
per the v4 brief.

## Done criteria

- `app/credentials.py` exposes the public API with keyring-first +
  `.env` fallback semantics
- `app/config.py` no longer eagerly raises on missing creds
- Existing fetchers (`steam.py`, `steam_achievements.py`) call
  credential accessors instead of importing module-level constants
- First-run middleware redirects unconfigured launches to
  `/setup/welcome`; whitelisted paths flow through
- Wizard renders all five pages, validates credentials inline,
  persists to keyring on success, and kicks off the initial library
  sync
- Migration page surfaces only when applicable; choice persists
  via `migration_done` keyring marker
- Settings page renders current credentials with API-key masking,
  per-credential edit + re-validate, and `.env`-fallback banner
- Existing dev install with project-root `.env` keeps working
  unchanged — migration is offered only for data-dir `.env`
- All 18 credential tests pass; existing test suites + app boot
  verified clean

## Out of scope (deferred)

- Full integration test harness for the wizard pages (would benefit
  from pytest-asyncio + a real keyring fixture; deferred until v5
  friend-testing surfaces concrete failure modes)
- Settings page action: "Reset all credentials" (clear keyring +
  redirect to wizard). Trivial to add when the use case appears
- Per-platform install-keychain prompts on Linux when no backend
  is available (gnome-keyring isn't installed by default everywhere)
- Migration option: "Migrate AND delete .env" — current behavior
  preserves the file as backup; deletion is a v5 polish if users ask
