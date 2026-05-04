"""
Shared Jinja2Templates instance with custom filters.
Import `templates` from here in all route files instead of constructing
a new Jinja2Templates per file.
"""

from pathlib import Path
from typing import Optional

from fastapi.templating import Jinja2Templates


def _fmt_hours(h) -> str:
    """Float hours → '45m', '2.5h', '12h'."""
    if h is None:
        return "—"
    h = float(h)
    if h < 1:
        return f"{int(h * 60)}m"
    if h == int(h):
        return f"{int(h)}h"
    return f"{h:.1f}h"


def _fmt_minutes(m) -> str:
    """Integer playtime minutes → '45m', '1.5h', '8h'."""
    if m is None or m == 0:
        return "—"
    m = int(m)
    if m < 60:
        return f"{m}m"
    h = m / 60
    if h == int(h):
        return f"{int(h)}h"
    return f"{h:.1f}h"


def _review_label(pct: int, count: int) -> str:
    """Map Steam review percentage to the human label Steam itself shows."""
    if pct >= 95 and count >= 500:
        return "Overwhelmingly Positive"
    if pct >= 80:
        return "Very Positive"
    if pct >= 70:
        return "Mostly Positive"
    if pct >= 40:
        return "Mixed"
    return "Mostly Negative"


def _fmt_count(n) -> str:
    """Format a large integer compactly: 1234 → '1.2k', 15000 → '15k', 999 → '999'."""
    if n is None:
        return "—"
    n = int(n)
    if n >= 10_000:
        return f"{n // 1000}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return f"{n:,}"


def _fmt_hltb(h) -> str:
    """HLTB hours: one decimal for < 10h, whole number for 10h+.
    Unlike fmt_hours, never converts to minutes — HLTB data is always hours."""
    if h is None:
        return "—"
    h = float(h)
    if h < 10:
        return f"{h:.1f}h"
    return f"{round(h)}h"


def _duration_label(h) -> str:
    """Human commitment label derived from HLTB main hours."""
    if h is None or h == 0:
        return "Unknown length"
    h = float(h)
    if h <= 5:
        return "Finishable tonight"
    if h <= 15:
        return "A few sessions"
    if h <= 50:
        return "1–2 week commitment"
    if h <= 100:
        return "Multi-week commitment"
    if h <= 300:
        return "Your game for the month"
    return "Lifestyle game"


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["fmt_hours"] = _fmt_hours
templates.env.filters["fmt_minutes"] = _fmt_minutes
templates.env.filters["fmt_count"] = _fmt_count
templates.env.filters["fmt_hltb"] = _fmt_hltb
templates.env.globals["review_label"] = _review_label
templates.env.globals["duration_label"] = _duration_label

# relative_time lives in app.game_detail (pure-functional). Imported here so
# templates can call it as a Jinja global without each route having to inject
# it into context.
from app.game_detail import relative_time as _relative_time  # noqa: E402
templates.env.globals["relative_time"] = _relative_time

# Game-type classification helpers are registered as Jinja globals so the
# Library row template can render badges per-row without each route having
# to pre-attach the type. resolve_type is the public entry point — it
# falls back to classify_game(game) when game.game_type is NULL (the
# migration window before the first refresh after the schema landed).
# The label/tooltip dicts cover all 11 types (full taxonomy).
from app.game_type import (  # noqa: E402
    GAME_TYPE_LABELS as _GAME_TYPE_LABELS,
    GAME_TYPE_TOOLTIPS as _GAME_TYPE_TOOLTIPS,
    resolve_type as _resolve_type,
)
templates.env.globals["resolve_type"] = _resolve_type
templates.env.globals["game_type_labels"] = _GAME_TYPE_LABELS
templates.env.globals["game_type_tooltips"] = _GAME_TYPE_TOOLTIPS
# Backwards-compat alias for templates that haven't been switched yet.
templates.env.globals["compute_game_type"] = _resolve_type

# Phase 1a engagement-signals display helper used by game_detail_engagement.html.
from app.hook_metrics import qualitative_ratio_hint as _qualitative_ratio_hint  # noqa: E402
templates.env.globals["qualitative_ratio_hint"] = _qualitative_ratio_hint
