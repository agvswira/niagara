"""Agent run tracing. Every step an agent takes is stored so the dashboard can replay its reasoning."""

import contextvars
import time
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from app import clock
from app.models import AgentRun, AgentStep

_current: contextvars.ContextVar[tuple[Session, AgentRun] | None] = contextvars.ContextVar("run", default=None)


@contextmanager
def agent_run(s: Session, agent: str, *, event_id: int | None = None, entity_type: str | None = None,
              entity_id: int | None = None, customer_id: int | None = None):
    run = AgentRun(agent=agent, event_id=event_id, entity_type=entity_type, entity_id=entity_id,
                   customer_id=customer_id, started_at=clock.now(s))
    s.add(run)
    s.flush()
    token = _current.set((s, run))
    try:
        yield run
        if run.status == "RUNNING":
            run.status = "DONE"
    except Exception as exc:
        run.status = "FAILED"
        step("error", f"Gagal: {type(exc).__name__}", {"error": str(exc)[:500]}, ok=False)
        raise
    finally:
        run.ended_at = clock.now(s)
        _current.reset(token)
        s.flush()


def current_run() -> AgentRun | None:
    cur = _current.get()
    return cur[1] if cur else None


def step(kind: str, title: str, detail: Any = None, ok: bool = True, duration_ms: int = 0) -> None:
    cur = _current.get()
    if not cur:
        return
    s, run = cur
    s.add(AgentStep(run_id=run.id, kind=kind, title=title[:200], detail=detail, ok=ok, duration_ms=duration_ms,
                    created_at=clock.now(s)))
    s.flush()


def link(entity_type: str, entity_id: int, customer_id: int | None = None) -> None:
    run = current_run()
    if run:
        run.entity_type, run.entity_id = entity_type, entity_id
        if customer_id:
            run.customer_id = customer_id


def summarize(text: str) -> None:
    run = current_run()
    if run:
        run.summary = text[:500]


def count_llm_call() -> None:
    run = current_run()
    if run:
        run.llm_calls += 1


class Timer:
    def __enter__(self):
        self.t = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = int((time.perf_counter() - self.t) * 1000)
