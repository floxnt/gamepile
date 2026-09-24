# Reliability and restore — GamePile 1.1.0

Version 1.1.0 implements the authorized app review. It preserves the existing
navigation, Library columns, dark theme, and Steam-only desktop scope.
The installer and desktop runtime configuration are unchanged.

## User-visible changes

- Shortlist pick buttons save the selected mode, time window, and candidates.
  Notes and manual hours return a working editor after every save.
- Personal ratings now teach the recommender. Editing or clearing a rating
  replaces or removes that rating's contribution. A personal rating takes
  precedence over the game's quick status signal.
- Feedback is saved as each step is answered and remains pending until completed.
  Repeating the final submission does not add the same contribution again.
  Pick history links open a working feedback page.
- Quick decisions have Undo. Undo restores the prior status, pin, relevant pick,
  and taste signal, and refuses to overwrite edits made since that action.
  Excluded games can be restored from Game Detail. Technical issue flags can
  also be cleared there.
- “Not feeling it” skips the game temporarily. Find Games / Reset starts a new
  round and clears those skips; exiting the app also clears them. No permanent
  exclusion or negative taste signal is recorded for a temporary skip.
- Decision Sessions keep their queue, position, and recap across detail-page
  navigation. Details offers a return link; Backlog lists unfinished sessions.
  Undo last decision returns to the previous card. Sessions last until app exit
  or backup import; at most 20 are retained in memory.
- HLTB shows the matched record and title, automatic versus manual selection,
  title similarity when applicable, and the last successful fetch time.
  Similarity describes title matching, not the accuracy of estimated hours.
- HTMX is bundled locally. The local API validates loopback Host and same-origin
  requests and requires a process-specific token for writes. Native forms and
  HTMX/fetch requests supply it automatically. Request failures show a message.

## Refresh integrity

Fetched updates are applied to the current row in a write transaction instead
of writing back a stale Game object. A manual type set while a fetch is running
survives that fetch. HLTB overrides carry a revision token, so setting or resetting
an override also invalidates HLTB results already in flight.

HLTB, tags, global achievements, and personal achievements have independent
success timestamps. Skipped and failed sources do not have their clocks renewed.
Metadata keeps the existing age-based TTL; personal achievements refresh daily
or after newer Steam play activity, even for older games. An ambiguous or
incomplete owned-games response fails before ownership removal is applied.

HLTB record metadata is stored, not the service's full raw response. Existing
cached matches gain provenance on their next successful refresh.

## Taste migration

SQLite `user_version` advances from 2 to 3 once. Existing aggregate affinity is
preserved in `affinity_base`. Existing personal ratings are backfilled once into
`taste_signals`; earlier versions did not learn from those ratings.
New contributions have one source per personal rating, quick status, or pick.
Confidence counts distinct games rather than repeated clicks.

Old aggregate taste history cannot be separated into exact, reversible events:
previous versions did not record which quick actions were applied, and partial
feedback could be ambiguous. Completed historical picks are therefore archived
for taste purposes rather than replayed. Their history remains visible; new
personal ratings can be set from details. New feedback is individually editable.
This preserves learned history without inventing a reconstruction.

## Backup export and import

Settings exports schema 2 JSON with the original timestamps, 0–10 half-star
ratings, manual overrides (including dormant legacy overrides), picks, stable
pick keys, the preserved affinity baseline, and individual taste signals.
Minimal game identities are included; fetched game data and credentials are not.
The export reads one coherent database snapshot.

Import accepts schemas 1 and 2, up to 16 MB. It validates the complete file before
showing a preview. Unsupported schemas/scales, duplicate records or JSON keys,
invalid references, invalid ranges, and non-finite numbers are rejected.

The preview shows record counts, conflicts, and games missing from this
installation. Choose backup values or local values for overlapping records.
Local-only games and picks are preserved. Inferred, otherwise untouched local
state can receive imported personal data even under “keep local.” Overrides are
merged per field; old taste-baseline labels are replaced or retained, never
added together. New signals follow their corresponding game/pick conflict choice.

Missing games are retained as inactive placeholders, including games referenced
by historical alternatives. Refresh Steam to confirm current ownership and fill
in metadata. They do not enter recommendations before ownership is confirmed.
Imported HLTB IDs invalidate the old duration cache. Schema 1 taste is kept as a
legacy baseline; its completed feedback is archived against replay.

Before confirmation, the preview fingerprints the current user data. A change
since the preview requires a fresh preview. Confirmation first saves a recovery
JSON under `<data directory>/backups/`, then imports in one transaction. Failed
imports roll back. Preview tokens expire after ten minutes and are consumed only
after commit. Undo records and active review sessions reset after import.

Pick keys derive from immutable pick context and are mapped to local IDs during
import. Re-importing the same backup does not duplicate picks or learned signals.
The import is a merge, so restoring a pre-import backup does not delete records
that exist only in the current installation.

## Validation

`uv run --locked python tests/run_tests.py` runs every suite. New workflow tests
exercise actual URL-encoded/multipart requests, rendered responses, saved state,
source clocks, intervening manual overrides, editable taste, repeated feedback,
Undo conflicts, session navigation, migrations, fresh backup round trips, pick-ID
remapping, conflict policies, stale previews, malformed files, and rollback.
Tests hard-assert isolated temporary database paths. PR CI runs on Python 3.12
on Linux and Windows, separately from the tag-driven release build.

A live browser pass was attempted but the browser connection became unavailable.
No native Windows or CachyOS visual verification has been claimed. Before a
release, use a disposable data directory or a copy of an existing profile and
check these workflows in the actual packaged webviews:

1. Start offline with an existing profile; confirm navigation, local scripts,
   editors, and error messages work when enrichment/artwork is unavailable.
2. Save notes/hours twice, set and clear half-star ratings, and navigate away/back.
3. Pick in each mode, give partial feedback, resume, edit, and complete it.
4. Exclude/restore and Undo a decision; check conflict messaging after a later edit.
5. Review several backlog games, visit details, return, Undo, and inspect recap.
6. Export, choose that file in the native file picker, preview both conflict
   choices, import, and verify the reported recovery file exists.
7. Change a manual type/HLTB ID during refresh; verify it survives. Check keyboard
   access, long titles, scrolling, and Windows/CachyOS display scaling.
