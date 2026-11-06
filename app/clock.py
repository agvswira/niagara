"""Simulated business clock (WIB, naive datetimes) so the time machine can fast-forward collection cycles."""

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import KV


def now(s: Session) -> datetime:
    row = s.get(KV, "clock")
    if row is None:
        return datetime.now().replace(microsecond=0)
    return datetime.fromisoformat(row.value)


def today(s: Session) -> date:
    return now(s).date()


def set_now(s: Session, value: datetime) -> None:
    row = s.get(KV, "clock")
    if row is None:
        s.add(KV(key="clock", value=value.isoformat()))
    else:
        row.value = value.isoformat()
    s.flush()


def advance(s: Session, days: int = 0, hours: int = 0) -> datetime:
    new = now(s) + timedelta(days=days, hours=hours)
    set_now(s, new)
    return new
