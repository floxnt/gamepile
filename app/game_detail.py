"""
Game Detail view — pure domain logic.

Mirrors backlog.py / dashboard.py. No FastAPI / DB imports — keep this
layer testable in isolation.

Surfaces:
  - Status dropdown options per state-machine transitions, with
    "Bounced off it" / "Not my thing" labels for the dropped variants
    (matching the existing card-action vocabulary).
  - Per-game affinity contribution pills (this game's labels, looked up
    in the global affinity table — no per-game contribution attribution
    is computed since the historical data isn't preserved).
  - Pick-history rows formatted for display.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.affinity import deduplicate_labels
from app.dashboard import (
    LOW_CONFIDENCE_PICKS,
    PILL_NEUTRAL_CUTOFF,
    Pill,
)
from app.models import Game, GameStatus, PickHistory


# Status-dropdown form values. Underscore separator (not colon) avoids
# any URL-encoding / naive-split surprises.
_DROPPED_SOFT = "dropped_soft"
_DROPPED_STRONG = "dropped_strong"

# Labels — match the card vocabulary so the user sees the same words
# everywhere. "Bounced off it" / "Not my thing" come from the quick-action
# buttons on Shortlist cards.
_STATUS_LABELS = {
    "never_played": "Never played",
    "played_unclassified": "Played (uncategorized)",
    "in_progress": "In progress",
    "finished": "Finished",
    _DROPPED_SOFT: "Bounced off it",
    _DROPPED_STRONG: "Not my thing",
    "not_interested": "Not interested",
}

# State-machine transitions per docs/STATE_MACHINE.md. Each entry is the
# set of statuses a row can transition INTO from the current state. The
# current state is also surfaced in the dropdown (rendered as selected)
# so the user can see what's set without changing it.
_TRANSITIONS_FROM: dict = {
    GameStatus.never_played: ["played_unclassified", "not_interested", "in_progress", "finished"],
    GameStatus.played_unclassified: ["in_progress", "finished", _DROPPED_SOFT, _DROPPED_STRONG, "not_interested"],
    GameStatus.in_progress: ["finished", _DROPPED_SOFT, _DROPPED_STRONG, "not_interested"],
    GameStatus.finished: ["in_progress"],
    GameStatus.dropped: ["in_progress", "finished", "not_interested"],
    GameStatus.not_interested: ["in_progress"],
}


def _current_form_value(status: GameStatus, dropped_strength: Optional[str]) -> str:
    """Encode the current state into the dropdown's form-value vocabulary."""
    if status == GameStatus.dropped:
        return _DROPPED_STRONG if dropped_strength == "strong" else _DROPPED_SOFT
    return status.value


def valid_status_transitions(
    current_status: GameStatus,
    current_dropped_strength: Optional[str] = None,
) -> list:
    """Return [(form_value, label, is_current), ...] for the status dropdown.

    Includes the current state (rendered as selected by the template) plus
    every state reachable per the state-machine doc. dropped_soft and
    dropped_strong appear as separate entries so the dropdown is a flat list.
    """
    current_value = _current_form_value(current_status, current_dropped_strength)
    transitions = list(_TRANSITIONS_FROM.get(current_status, []))

    # Build ordered list: current first (so it shows as the selected option),
    # then transitions in canonical order.
    ordered: list = [current_value]
    for v in transitions:
        if v not in ordered:
            ordered.append(v)

    return [
        (value, _STATUS_LABELS[value], value == current_value)
        for value in ordered
    ]


def parse_status_form_value(value: str) -> tuple[str, Optional[str]]:
    """Decode a dropdown form value into (status, dropped_strength).

    'dropped_soft'   → ('dropped', 'soft')
    'dropped_strong' → ('dropped', 'strong')
    'in_progress'    → ('in_progress', None)
    """
    if value == _DROPPED_SOFT:
        return ("dropped", "soft")
    if value == _DROPPED_STRONG:
        return ("dropped", "strong")
    return (value, None)


# ---------------------------------------------------------------------------
# Per-game affinity pills
# ---------------------------------------------------------------------------

@dataclass
class AffinityCategory:
    """One labeled group of pills for the per-game affinity section."""
    name: str        # "Genres" | "Tags" | "Developers"
    pills: list      # list of Pill


def compute_per_game_affinity_pills(game: Game, affinities: dict) -> list:
    """Return AffinityCategory objects for this game's deduplicated labels.

    Looks each label up in the global affinity table and renders a pill with
    the global weight. Same |weight| > 0.5 cutoff and pick_count < 3
    low-confidence rule as Dashboard. Categories with zero qualifying pills
    are returned with empty `.pills` — the template decides whether to render.

    Dedup precedence (developer > tag > genre, per app.affinity) ensures a
    label like "Action" that's both a Steam genre and a SteamSpy tag only
    contributes once, under the higher-precision kind.
    """
    labels = deduplicate_labels(
        game.genre_list(),
        game.user_tags_list(),
        game.developer,
    )

    by_kind = {"genre": [], "tag": [], "developer": []}
    for kind, value in labels:
        key = (kind, value.lower())
        entry = affinities.get(key)
        if entry is None:
            continue
        weight, pick_count = entry
        if abs(weight) <= PILL_NEUTRAL_CUTOFF:
            continue
        by_kind[kind].append(Pill(
            label=value,
            weight=round(weight, 1),
            pick_count=pick_count,
            low_confidence=pick_count < LOW_CONFIDENCE_PICKS,
            is_negative=weight < 0,
        ))

    # Sort each kind by absolute weight descending — strongest signals first.
    for kind in by_kind:
        by_kind[kind].sort(key=lambda p: -abs(p.weight))

    return [
        AffinityCategory(name="Genres", pills=by_kind["genre"]),
        AffinityCategory(name="Tags", pills=by_kind["tag"]),
        AffinityCategory(name="Developers", pills=by_kind["developer"]),
    ]


# ---------------------------------------------------------------------------
# Pick history formatting
# ---------------------------------------------------------------------------

@dataclass
class PickHistoryRow:
    """Display-ready view of a single pick_history row."""
    date_label: str          # "May 3, 2025"
    mode_label: str          # "Continue something"
    window_label: str        # "90 min window" | "no window"
    outcome_label: str       # "Played and finished. Rating: 5/5. Genre fit: 5/5."
    rating: Optional[int]
    retroactive_pick_label: Optional[str]  # "Picked instead: Hades" or None


_MODE_LABELS = {
    "i_only_have_tonight": "I only have tonight",
    "continue_something": "Continue something",
    "comfort_pick": "Comfort pick",
    "start_something_new": "Start something new",
    "surprise_me": "Surprise me",
    # Legacy values still appearing in old pick_history rows
    "both": "I only have tonight",
    "short_term": "I only have tonight",
    "long_term": "Start something new",
    "surprise": "Surprise me",
}

_DID_NOT_PLAY_REASONS = {
    "no_time": "ran out of time",
    "changed_mood": "changed mood",
    "picked_another_game": "picked another game",
    "technical_issue": "technical issue",
}


def _date_label(dt: datetime) -> str:
    """'May 3, 2025' — used for pick rows. Avoids platform-specific %-d."""
    return f"{_MONTHS[dt.month - 1]} {dt.day}, {dt.year}"


_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _outcome_label(pick: PickHistory, retroactive_name: Optional[str]) -> str:
    if pick.outcome is None and pick.did_not_play_reason is None:
        return "No outcome recorded yet."

    if pick.did_not_play_reason:
        reason = _DID_NOT_PLAY_REASONS.get(pick.did_not_play_reason, pick.did_not_play_reason)
        if pick.did_not_play_reason == "picked_another_game" and retroactive_name:
            return f"Did not play ({reason}). Picked instead: {retroactive_name}."
        return f"Did not play ({reason})."

    base = {
        "played_and_finished": "Played and finished.",
        "played_and_dropped": "Played and dropped.",
        "played_still_going": "Played, still going.",
        "did_not_play": "Did not play.",
    }.get(pick.outcome, f"Outcome: {pick.outcome}.")

    extras: list = []
    if pick.rating is not None:
        extras.append(f"Rating: {pick.rating}/5")
    if pick.genre_match_rating is not None:
        extras.append(f"Genre fit: {pick.genre_match_rating}/5")

    if extras:
        return base + " " + ". ".join(extras) + "."
    return base


def format_pick_history_rows(
    picks: list,
    game_name_by_appid: Optional[dict] = None,
) -> list:
    """Convert PickHistory rows to display-ready PickHistoryRow objects.

    game_name_by_appid resolves actually_played_appid (did-not-play branch)
    to a game name for the "Picked instead: Hades" tail. Pass None / empty
    if names aren't available — the row still renders, just without a
    retroactive-pick label.
    """
    name_map = game_name_by_appid or {}
    out: list = []
    for p in picks:
        retroactive_name: Optional[str] = None
        if p.actually_played_appid:
            retroactive_name = name_map.get(p.actually_played_appid)

        window_label = "no window"
        if p.time_window_minutes:
            window_label = f"{p.time_window_minutes} min window"

        retroactive_label: Optional[str] = None
        if retroactive_name and p.did_not_play_reason != "picked_another_game":
            # Some non-did-not-play paths also set actually_played_appid via
            # step 1.6 — surface it as supplementary info.
            retroactive_label = f"Actually played: {retroactive_name}"

        out.append(PickHistoryRow(
            date_label=_date_label(p.picked_at),
            mode_label=_MODE_LABELS.get(p.mode, p.mode),
            window_label=window_label,
            outcome_label=_outcome_label(p, retroactive_name),
            rating=p.rating,
            retroactive_pick_label=retroactive_label,
        ))
    return out


# ---------------------------------------------------------------------------
# Relative time formatting (used by status bar + game data section)
# ---------------------------------------------------------------------------

def relative_time(dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    """'3 days ago' / '2 hours ago' / '5 minutes ago' / 'Never' / 'just now'.

    For dates in the future (clock skew), returns 'just now'.
    """
    if dt is None:
        return "Never"
    if now is None:
        now = datetime.utcnow()
    delta = now - dt
    seconds = delta.total_seconds()

    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = months // 12
    return f"{years} year{'s' if years != 1 else ''} ago"
