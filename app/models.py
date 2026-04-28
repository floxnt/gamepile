from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class GameStatus(str, Enum):
    never_played = "never_played"
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
    genres: str                          # comma-separated
    tags: str                            # comma-separated, top 10
    developer: Optional[str]
    publisher: Optional[str]
    metacritic_score: Optional[int]
    opencritic_score: Optional[int]
    steam_review_pct: Optional[int]
    steam_review_count: Optional[int]
    last_refreshed: datetime
    is_active: bool = True

    def primary_genre(self) -> Optional[str]:
        parts = [g.strip() for g in self.genres.split(",") if g.strip()]
        return parts[0] if parts else None

    def genre_list(self) -> list[str]:
        return [g.strip() for g in self.genres.split(",") if g.strip()]

    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]


@dataclass
class GameState:
    appid: int
    status: GameStatus
    hours_played_manual: Optional[float]
    notes: Optional[str]
    updated_at: datetime


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
