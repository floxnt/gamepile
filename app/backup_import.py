"""Validate, preview, and atomically merge portable user data.

The preview is bound to the current user-data fingerprint. Imported picks use
immutable content keys, while taste sources are remapped to local pick IDs.
This makes repeated imports idempotent without trusting IDs from another DB.
"""

import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from app import database as db, taste
from app.models import GameStatus

MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 50000
_PENDING = {}


class ImportError(ValueError):
    pass


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Expected an ISO date and time")
    if parsed.tzinfo:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    return value


Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Flag = Literal[0, 1]
Timestamp = Annotated[str, Field(max_length=64), AfterValidator(_timestamp)]
Label = Annotated[str, Field(min_length=1, max_length=1000)]
Kind = Literal["genre", "tag", "developer"]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class State(Row):
    appid: Id
    status: GameStatus
    hours_played_manual: Annotated[Number, Field(ge=0)] | None
    notes: Annotated[str, Field(max_length=1000000)] | None
    manually_set: Flag
    has_technical_issue: Flag
    blacklisted: Flag
    dropped_strength: Literal["soft", "strong"] | None
    pinned_for_shortlist: Flag
    pinned_at: Timestamp | None
    personal_rating: Annotated[int, Field(strict=True, ge=1, le=10)] | None
    finished_at: Timestamp | None
    updated_at: Timestamp


class Override(Row):
    appid: Id
    # Older backups may carry retired classifications. Preserve the user's
    # string; the UI can reset it to a current classification explicitly.
    game_type: Label | None
    game_type_manual: Flag
    hltb_id_manual: Id | None
    completion_achievement_name_manual: Label | None = None
    stickiness_badge_manual: Label | None = None


class Affinity(Row):
    kind: Kind
    value: Label
    weight: Annotated[Number, Field(ge=-10, le=10)]
    pick_count: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]


class Pick(Row):
    key: str | None = None
    appid: Id
    game_name: Label
    picked_at: Timestamp
    time_window_minutes: Annotated[int, Field(strict=True, ge=0, le=100000)] | None
    mode: Annotated[str, Field(min_length=1, max_length=100)]
    candidates_at_pick: str = "[]"
    outcome: (
        Literal[
            "played_and_finished",
            "played_and_dropped",
            "played_still_going",
            "did_not_play",
            "skipped_feedback",
        ]
        | None
    )
    outcome_recorded_at: Timestamp | None
    rating: Annotated[int, Field(strict=True, ge=1, le=5)] | None
    genre_match_rating: Annotated[int, Field(strict=True, ge=1, le=5)] | None
    would_have_picked_other_appid: Id | None
    did_not_play_reason: (
        Literal["no_time", "changed_mood", "picked_another_game", "technical_issue"]
        | None
    )
    actually_played_appid: Id | None
    status_at_pick: GameStatus | None = None
    was_forever_at_pick: Flag | None = None
    feedback_completed_at: Timestamp | None = None
    legacy_taste_recorded: Flag = 0


class Catalog(Row):
    appid: Id
    name: Label


class Contribution(Row):
    kind: Kind
    value: Label
    delta: Annotated[Number, Field(ge=-1.5, le=1.5)]
    confidence: bool


class Signal(Row):
    source: Annotated[str, Field(max_length=100)]
    appid: Id
    contributions: Annotated[list[Contribution], Field(max_length=1000)]
    updated_at: Timestamp


def pick_key(pick):
    fields = [
        pick.get(k) for k in ("appid", "picked_at", "mode", "time_window_minutes")
    ]
    return hashlib.sha256(
        json.dumps(fields, separators=(",", ":")).encode()
    ).hexdigest()


def _section(payload, name, model, required=True):
    rows = payload.get(name, None if required else [])
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ImportError(f"{name}: expected a list with at most {MAX_ROWS} rows.")
    try:
        return [model.model_validate(row).model_dump(mode="json") for row in rows]
    except ValidationError as exc:
        error = exc.errors()[0]
        # Do not echo the offending payload (it may contain personal notes).
        field = ".".join(str(x) for x in error["loc"])
        raise ImportError(f'{name}.{field}: {error["msg"]}') from None


def _unique(rows, key, label):
    keys = [key(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise ImportError(f"{label}: duplicate entries.")


def validate(raw):
    if len(raw) > MAX_BYTES:
        raise ImportError("This backup is larger than 16 MB.")
    try:

        def reject_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result

        payload = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("Non-finite number")
            ),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ImportError("Choose a valid GamePile JSON backup.") from None
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema")) is not int
        or payload["schema"] not in (1, 2)
    ):
        raise ImportError(
            "Unsupported backup version. This app accepts versions 1 and 2."
        )
    if payload.get("rating_scale") != "0-10":
        raise ImportError("Unsupported rating scale. Expected 0–10 half-star ratings.")
    allowed = {
        "schema",
        "rating_scale",
        "exported_at",
        "app_version",
        "game_state",
        "game_overrides",
        "affinity",
        "picks",
    }
    if payload["schema"] == 2:
        allowed |= {"catalog", "affinity_base", "taste_signals"}
    if set(payload) - allowed:
        raise ImportError(
            "This backup has unrecognized sections; it may need a newer app."
        )
    out = {"schema": payload["schema"]}
    for name, model in [
        ("game_state", State),
        ("game_overrides", Override),
        ("affinity", Affinity),
        ("picks", Pick),
        ("catalog", Catalog),
        ("affinity_base", Affinity),
        ("taste_signals", Signal),
    ]:
        out[name] = _section(
            payload,
            name,
            model,
            required=name not in ("catalog", "affinity_base", "taste_signals")
            or out["schema"] == 2,
        )
    for name in ("game_state", "game_overrides", "catalog"):
        _unique(out[name], lambda row: row["appid"], name)
    for name in ("affinity", "affinity_base"):
        _unique(out[name], lambda row: (row["kind"], row["value"].casefold()), name)
    for row in out["game_overrides"]:
        if row["game_type_manual"] and not row["game_type"]:
            raise ImportError("A manual game type needs its chosen value.")
    for pick in out["picks"]:
        key = pick_key(pick)
        if out["schema"] == 2 and pick["key"] != key:
            raise ImportError("A pick has an invalid identity key.")
        pick["key"] = key
        try:
            ids = json.loads(pick["candidates_at_pick"])
            if (
                not isinstance(ids, list)
                or len(ids) > 20000
                or any(type(v) is not int or not 0 < v <= 2**63 - 1 for v in ids)
            ):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise ImportError("A pick has an invalid recommendation list.") from None
        if out["schema"] == 1 and pick["outcome"]:
            pick["feedback_completed_at"] = (
                pick["outcome_recorded_at"] or pick["picked_at"]
            )
            pick["legacy_taste_recorded"] = 1
    _unique(out["picks"], lambda row: row["key"], "picks")
    _unique(out["taste_signals"], lambda row: row["source"], "taste_signals")
    picks = {p["key"]: p for p in out["picks"]}
    states = {row["appid"]: row for row in out["game_state"]}
    for signal in out["taste_signals"]:
        kind, _, identifier = signal["source"].partition(":")
        if kind == "pick":
            if identifier not in picks or picks[identifier]["appid"] != signal["appid"]:
                raise ImportError(
                    "A taste signal refers to a missing or different pick."
                )
        elif kind in ("rating", "quick"):
            if identifier != str(signal["appid"]) or signal["appid"] not in states:
                raise ImportError("A taste signal refers to a missing game state.")
        else:
            raise ImportError("Unrecognized taste signal source.")
    if out["schema"] == 1:
        out["affinity_base"] = out["affinity"]
    return out


def _catalog(data):
    names = {row["appid"]: row["name"] for row in data["catalog"]}
    for pick in data["picks"]:
        names.setdefault(pick["appid"], pick["game_name"])
        for appid in json.loads(pick["candidates_at_pick"]) + [
            pick["actually_played_appid"],
            pick["would_have_picked_other_appid"],
        ]:
            if appid:
                names.setdefault(appid, f"Steam app {appid}")
    for section in ("game_state", "game_overrides", "taste_signals"):
        for row in data[section]:
            names.setdefault(row["appid"], f'Steam app {row["appid"]}')
    return names


def fingerprint(conn):
    from app.backup import build_backup

    data = build_backup(conn)
    data.pop("exported_at")
    data.pop("app_version")
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _authored(row):
    return bool(
        row["manually_set"]
        or row["notes"]
        or row["hours_played_manual"] is not None
        or row["personal_rating"] is not None
        or row["blacklisted"]
        or row["has_technical_issue"]
        or row["pinned_for_shortlist"]
    )


def preview(conn, data):
    existing = {
        row["appid"]: dict(row) for row in conn.execute("SELECT * FROM game_state")
    }
    owned = {row[0] for row in conn.execute("SELECT appid FROM games")}
    picks = {
        pick_key(dict(row)): dict(row)
        for row in conn.execute("SELECT * FROM pick_history")
    }
    overrides = {
        row["appid"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM games WHERE game_type_manual=1 OR hltb_id_manual IS NOT NULL"
        )
    }

    def differs(left, right, ignored=()):
        return any(
            left.get(key) != value for key, value in right.items() if key not in ignored
        )

    counts = {
        "Game records": len(data["game_state"]),
        "Manual overrides": len(data["game_overrides"]),
        "Pick history entries": len(data["picks"]),
        "Taste signals": len(data["taste_signals"]),
    }
    conflicts = {
        "Game records": sum(
            r["appid"] in existing
            and _authored(existing[r["appid"]])
            and differs(existing[r["appid"]], r, ("updated_at",))
            for r in data["game_state"]
        ),
        "Manual overrides": sum(
            r["appid"] in overrides and differs(overrides[r["appid"]], r)
            for r in data["game_overrides"]
        ),
        "Pick history entries": sum(
            r["key"] in picks and differs(picks[r["key"]], r, ("key",))
            for r in data["picks"]
        ),
    }
    placeholders = [
        {"appid": appid, "name": name}
        for appid, name in _catalog(data).items()
        if appid not in owned
    ]
    return {
        "counts": counts,
        "conflicts": conflicts,
        "placeholders": placeholders,
        "legacy": data["schema"] == 1,
    }


def prepare(conn, data):
    now = time.monotonic()
    for token in list(_PENDING):
        if now - _PENDING[token]["created"] > 600:
            del _PENDING[token]
    while len(_PENDING) >= 5:
        del _PENDING[next(iter(_PENDING))]
    token = secrets.token_urlsafe(24)
    _PENDING[token] = {"data": data, "fingerprint": fingerprint(conn), "created": now}
    return token, preview(conn, data)


def _upsert(conn, table, row, key):
    # Only internal, validated column names reach this helper.
    columns = list(row)
    updates = ",".join(f"{col}=excluded.{col}" for col in columns if col != key)
    conn.execute(
        f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)}) '
        f"ON CONFLICT({key}) DO UPDATE SET {updates}",
        [row[col] for col in columns],
    )


def merge(conn, data, policy):
    """Merge inside the caller's transaction. Never delete local-only records."""
    if policy not in ("backup", "local"):
        raise ImportError("Choose which values to keep when records conflict.")
    taste.write_lock(conn)
    now = datetime.utcnow().isoformat()
    for appid, name in _catalog(data).items():
        conn.execute(
            "INSERT OR IGNORE INTO games(appid,name,last_refreshed,is_active) VALUES (?,?,?,0)",
            (appid, name, now),
        )
        db._ensure_state_row(conn, appid)
    selected_states = set()
    for row in data["game_state"]:
        existing = conn.execute(
            "SELECT * FROM game_state WHERE appid=?", (row["appid"],)
        ).fetchone()
        if policy == "local" and existing and _authored(existing):
            continue
        _upsert(conn, "game_state", row, "appid")
        selected_states.add(row["appid"])
        conn.execute(
            "DELETE FROM taste_signals WHERE source IN (?,?)",
            (f'rating:{row["appid"]}', f'quick:{row["appid"]}'),
        )
    for row in data["game_overrides"]:
        game = db.get_game_by_appid(conn, row["appid"])
        if row["game_type_manual"] and (
            policy == "backup" or not game.game_type_manual
        ):
            db.set_game_type(conn, row["appid"], row["game_type"], manual=True)
        for field in ("completion_achievement_name_manual", "stickiness_badge_manual"):
            if row[field] is not None and (
                policy == "backup" or getattr(game, field) is None
            ):
                conn.execute(
                    f"UPDATE games SET {field}=? WHERE appid=?",
                    (row[field], row["appid"]),
                )
        if row["hltb_id_manual"] is not None and (
            policy == "backup" or game.hltb_id_manual is None
        ):
            # Imported IDs invalidate durations from a different record, and
            # the revision invalidates any enrichment currently in flight.
            db.set_hltb_id_manual(
                conn, row["appid"], row["hltb_id_manual"], None, None, None
            )
            conn.execute(
                "UPDATE games SET hltb_fetched_at=NULL WHERE appid=?", (row["appid"],)
            )
    local_picks = {
        pick_key(dict(row)): row["id"]
        for row in conn.execute("SELECT * FROM pick_history")
    }
    selected_picks = set()
    for row in data["picks"]:
        key = row["key"]
        if key in local_picks and policy == "local":
            continue
        values = {k: v for k, v in row.items() if k != "key"}
        if key in local_picks:
            _upsert(conn, "pick_history", {"id": local_picks[key], **values}, "id")
        else:
            columns = list(values)
            cursor = conn.execute(
                f'INSERT INTO pick_history ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                list(values.values()),
            )
            local_picks[key] = cursor.lastrowid
        selected_picks.add(key)
        conn.execute(
            "DELETE FROM taste_signals WHERE source=?", (f"pick:{local_picks[key]}",)
        )
    for row in data["affinity_base"]:
        previous = conn.execute(
            "SELECT value FROM affinity_base WHERE kind=? AND lower(value)=lower(?)",
            (row["kind"], row["value"]),
        ).fetchone()
        if previous and policy == "local":
            continue
        if previous:
            conn.execute(
                "DELETE FROM affinity_base WHERE kind=? AND value=?",
                (row["kind"], previous["value"]),
            )
        conn.execute(
            "INSERT INTO affinity_base(kind,value,weight,pick_count) VALUES (?,?,?,?)",
            tuple(row[k] for k in ("kind", "value", "weight", "pick_count")),
        )
    for row in data["taste_signals"]:
        kind, identifier = row["source"].split(":", 1)
        if kind == "pick":
            if identifier not in selected_picks:
                continue
            source = f"pick:{local_picks[identifier]}"
        else:
            if row["appid"] not in selected_states:
                continue
            source = row["source"]
        _upsert(
            conn,
            "taste_signals",
            {
                **row,
                "source": source,
                "contributions": json.dumps(row["contributions"]),
            },
            "source",
        )
    if data["schema"] == 1:
        # Ratings in old exports never contributed to their aggregate model.
        for appid in selected_states:
            state = db.get_game_with_state_by_appid(conn, appid)
            if state.state.personal_rating is not None:
                taste.set_personal_rating(conn, state.game, state.state.personal_rating)
    taste.rebuild(conn)
    conn.execute("DELETE FROM action_undo")
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise ImportError("The backup contains unresolved game references.")


def apply(conn, token, policy):
    from app.backup import write_backup

    pending = _PENDING.get(token)
    if not pending or time.monotonic() - pending["created"] > 600:
        raise ImportError("This preview has expired. Select the file again.")
    taste.write_lock(conn)
    if fingerprint(conn) != pending["fingerprint"]:
        raise ImportError(
            "Your library changed after this preview. Preview the file again before importing."
        )
    if policy not in ("backup", "local"):
        raise ImportError("Choose which values to keep when records conflict.")
    before_path = write_backup(conn, target_dir=db.DB_PATH.parent / "backups")
    merge(conn, pending["data"], policy)
    return before_path


def discard(token):
    _PENDING.pop(token, None)
