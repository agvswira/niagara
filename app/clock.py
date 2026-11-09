"""Simulated business clock (WIB, naive datetimes) so the time machine can fast-forward collection cycles.

Between jumps the simulated clock runs at real speed, so a demo shows 08.03, 08.07 and so on instead of every
message stamped 08.00. It stops at closing time and waits for the next "advance one day".
"""

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import KV

CLOSING_HOUR = 20


def _get(s: Session, key: str) -> str | None:
    row = s.get(KV, key)
    return row.value if row is not None else None


def _put(s: Session, key: str, value: str) -> None:
    row = s.get(KV, key)
    if row is None:
        s.add(KV(key=key, value=value))
    else:
        row.value = value


def now(s: Session) -> datetime:
    value = _get(s, "clock")
    if value is None:
        return datetime.now().replace(microsecond=0)
    base = datetime.fromisoformat(value)
    anchor = _get(s, "clock_anchor")
    if anchor is None:
        return base
    elapsed = datetime.now() - datetime.fromisoformat(anchor)
    closing = base.replace(hour=CLOSING_HOUR, minute=0, second=0)
    ticking = base + max(elapsed, timedelta(0))
    return (ticking if base >= closing else min(ticking, closing)).replace(microsecond=0)


def today(s: Session) -> date:
    return now(s).date()


def set_now(s: Session, value: datetime) -> None:
    _put(s, "clock", value.isoformat())
    _put(s, "clock_anchor", datetime.now().isoformat())
    s.flush()


def advance(s: Session, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
    new = now(s) + timedelta(days=days, hours=hours, minutes=minutes)
    set_now(s, new)
    return new
