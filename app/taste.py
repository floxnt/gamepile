"""Editable taste signals layered over the preserved pre-upgrade model.

Each source owns one contribution: replacing/deleting a source is reversible.
Confidence counts distinct games, so repeated clicks cannot manufacture it.
"""

import json
from datetime import datetime


def write_lock(conn):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def set_signal(conn, source: str, appid: int, contributions: list[dict]):
    write_lock(conn)
    if contributions:
        conn.execute(
            """INSERT INTO taste_signals(source,appid,contributions,updated_at)
            VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET
            appid=excluded.appid, contributions=excluded.contributions, updated_at=excluded.updated_at""",
            (source, appid, json.dumps(contributions), datetime.utcnow().isoformat()),
        )
    else:
        conn.execute("DELETE FROM taste_signals WHERE source=?", (source,))
    rebuild(conn)


def labels(game, delta: float, *, confidence=True):
    from app.affinity import deduplicate_labels

    return [
        {"kind": kind, "value": value, "delta": delta, "confidence": confidence}
        for kind, value in deduplicate_labels(
            game.genre_list(), game.user_tags_list(), game.developer
        )
    ]


def set_personal_rating(conn, game, rating):
    # Same endpoints as session ratings: 1★=-1, 3★=0, 5★=+1.
    values = (
        [] if rating is None else labels(game, max(-1, min(1, (rating / 2 - 3) / 2)))
    )
    set_signal(conn, f"rating:{game.appid}", game.appid, values)


def rebuild(conn):
    """Recompute without subtraction from already-clamped aggregate weights."""
    totals = {}
    for row in conn.execute("SELECT * FROM affinity_base"):
        key = (row["kind"], row["value"].casefold())
        totals[key] = [row["value"], row["weight"], row["pick_count"], set()]
    signals = conn.execute("SELECT * FROM taste_signals ORDER BY source").fetchall()
    rated = {row["appid"] for row in signals if row["source"].startswith("rating:")}
    for row in signals:
        # An explicit personal rating supersedes the weaker status nudge.
        if row["source"].startswith("quick:") and row["appid"] in rated:
            continue
        for item in json.loads(row["contributions"]):
            key = (item["kind"], item["value"].casefold())
            entry = totals.setdefault(key, [item["value"], 0.0, 0, set()])
            entry[1] += item["delta"]
            if item.get("confidence"):
                entry[3].add(row["appid"])
    now = datetime.utcnow().isoformat()
    conn.execute("DELETE FROM affinity")
    conn.executemany(
        "INSERT INTO affinity(kind,value,weight,pick_count,updated_at) VALUES (?,?,?,?,?)",
        [
            (kind, label, max(-10, min(10, weight)), count + len(games), now)
            for (kind, _), (label, weight, count, games) in totals.items()
        ],
    )


def refresh_rating_labels(conn, game):
    """Fill rating labels after restoring onto an empty metadata cache."""
    row = conn.execute(
        "SELECT personal_rating FROM game_state WHERE appid=?", (game.appid,)
    ).fetchone()
    if not row or row["personal_rating"] is None:
        return
    values = labels(game, max(-1, min(1, (row["personal_rating"] / 2 - 3) / 2)))
    existing = conn.execute(
        "SELECT contributions FROM taste_signals WHERE source=?",
        (f"rating:{game.appid}",),
    ).fetchone()
    if values and (not existing or json.loads(existing["contributions"]) != values):
        set_signal(conn, f"rating:{game.appid}", game.appid, values)
