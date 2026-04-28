Tonight's Pick — V2 Spec
Overview
V2 layers taste learning and a feedback loop onto v1's deterministic recommender. After picking a game from /, the user is prompted (gently, on next visit, inline) with structured questions about how it went. Their answers update genre/tag/developer affinity scores that feed back into future recommendations. SteamSpy is added as a third fetcher to populate real user tags. V2 must not break v1; all v1 functionality continues to work unchanged.
New external dependencies

SteamSpy — https://steamspy.com/api.php?request=appdetails&appid={appid} — free, no auth, ~1 req/sec rate limit. Returns user-applied tags as a JSON dict where keys are tag names and values are vote counts. Top 10 by vote count = the game's "user tags."

Schema changes
New table: pick_history

id (int, PK, autoincrement)
appid (int, FK to games)
picked_at (timestamp)
time_window_minutes (int, nullable — null for surprise-me picks where time was ignored)
mode (enum: short_term, long_term, both, surprise_me)
candidates_at_pick (text — JSON array of the 5 appids that were shown when this pick was made; needed for the retroactive-pick question)
outcome (enum: pending, played_and_finished, played_and_dropped, played_still_going, did_not_play, skipped_feedback, nullable — null until feedback)
outcome_recorded_at (timestamp, nullable)
rating (int 1-5, nullable — only set if user provided)
genre_match_rating (int 1-5, nullable)
would_have_picked_other_appid (int, nullable, FK to games — set if user said yes to the retroactive question and selected a different game from the original 5)

New table: affinity

kind (enum: genre, tag, developer, PK part 1)
value (text, PK part 2 — the actual genre/tag/developer name, e.g., "Action", "Soulslike", "FromSoftware")
weight (float — starts at 0, range clamped to [-10, +10])
pick_count (int — how many picks this affinity has been updated by; used for confidence weighting)
updated_at (timestamp)

Schema additions to existing games table

user_tags (text — comma-separated, top 10 by vote count from SteamSpy. Distinct from existing tags column which holds Steam categories. Old column stays for backward compatibility but is no longer the affinity signal source.)

Schema additions to existing game_state table

No changes needed.

New / changed routes
GET / — Tonight's Pick (modified)
Two changes:

Inline feedback prompt at the top of the page (above the recommendation grid) if there are pending picks:

Renders as a dismissible card, not a modal
Shows once per app session (dismissal in this session = won't reappear until next launch)
Format described in detail below
"Skip" button on each step closes the prompt for this session, no affinity update
"Skip permanently" option on the prompt header dismisses for this pick specifically (sets outcome = skipped_feedback, never re-prompts for this pick)


Recommendations now include affinity scoring in the underlying scoring function. See "Updated scoring" below.

GET /feedback/{pick_id} — Feedback flow (new)
HTMX-driven, four steps. Each step swaps the previous step's content via HTMX, no full reload. The whole flow lives inside the inline prompt card on /.
Step 1: Did you play it?

Question: "Did you end up playing {game_name}?"
Options: Yes / No / Skip
If No → set outcome = did_not_play, end flow, no affinity changes
If Skip → close prompt for this session, no outcome change (re-asks next session)
If Yes → continue to step 2

Step 2: How was it? (1-5)

Question: "How would you rate the experience?" (Or skipped if user already used "I picked this" → "Not feeling it" path on a prior pick — though for v2, all picks go through the prompt regardless, simplifies logic)
Options: 1, 2, 3, 4, 5, Skip
Save to rating field
Map to outcome:

1-2 → played_and_dropped
3 → played_still_going (neutral, neither dropped nor finished)
4-5 → played_and_finished if user has marked it as finished in game_state, else played_still_going


Skip → save outcome = played_still_going, no rating, continue to step 3

Step 3: Did the genre/style fit?

Question: "Was the genre/style a good match for what you were in the mood for?"
Options: 1 (No, totally off), 2, 3, 4, 5 (Yes, perfect), Skip
Save to genre_match_rating
Skip → continue without saving

Step 4: Retroactive pick

Question: "Looking at the other games we recommended, would you have picked a different one if you could go back?"
Options: No / Yes / Skip
If No or Skip → end flow
If Yes → show the other 4 games from candidates_at_pick as selectable cards. User picks one. Save would_have_picked_other_appid.

End of flow: apply affinity updates (see below), close prompt, show small confirmation: "Thanks — recommendations will adjust based on your feedback."
Affinity update logic
Affinity updates run after the feedback flow completes. Multiple signals are combined:
From the chosen game's metadata (genres, user_tags, developer):
For each genre, user tag, and the developer of the played game:

Step 2 rating maps to a delta:

5 → +1.0
4 → +0.5
3 → 0 (neutral, no change — but pick_count still increments)
2 → -0.5
1 → -1.0


Step 3 genre_match rating only applies to genres and user_tags (not developer):

5 → +0.5 multiplier
4 → +0.25
3 → 0
2 → -0.25
1 → -0.5


Combined delta = step 2 delta + step 3 modifier (clamped to [-1.5, +1.5] per pick)

For developer specifically: only step 2 rating applies (genre match doesn't relate to developer affinity).
From "would have picked other" answer:
If the user said they'd retroactively pick a different game:

The retroactively-preferred game's genres/tags/developer get +0.3 each (they'd have picked it given the chance, that's a positive signal)
The actually-picked game gets an additional -0.3 to its genres/tags/developer (the user disliked it enough to wish for an alternative — beyond what step 2 captured)

Weight clamping:

All weight values clamped to [-10, +10]
Pick count increments by 1 per affinity touched, regardless of whether weight changed (used for confidence — affinity with 50 picks is more trustworthy than affinity with 2 picks)

Affinity scoring contribution to recommendations:
Add to the existing v1 score for each candidate:

For each genre on the candidate: + (affinity.weight × 0.5 × confidence_factor) if affinity exists, where confidence_factor = min(pick_count / 5, 1.0) — affinities ramp up to full effect at 5 picks of confidence
For each user_tag: + (affinity.weight × 0.3 × confidence_factor)
For developer: + (affinity.weight × 0.7 × confidence_factor) — strongest per-instance signal because it's most specific
Total affinity contribution per candidate is clamped to ±5 so taste learning never completely overrides community consensus and time-fit

SteamSpy integration
fetchers/steamspy.py (new)

Single function: fetch_user_tags(appid: int) -> list[tuple[str, int]] returning [(tag_name, vote_count), ...] sorted descending by vote count, top 10
Internal rate limiter: max 1 request per second across the whole fetcher
On SteamSpy 404 / no data: return empty list, log, do not raise
On rate limit response (429): exponential backoff up to 3 retries, then return empty list

Refresh orchestration changes

During refresh, after Steam appdetails call for a game, also call SteamSpy
SteamSpy result populates user_tags column (top 10 tags as comma-separated string)
SteamSpy is cached the same way HLTB and OpenCritic are cached: skip if user_tags is populated AND last_refreshed < 30 days
Force refresh (?force=true on /refresh) bypasses SteamSpy cache too

V2 done criteria

pick_history records every pick with full context (mode, time, candidates shown)
Feedback prompt renders inline at top of / when there are pending picks, dismissible without forcing engagement
All four feedback steps work and update pick_history correctly
Affinity updates run after feedback completion and are visible in the affinity table (you should be able to inspect the table and see why a game was scored higher/lower)
SteamSpy populates user_tags for refreshed games; old tags column is left alone (no rewrites of v1 data)
Recommendations on / reflect affinity scoring (verify by giving high ratings to one developer's games for a few picks, then confirming that developer's other games rank higher in subsequent recommendations)
All v1 functionality continues working unchanged
Card "why picked" reasoning (queued separately for Claude Code) now also includes affinity factors when relevant: "Why: matches your taste (FromSoftware: +3.2), in progress, fits tonight's window"

Out of scope for v2 (explicit deferral)

Reddit/hook-point integration → v3 milestone
Cover art caching → v3 or shareable milestone
First-run setup wizard → shareable milestone
PyInstaller binary build → shareable milestone
Cross-platform packaging → shareable milestone, possibly indefinitely

Code style constraints (continued)

v2 code follows v1 conventions
New routes go in their own files in routes/ (e.g., routes/feedback.py)
New fetcher in fetchers/steamspy.py
Affinity update logic lives in recommender.py or a new affinity.py — your call as the implementer
All affinity updates wrapped in a single transaction so partial failures don't leave the table inconsistent
