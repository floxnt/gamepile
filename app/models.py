from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class GameStatus(str, Enum):
    never_played = "never_played"
    # Steam shows playtime but the user hasn't categorised the game.
    # Eligible for "I only have tonight", "Comfort pick", "Surprise me" and
    # (when playtime < 30min) "Start something new".
    played_unclassified = "played_unclassified"
    in_progress = "in_progress"
    finished = "finished"
    dropped = "dropped"
    not_interested = "not_interested"


@dataclass
class Game:
    appid: int
    name: str
    playtime_minutes: int
    last_played_steam: Optional[datetime]
    installed: Optional[bool]
    hltb_main_hours: Optional[float]
    hltb_main_extra_hours: Optional[float]
    hltb_completionist_hours: Optional[float]
    genres: str                          # comma-separated Steam store genres
    tags: str                            # comma-separated Steam store categories (v1 compat, kept)
    developer: Optional[str]
    publisher: Optional[str]
    metacritic_score: Optional[int]
    opencritic_score: Optional[int]
    steam_review_pct: Optional[int]
    steam_review_count: Optional[int]
    last_refreshed: datetime
    is_active: bool = True
    user_tags: str = ""                  # comma-separated SteamSpy user tags (top 10 by vote)
    release_date: Optional[datetime] = None  # parsed from Steam appdetails; drives age-based caching
    description: Optional[str] = None    # Steam short_description (marketing summary)
    # v3 hook-point Phase 1a: per-game engagement signals.
    completion_rate: Optional[float] = None            # 0.0-1.0; from Steam global achievement %
    completion_rate_confidence: Optional[str] = None   # 'high' / 'low' / None — see hook_metrics.compute_completion_rate_confidence
    cliff_metric: Optional[float] = None               # pct-point gap; largest drop after discarding launch achievements
    cliff_position: Optional[float] = None             # 0.0-1.0; position of the largest cliff in the sorted achievement list (0 = early, 1 = late)
    review_playtime_median: Optional[int] = None       # minutes; median of author.playtime_at_review across the fetched review sample
    stickiness_ratio: Optional[float] = None           # 0.0-1.0; fraction of reviewers with >=20h at review time
    playtime_median_avg_ratio: Optional[float] = None  # 0.0-1.0; SteamSpy median_forever / average_forever
    # v3 game-type classification (see app/game_type.py)
    game_type: Optional[str] = None                    # one of GAME_TYPE_* constants; cached classify_game result
    game_type_manual: bool = False                     # user override; when True, refresh inference doesn't override
    app_type: Optional[str] = None                     # raw Steam appdetails type ("game" / "dlc" / "demo" / "music" / etc.)
    # v3 Phase 4 — manual completion-achievement override. When set, sync
    # bypasses the heuristic story-completion match and resolves
    # completion_rate from this achievement's current unlock percent
    # (forced 'high' confidence). NULL means auto-derive.
    completion_achievement_name_manual: Optional[str] = None
    # v3 Phase 4 — manual HLTB game ID. When set, sync uses this ID
    # directly via fetch_hltb_by_id and skips name-based search.
    # Escape hatch for stuck-on-search titles where title cleaning
    # can't find the right record. NULL means auto-derive via search.
    hltb_id_manual: Optional[int] = None
    # v3 Phase 4 — manual stickiness badge override. One of the five
    # active BADGE_* constants. NULL means use auto-computed badge.
    # Surfaces on Library, Game Detail, and Shortlist pill — bypasses
    # game-type display rules so the user can assert a badge even on
    # types where engagement is normally suppressed.
    stickiness_badge_manual: Optional[str] = None
    # v0.7.0 — median per-achievement global unlock percent (0.0–100.0).
    # Display-only stat surfaced as a Library column and a Game Detail
    # row. NULL when the game has no achievements.
    median_achievement_unlock_pct: Optional[float] = None
    # v0.8.7 — user's own unlock percent for this game (0.0–100.0).
    # Display-only Library column ("My Achievement %"). NULL when the
    # game has no achievements, the user's profile is API-private, or
    # the fetch hasn't run yet for this game. Populated by sync via
    # GetPlayerAchievements alongside the global percentages fetch.
    user_achievement_pct: Optional[float] = None

    def primary_genre(self) -> Optional[str]:
        parts = [g.strip() for g in self.genres.split(",") if g.strip()]
        return parts[0] if parts else None

    def genre_list(self) -> list[str]:
        return [g.strip() for g in self.genres.split(",") if g.strip()]

    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    def user_tags_list(self) -> list[str]:
        return [t.strip() for t in self.user_tags.split(",") if t.strip()]


@dataclass
class GameState:
    appid: int
    status: GameStatus
    hours_played_manual: Optional[float]
    notes: Optional[str]
    updated_at: datetime
    # False = status was auto-inferred from playtime; True = user set it via the UI.
    # Auto-inference only runs when manually_set is False.
    manually_set: bool = False
    has_technical_issue: bool = False
    # Hard exclusion: never appears in recommendations regardless of mode or toggles.
    blacklisted: bool = False
    # Only set when status == dropped. "soft" = bounced off; "strong" = not my thing.
    dropped_strength: Optional[str] = None
    # Backlog → Shortlist priority pin. When true, the game receives PIN_SCORE_BOOST
    # in every Shortlist mode except Surprise me. Cleared automatically on "I picked
    # this", on manual unpin, or after 14 days.
    pinned_for_shortlist: bool = False
    pinned_at: Optional[datetime] = None
    # 1-5 user rating set via the Game Detail page; NULL when the user
    # hasn't rated. Distinct from the recommender's affinity weight.
    personal_rating: Optional[int] = None


@dataclass
class RefreshLog:
    id: Optional[int]
    started_at: datetime
    completed_at: Optional[datetime]
    games_added: int
    games_updated: int
    errors: Optional[str]


@dataclass
class GameWithState:
    """Joined view used by the recommender and templates."""
    game: Game
    state: GameState


@dataclass
class RecentPick:
    """A pick_history row enriched with current game and state data for display."""
    pick: "PickHistory"
    game: Game
    state: GameState


@dataclass
class PickHistory:
    """One entry in pick_history: a game the user chose to play."""
    id: Optional[int]
    appid: int
    game_name: str                       # denormalised for display without extra join
    picked_at: datetime
    time_window_minutes: Optional[int]   # None for surprise_me (time was irrelevant)
    mode: str                            # see RecommendMode for canonical values
    candidates_at_pick: str             # JSON array of 5 appids shown at pick time
    outcome: Optional[str]              # None until feedback is recorded
    outcome_recorded_at: Optional[datetime]
    rating: Optional[int]               # 1-5, from step 2
    genre_match_rating: Optional[int]   # 1-5, from step 3
    would_have_picked_other_appid: Optional[int]  # from step 4 / step 1.6 sub-path 1
    # Did-not-play branch (step 1.5 / 1.6):
    did_not_play_reason: Optional[str]        # no_time | changed_mood | picked_another_game | technical_issue
    actually_played_appid: Optional[int]      # FK to games — set via step 1.6 library search
    # Eligibility-at-pick-time snapshot, captured by mark_picked. Both NULL on
    # rows inserted before the v3 Dashboard work — treated as "include" by the
    # Dashboard picks-per-week filter (no backfill from current state, since
    # current state is unreliable signal for past intent).
    status_at_pick: Optional[str] = None      # one of GameStatus values; NULL on legacy rows
    was_forever_at_pick: Optional[bool] = None  # is_forever_game(game) at pick time


@dataclass
class Affinity:
    """Taste signal for a genre, user tag, or developer."""
    kind: str                # "genre" | "tag" | "developer"
    value: str               # e.g. "Action", "Soulslike", "FromSoftware"
    weight: float            # clamped to [-10, +10]
    pick_count: int          # how many picks contributed; drives confidence
    updated_at: datetime
