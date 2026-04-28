from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class GameStatus(str, Enum):
    never_played = "never_played"
    in_progress = "in_progress"
    # played: auto-inferred from Steam playtime when the user hasn't categorised the
    # game explicitly. Means "Steam shows hours but we don't know why they stopped."
    # Excluded from recommendations by default (same as finished).
    played = "played"
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
    # Set via the "Technical issue" did-not-play reason; surfaced in the library view.
    has_technical_issue: bool = False


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
class PickHistory:
    """One entry in pick_history: a game the user chose to play."""
    id: Optional[int]
    appid: int
    game_name: str                       # denormalised for display without extra join
    picked_at: datetime
    time_window_minutes: Optional[int]   # None for surprise_me (time was irrelevant)
    mode: str                            # short_term | long_term | both | surprise_me
    candidates_at_pick: str             # JSON array of 5 appids shown at pick time
    outcome: Optional[str]              # None until feedback is recorded
    outcome_recorded_at: Optional[datetime]
    rating: Optional[int]               # 1-5, from step 2
    genre_match_rating: Optional[int]   # 1-5, from step 3
    would_have_picked_other_appid: Optional[int]  # from step 4 / step 1.6 sub-path 1
    # Did-not-play branch (step 1.5 / 1.6):
    did_not_play_reason: Optional[str]        # no_time | changed_mood | picked_another_game | technical_issue
    actually_played_appid: Optional[int]      # FK to games — set via step 1.6 library search


@dataclass
class Affinity:
    """Taste signal for a genre, user tag, or developer."""
    kind: str                # "genre" | "tag" | "developer"
    value: str               # e.g. "Action", "Soulslike", "FromSoftware"
    weight: float            # clamped to [-10, +10]
    pick_count: int          # how many picks contributed; drives confidence
    updated_at: datetime
