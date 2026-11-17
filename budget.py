"""Monthly budget: a number the user sets, and where this calendar month is heading against it.

The budget lives in ``budget.json`` next to the DB (``{"monthly_usd": 500.0}``). An absent
file means no budget: never a default, never 0. Spend is the dashboard's own costing
(``api.get_daily``: API list price, per day), for the current calendar month whatever
range the dashboard is filtered to.
"""

import calendar
import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import api
import config

TRAILING_DAYS = 7


def budget_path(db_path=None):
    return Path(db_path or config.DB_PATH).with_name("budget.json")


def load_budget(db_path=None):
    """The monthly budget in USD, or None when unset (or the file is unreadable)."""
    try:
        v = json.loads(budget_path(db_path).read_text(encoding="utf-8"))["monthly_usd"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return float(v) if _valid(v) else None


def _valid(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        f = float(v)  # an int too big for a float overflows here
    except OverflowError:
        return False
    return math.isfinite(f) and f > 0


def set_budget(value, db_path=None):
    """Store ``value`` (finite, > 0) atomically; ``None`` clears the budget. ValueError otherwise."""
    path = budget_path(db_path)
    if value is None:
        path.unlink(missing_ok=True)
        return None
    if not _valid(value):
        raise ValueError("monthly_usd must be a finite number greater than 0")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")  # one temp file per writer
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"monthly_usd": float(value)}, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return float(value)


def month_status(db_path=None, today=None, tz="local"):
    """Where the current calendar month stands. Data runs through ``today`` inclusive.

    days_remaining = days in month - day of month. The daily rate is the average of the last
    7 calendar days (/7) once 7 days of the month have passed, else month-to-date spend per
    elapsed day. projected = spent + rate * days_remaining. gap = projected - budget
    (positive = over); needed_daily = (budget - spent) / days_remaining."""
    db_path = Path(db_path or config.DB_PATH)
    if today is None:
        today = datetime.now(timezone.utc).date() if tz == "utc" else date.today()
    first = today.replace(day=1)
    dim = calendar.monthrange(today.year, today.month)[1]
    # start/end filters compare UTC days; widen by one day each side, then bucket by the display day.
    got = api.get_daily(start=(first - timedelta(days=1)).isoformat(),
                        end=(first.replace(day=dim) + timedelta(days=1)).isoformat(),
                        tz=tz, db_path=db_path)
    per_day = {}
    for r in got.get("daily", []):
        per_day[r["day"]] = per_day.get(r["day"], 0.0) + r["cost"]
    days = [(first + timedelta(days=i)) for i in range(today.day)]
    spent = sum(per_day.get(d.isoformat(), 0.0) for d in days)
    remaining = dim - today.day
    if today.day >= TRAILING_DAYS:
        method = "trailing_7d"
        rate = sum(per_day.get(d.isoformat(), 0.0) for d in days[-TRAILING_DAYS:]) / TRAILING_DAYS
    else:
        method = "month_to_date"
        rate = spent / today.day
    projected = spent + rate * remaining
    budget = load_budget(db_path)
    return {
        "month": today.strftime("%Y-%m"), "spent": round(spent, 6), "budget": budget,
        "projected": round(projected, 6),
        "gap": None if budget is None else round(projected - budget, 6),
        "needed_daily": (round((budget - spent) / remaining, 6) if budget is not None and remaining else None),
        "daily_rate": round(rate, 6), "method": method,
        "days_elapsed": today.day, "days_remaining": remaining,
        "basis": "estimated at API list price",
    }
