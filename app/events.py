"""Event bus on top of the `events` table (outbox pattern). Agents never call each other; they emit events."""

import threading
import traceback
from typing import Callable

from sqlalchemy.orm import Session

from app import clock, db
from app.models import Event

MAX_ATTEMPTS = 5
_handlers: dict[str, Callable[[Session, Event], None]] = {}
_lock = threading.Lock()


def on(event_type: str):
    def deco(fn):
        _handlers[event_type] = fn
        return fn
    return deco


def emit(s: Session, type_: str, entity_type: str | None = None, entity_id: int | None = None,
         payload: dict | None = None) -> Event:
    ev = Event(type=type_, entity_type=entity_type, entity_id=entity_id, payload=payload or {}, created_at=clock.now(s))
    s.add(ev)
    s.flush()
    return ev


def drain(max_events: int = 200) -> int:
    """Processes pending events until the queue is empty. Each event runs in its own transaction."""
    from app import agents  # noqa: F401  registers handlers

    processed = 0
    with _lock:
        tried: set[int] = set()
        while processed < max_events:
            with db.session_scope() as s:
                q = s.query(Event).filter(Event.status == "PENDING", Event.attempts < MAX_ATTEMPTS)
                if tried:
                    q = q.filter(Event.id.notin_(tried))
                ev = q.order_by(Event.id).first()
                if ev is None:
                    break
                ev_id = ev.id
            tried.add(ev_id)
            try:
                with db.session_scope() as s:
                    ev = s.get(Event, ev_id)
                    handler = _handlers.get(ev.type)
                    if handler is not None:
                        handler(s, ev)
                    ev.status = "DONE" if handler else "IGNORED"
            except Exception as exc:
                with db.session_scope() as s:
                    ev = s.get(Event, ev_id)
                    ev.attempts += 1
                    ev.last_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
                    if ev.attempts >= MAX_ATTEMPTS:
                        ev.status = "DEAD"
            processed += 1
    return processed
