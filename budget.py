"""Monthly budget: a number the user sets, and where this calendar month is heading against it.

The budget lives in ``budget.json`` next to the DB (``{"monthly_usd": 500.0}``), together with the
per-project display tags (``"project_tags"``). An absent file or key means no budget: never a default,
never 0. Spend is the dashboard's own costing
(``api.get_daily``: API list price, per day), for the current calendar month whatever
range the dashboard is filtered to.
"""

import calendar
import contextlib
import json
import math
import os
import tempfile
import threading
import time
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


_lock = threading.RLock()
_depth = threading.local()


@contextlib.contextmanager
def _locked(path):
    """One writer at a time across threads AND processes (the dashboard and the CLI both write
    this file): an O_EXCL lock file beside it, re-entrant per thread, stale after 10 s."""
    with _lock:
        if getattr(_depth, "n", 0):
            _depth.n += 1
            try:
                yield
            finally:
                _depth.n -= 1
            return
        lock = path.with_name(path.name + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 5
        while True:
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 10:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise OSError("settings file is locked by another process")
                time.sleep(0.02)
        _depth.n = 1
        try:
            yield
        finally:
            _depth.n = 0
            lock.unlink(missing_ok=True)
TAG_MAX = 40


def _read(path, strict=False):
    """The settings dict; unreadable reads as empty. ``strict`` (writers) raises ValueError
    instead when the file exists but can't be parsed, so a save never erases the other settings."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise ValueError(f"{path.name} is unreadable; fix or delete it") from None
        return {}
    if not isinstance(d, dict) and strict:
        raise ValueError(f"{path.name} is not a JSON object; fix or delete it")
    return d if isinstance(d, dict) else {}


def _write(path, d):
    """Replace the settings file atomically; an empty dict removes it (no file = nothing set)."""
    if not d:
        path.unlink(missing_ok=True)
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")  # one temp file per writer
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def set_budget(value, db_path=None):
    """Store ``value`` (finite, > 0) atomically; ``None`` clears the budget. ValueError otherwise.
    Other settings in the file (project tags) are kept."""
    path = budget_path(db_path)
    if value is not None and not _valid(value):
        raise ValueError("monthly_usd must be a finite number greater than 0")
    with _locked(path):
        d = _read(path, strict=True)
        if value is None:
            d.pop("monthly_usd", None)
        else:
            d["monthly_usd"] = float(value)
        _write(path, d)
    return None if value is None else float(value)


def load_tags(db_path=None):
    """{project: tag}: one free-text display label per project, set by the user."""
    t = _read(budget_path(db_path)).get("project_tags")
    return {k: v for k, v in t.items() if isinstance(k, str) and isinstance(v, str) and v} if isinstance(t, dict) else {}


def set_tag(project, tag, db_path=None):
    """Set (or, with None / blank, clear) a project's display tag. ValueError on a bad tag."""
    if not isinstance(project, str) or not project:
        raise ValueError("project is required")
    if tag is not None and not isinstance(tag, str):
        raise ValueError("tag must be text")
    tag = (tag or "").strip()
    if len(tag) > TAG_MAX or any(ord(c) < 32 for c in tag):
        raise ValueError(f"tag must be plain text of at most {TAG_MAX} characters")
    path = budget_path(db_path)
    with _locked(path):
        d = _read(path, strict=True)
        tags = d.get("project_tags") if isinstance(d.get("project_tags"), dict) else {}
        if tag:
            tags[project] = tag
        else:
            tags.pop(project, None)
        if tags:
            d["project_tags"] = tags
        else:
            d.pop("project_tags", None)
        _write(path, d)
    return tag or None


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
