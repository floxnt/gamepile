"""
In-memory state for the feedback prompt.

"Once per app session" is implemented by tracking dismissed pick IDs in a
module-level set. The set resets when the process restarts — which is
exactly what "until next launch" means for a desktop app.

No persistence, no cookies needed. Same pattern as sync.py's progress object.
"""

_dismissed: set[int] = set()


def is_dismissed(pick_id: int) -> bool:
    return pick_id in _dismissed


def dismiss_for_session(pick_id: int) -> None:
    """Hide prompt for this pick until the app is restarted."""
    _dismissed.add(pick_id)
