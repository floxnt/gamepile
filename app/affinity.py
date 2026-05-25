"""
Affinity system — v2.

Two responsibilities:
  1. Scoring: compute how much a candidate's genres/tags/developer shift its
     recommendation score based on accumulated taste signals.
  2. Updating: after feedback, translate ratings into affinity deltas and persist
     them in the affinity table in a single transaction.

Deduplication rule (developer > tag > genre):
  If a label name appears in multiple kinds (e.g. "Action" is both a Steam genre
  and a SteamSpy tag), only the highest-precision kind is used. This prevents
  a single signal from being counted twice with different multipliers.
"""

import sqlite3
from typing import Optional

from app import database as db
from app.models import Game, PickHistory


# Multipliers per kind — developer is most specific so has the strongest signal.
_MULTIPLIERS = {"genre": 0.5, "tag": 0.3, "developer": 0.7}

# Feedback step 2 (overall rating) → affinity delta.
_RATING_DELTA = {5: +1.0, 4: +0.5, 3: 0.0, 2: -0.5, 1: -1.0}

# Feedback step 3 (genre/style match) → additional modifier for genres and tags only.
_GENRE_MATCH_MOD = {5: +0.5, 4: +0.25, 3: 0.0, 2: -0.25, 1: -0.5}

# Minimum absolute contribution to include in a "Why" reason string.
_REASON_THRESHOLD = 0.3


def deduplicate_labels(
    genres: list[str],
    user_tags: list[str],
    developer: Optional[str],
) -> list[tuple[str, str]]:
    """
    Return (kind, canonical_value) pairs with cross-kind duplicates resolved
    to the highest-precision kind (developer > tag > genre).

    The canonical value preserves original casing for DB storage; the key used
    for deduplication is lowercased.
    """
    seen: dict[str, tuple[str, str]] = {}  # lower_name → (kind, value)

    for g in genres:
        k = g.lower()
        if k not in seen:
            seen[k] = ("genre", g)

    for t in user_tags:
        k = t.lower()
        # tag overrides genre if same name
        seen[k] = ("tag", t)

    if developer:
        k = developer.lower()
        # developer overrides tag/genre if same name
        seen[k] = ("developer", developer)

    return list(seen.values())


def compute_affinity_score(game: Game, affinities: dict) -> float:
    """
    Return the total affinity contribution for one candidate, capped at ±5.

    affinities: {(kind, value_lower): (weight, pick_count)} as returned by
    db.get_all_affinities().
    """
    if not affinities:
        return 0.0

    labels = deduplicate_labels(game.genre_list(), game.user_tags_list(), game.developer)
    total = 0.0

    for kind, value in labels:
        key = (kind, value.lower())
        if key not in affinities:
            continue
        weight, pick_count = affinities[key]
        confidence = min(pick_count / 5.0, 1.0)
        total += weight * _MULTIPLIERS[kind] * confidence

    return max(-5.0, min(5.0, total))


def get_affinity_summary(game: Game, affinities: dict) -> list[str]:
    """
    Return human-readable reason strings for the "Why picked" card line,
    e.g. ["matches your taste (FromSoftware: +3.2, Action: +1.1)"].
    Returns [] when no affinity contribution clears the display threshold.
    """
    if not affinities:
        return []

    labels = deduplicate_labels(game.genre_list(), game.user_tags_list(), game.developer)
    contributions: list[tuple[float, str]] = []

    for kind, value in labels:
        key = (kind, value.lower())
        if key not in affinities:
            continue
        weight, pick_count = affinities[key]
        confidence = min(pick_count / 5.0, 1.0)
        contrib = weight * _MULTIPLIERS[kind] * confidence
        if abs(contrib) >= _REASON_THRESHOLD:
            contributions.append((contrib, value))

    if not contributions:
        return []

    contributions.sort(key=lambda x: abs(x[0]), reverse=True)
    top = contributions[:2]
    parts = [f"{label}: {'+' if c >= 0 else ''}{c:.1f}" for c, label in top]
    return [f"matches your taste ({', '.join(parts)})"]


def get_affinity_explanation(game: Game, affinities: dict) -> list:
    """Return ExplanationComponent list for the Why Picked breakdown."""
    from app.recommender import ExplanationComponent
    if not affinities:
        return []

    labels = deduplicate_labels(game.genre_list(), game.user_tags_list(), game.developer)
    matched_labels: list[str] = []
    total_contrib = 0.0
    low_confidence_count = 0
    total_count = 0

    for kind, value in labels:
        key = (kind, value.lower())
        if key not in affinities:
            continue
        weight, pick_count = affinities[key]
        confidence = min(pick_count / 5.0, 1.0)
        contrib = weight * _MULTIPLIERS[kind] * confidence
        if abs(contrib) >= 0.1:
            matched_labels.append(value)
            total_contrib += contrib
            total_count += 1
            if pick_count < 5:
                low_confidence_count += 1

    if not matched_labels:
        return []

    components: list[ExplanationComponent] = []
    label_text = ", ".join(matched_labels[:4])
    if len(matched_labels) > 4:
        label_text += f" +{len(matched_labels) - 4} more"

    if total_contrib >= 0:
        components.append(ExplanationComponent(
            label=f"Matches your taste: {label_text}",
            magnitude=abs(total_contrib),
        ))
    else:
        components.append(ExplanationComponent(
            label=f"Against your taste: {label_text}",
            magnitude=abs(total_contrib),
            is_negative=True,
        ))

    if low_confidence_count > 0 and total_count > 0:
        if low_confidence_count == total_count:
            components.append(ExplanationComponent(
                label="Low confidence — GamePile has limited feedback for these taste labels",
                magnitude=0.0,
            ))
        elif low_confidence_count > total_count // 2:
            components.append(ExplanationComponent(
                label="Mixed confidence — some taste labels have limited feedback",
                magnitude=0.0,
            ))

    return components


def apply_quick_drop_affinity(
    conn: sqlite3.Connection,
    game: Game,
    strength: str,
) -> None:
    """
    Apply flat affinity penalty from the quick-action buttons on cards.
    strength: "soft"   (Bounced off it)  → -0.5 per label
              "strong" (Not my thing)    → -1.0 per label
    """
    delta = -0.5 if strength == "soft" else -1.0
    labels = deduplicate_labels(game.genre_list(), game.user_tags_list(), game.developer)
    for kind, value in labels:
        db.upsert_affinity_delta(conn, kind, value, delta, increment_pick_count=False)


def apply_quick_finished_affinity(conn: sqlite3.Connection, game: Game) -> None:
    """Positive affinity nudge from a Backlog "Mark finished" click.

    +0.5 per deduplicated label — equivalent to a 4-star feedback rating.
    pick_count is NOT incremented because this isn't a Shortlist outcome,
    just a user-confirmed engagement signal.

    Conservative magnitude: the click confirms an inferred state (1.5x+
    HLTB main from the Likely Finished section, or any backlog "Mark
    finished" overflow click). If the user later corrects course, lighter
    deltas are easier to recover from than +1.0.
    """
    delta = 0.5
    labels = deduplicate_labels(game.genre_list(), game.user_tags_list(), game.developer)
    for kind, value in labels:
        db.upsert_affinity_delta(conn, kind, value, delta, increment_pick_count=False)


def apply_did_not_play_affinity(
    conn: sqlite3.Connection,
    picked_game: Game,
    reason: str,
    preferred_game: Optional[Game] = None,
) -> None:
    """
    Apply affinity changes for a did-not-play feedback path.

    no_time        — no changes (user just ran out of time, no taste signal)
    technical_issue — no changes (problem was technical, not preference)
    changed_mood   — no negative on picked game; +0.3 to preferred_game if given
    picked_another_game — -0.3 to picked game; +0.3 to preferred_game if given
    """
    if reason in ("no_time", "technical_issue"):
        return

    picked_labels = deduplicate_labels(
        picked_game.genre_list(),
        picked_game.user_tags_list(),
        picked_game.developer,
    )

    if reason == "changed_mood":
        # No penalty on picked game — user didn't reject it, just changed mood.
        if preferred_game:
            other_labels = deduplicate_labels(
                preferred_game.genre_list(),
                preferred_game.user_tags_list(),
                preferred_game.developer,
            )
            for kind, value in other_labels:
                db.upsert_affinity_delta(conn, kind, value, +0.3, increment_pick_count=False)
        return

    if reason == "picked_another_game":
        for kind, value in picked_labels:
            db.upsert_affinity_delta(conn, kind, value, -0.3, increment_pick_count=False)
        if preferred_game:
            other_labels = deduplicate_labels(
                preferred_game.genre_list(),
                preferred_game.user_tags_list(),
                preferred_game.developer,
            )
            for kind, value in other_labels:
                db.upsert_affinity_delta(conn, kind, value, +0.3, increment_pick_count=False)


def apply_affinity_update(
    conn: sqlite3.Connection,
    pick: PickHistory,
    played_game: Game,
    rating: Optional[int],
    genre_match_rating: Optional[int],
    would_have_other_game: Optional[Game],
) -> None:
    """
    Compute and persist all affinity deltas for one completed feedback flow.
    All writes happen within the caller's transaction (conn from get_db() context).

    Signal sources:
      - Step 2 rating → delta for all labels of the played game
      - Step 3 genre match → additional modifier for genres/tags (not developer)
      - Step 4 retroactive → +0.3 to preferred game labels, -0.3 to played game labels
    """
    labels = deduplicate_labels(
        played_game.genre_list(),
        played_game.user_tags_list(),
        played_game.developer,
    )

    step2_delta = _RATING_DELTA.get(rating, 0.0) if rating is not None else 0.0
    step3_mod   = _GENRE_MATCH_MOD.get(genre_match_rating, 0.0) if genre_match_rating is not None else 0.0

    for kind, value in labels:
        is_dev = (kind == "developer")
        # Developer only gets the step-2 signal; genre match doesn't relate to developer.
        raw = step2_delta + (0.0 if is_dev else step3_mod)
        delta = max(-1.5, min(1.5, raw))
        db.upsert_affinity_delta(conn, kind, value, delta, increment_pick_count=True)

    if would_have_other_game:
        other_labels = deduplicate_labels(
            would_have_other_game.genre_list(),
            would_have_other_game.user_tags_list(),
            would_have_other_game.developer,
        )
        for kind, value in other_labels:
            db.upsert_affinity_delta(conn, kind, value, +0.3, increment_pick_count=False)
        for kind, value in labels:
            db.upsert_affinity_delta(conn, kind, value, -0.3, increment_pick_count=False)
