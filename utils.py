from __future__ import annotations
from datetime import datetime, timezone

def current_month_str_utc() -> str:
    """YYYY-MM in UTC (aware, aber Rückgabe ist String)."""
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"

def ymd(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def utcnow_naive() -> datetime:
    """UTC jetzt als naive datetime (tzinfo=None), damit alle DB-Zeitstempel konsistent sind."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
