Backlog View Spec
Purpose
A top-level page in GamePile dedicated to actively managing the unfinished portion of your Steam library. Distinct from Library (inventory) and Shortlist (recommendations). The Backlog answers: "what am I currently considering, what's nearly done, and what's worth committing to?"
Navigation
Add Backlog as a top-level nav item between Shortlist and Library. New nav order:
GamePile · Shortlist · Backlog · Library (Refresh Library / Force on the right)
Page route: /backlog
Eligibility — what's in the backlog
By default, the Backlog includes games where status is one of:

in_progress
played_unclassified
never_played

The Backlog NEVER includes:

finished (done, not pending)
dropped (strong) ("Not my thing" — explicitly rejected)
not_interested
blacklisted

dropped (soft) ("Bounced off it") is excluded by default but available via a filter chip ("Include bounced") so the user can reconsider games they bounced off without permanently committing them.
Page layout
[Page header: "Backlog"]
[Header band: light stats — see below]
[Filter bar — see below]

[Section: "Picking up where you left off"]
[in_progress games as compact rows]

[Section: "Barely touched"]
[played_unclassified games as compact rows]

[Section: "Never played"]
[never_played games as compact rows]

[Optional section, only if "Include bounced" filter active:]
[Section: "Bounced off"]
[dropped (soft) games as compact rows]
Each section is collapsible (click header to collapse/expand). Sections with zero results are hidden entirely (no empty section headers).
Section headers show count: "Picking up where you left off (8)"
Header band (top of page, above filters)
A single horizontal band with light backlog stats:

Total backlog count (sum of eligible games across all sections)
Total backlog hours (sum of HLTB main for games with data; "+ N games unknown" for HLTB-null games)
"X games in progress · Y barely touched · Z never played" — quick decomposition

Style: muted, single-line, not visually loud. This is supporting context, not a dashboard. Examples:

287 games · ~3,840 hours · 8 in progress · 47 barely touched · 232 never played

If user has zero eligible games:

Your backlog is clear. Try [Comfort Pick →] (link to Shortlist with Comfort Pick mode preselected) to revisit favorites, or refresh your library if you've added games on Steam.

Filter bar
Below the header band. Multi-select chips, OR-logic within each filter group:
Time fit:

Short (<5h)
Medium (5–15h)
Long (15–50h)
Very long (50+h)
Unknown

(Filters by HLTB main remaining-time per the same calculation Shortlist uses; "Unknown" includes HLTB-null games.)
Tags:

Top 15 most-common user_tags in the user's library
Search box for less-common tags ("Roguelike", "Cozy", "Soulslike", etc.)

Status (override default scope):

In progress
Barely touched (played_unclassified)
Never played
Bounced off (this is the toggle that adds the dropped-soft section)

Has HLTB data — toggle (off by default; when on, hides HLTB-null games)
When filters are active, sections automatically hide if their filtered contents are zero.
A "Clear filters" button appears when any filter is active.
Compact row layout
Each game in the backlog renders as a compact row, NOT a full card. Layout:
[Cover ~80px]  [Title]  [Status badge] [Length badge]
               [Description, truncated to ~100 chars]
               [Main 32h · Played 4.2h (13%) · 94% (52k) · MC 89]   [Add to Shortlist] [⋯]
Specifics:

Cover art ~80px wide (smaller than Shortlist cards' 120px), poster orientation
Title bold, single line, truncated with ellipsis if very long
Status badge color-coded per existing system
Length badge per the established categories (Finishable tonight / A few sessions / etc.)
Description italicized, muted, truncated to 100 characters with ellipsis. If null, omit.
Stats line condensed: HLTB main, playtime, Steam %, MC. OpenCritic only if present and integration is healthy.
For in_progress games specifically: include a progress percentage and a thin progress bar inline. Format: Played 4.2h / ~32h main (13%) with a ▓▓░░░░░░░░ style bar. If hours_played_manual is set, use that for the played figure; otherwise use Steam playtime.
Primary action: "Add to Shortlist" — flags this game as a priority for the next Shortlist run. (See "Add to Shortlist behavior" below.)
Overflow menu (⋯): I picked this / Mark in progress / Mark finished / Bounced off it / Not my thing / Already completed / Never recommend

Row vertical height roughly equal to cover thumbnail height. No empty space below.
Default sort within sections

Picking up where you left off (in_progress): sorted by completion percentage descending — closest-to-done first
Barely touched (played_unclassified): sorted by Steam playtime descending — games you actually touched (even briefly) above ones you barely opened
Never played: sorted by affinity score descending — best matches to your taste first
Bounced off (when shown): sorted by date dropped descending — most recent at top

Sort options exposed via dropdown above each section: Default / Title / HLTB main / Playtime / Recently added / Affinity score.
Sort selection per-section, persisted in URL params for sharing/bookmarking: /backlog?in_progress_sort=title&played_unclassified_sort=playtime.
"Add to Shortlist" behavior
A new mechanism. When a user clicks "Add to Shortlist" on a backlog row:

Add the game's appid to a pinned_for_shortlist list (new column on game_state, or a separate small table — implementer's call)
Pinned games receive a strong score boost (+5 to +8) in any Shortlist run, surfacing them near the top
Pinned status persists until either the game is picked from Shortlist (auto-clear), the user manually unpins via the same button (now shows as "Pinned ✓ — click to unpin"), or 14 days pass (auto-expire)
Visual indicator on the row: a small pin icon next to the title when pinned
Shortlist cards for pinned games show a "📌 Pinned from backlog" badge

This makes the Backlog → Shortlist flow concrete: user identifies candidates while browsing the backlog, pins them, then runs Shortlist to get the final 5.
Schema additions

game_state.pinned_for_shortlist (boolean, default false)
game_state.pinned_at (timestamp, nullable; used for 14-day expiry)

Both via try/except ALTER TABLE.
Recommender changes
recommender.py:

For all modes except Surprise me, if a candidate's game_state.pinned_for_shortlist = true, add +6 to its score
After a game is picked from Shortlist (existing "I picked this" action), clear its pin
During Backlog page load, run an inline cleanup pass: clear pins where pinned_at is older than 14 days

Eligibility interaction
A pinned game still must satisfy mode eligibility per the state machine doc. If a user pins a finished game and then runs "Continue something" mode, the pin doesn't override eligibility — finished games aren't eligible for Continue Something regardless. The pin is a score boost within an eligibility set, not a bypass.
Empty states

No games in any backlog section: "Your backlog is clear. Try [Comfort Pick] to revisit favorites, or refresh your library."
All sections filtered out: "No games match your current filters. [Clear filters]"
Section has zero games: hide the section entirely (no empty headers)

Out of scope for v3 backlog (defer to v3.5 or later)

Per-game notes / personal annotations (lives on Game Detail page when that ships)
Manual playtime override input (goes on Game Detail page)
"Acquired date" sort or filter (Steam doesn't expose this cleanly per-game in our current data model; defer)
Backlog burndown chart over time (Dashboard page)
Recommended-removals ("Maybe consider blacklisting these games you haven't touched in 5+ years") — fine idea, separate feature

Done criteria

/backlog route renders with the three default sections
Header band shows accurate counts and total hours
Filter chips work and compose correctly
Compact rows render with all specified fields, descriptions truncated, in-progress games show progress bar
Sort options work per-section, URL params persist sort selection
"Add to Shortlist" pins the game, persists across session, shows pin icon, clears on Shortlist pick or after 14 days
Pinned games receive score boost in Shortlist runs
Recent layout doesn't break Library, Shortlist, or recent picks views
Tested against the real 629-game library: all sections populate correctly, no performance issues from large lists
