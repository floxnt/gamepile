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


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["fmt_hours"] = _fmt_hours
templates.env.filters["fmt_minutes"] = _fmt_minutes
templates.env.filters["fmt_count"] = _fmt_count
templates.env.globals["review_label"] = _review_label
