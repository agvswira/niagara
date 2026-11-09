"""Caps on the demo endpoints that spend LLM quota.

The live demo is public, so anyone with the link can send simulated messages. Each one costs a model call
on the free Groq and Gemini quotas. These caps keep one visitor, or all of them together, from using it up
before the judges arrive. Limits come from the environment so a private deployment can lift them
(0 turns a cap off).
"""

import os
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

WINDOW_VISITOR = 10 * 60
WINDOW_GLOBAL = 60 * 60

_lock = threading.Lock()
_visitors: dict[str, deque] = {}
_everyone: deque = deque()


def _cap(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def client_ip(request: Request) -> str:
    # Render and most hosts put the visitor first in X-Forwarded-For
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _trim(q: deque, now: float, window: int) -> None:
    while q and now - q[0] >= window:
        q.popleft()


def _wait_minutes(q: deque, now: float, window: int) -> int:
    return max(1, int((window - (now - q[0])) // 60) + 1)


def check(request: Request) -> None:
    """FastAPI dependency: count one LLM-backed message, or refuse it with 429."""
    per_visitor = _cap("DEMO_MESSAGES_PER_VISITOR", 40)
    per_hour = _cap("DEMO_MESSAGES_PER_HOUR", 300)
    now = time.monotonic()
    ip = client_ip(request)
    with _lock:
        mine = _visitors.setdefault(ip, deque())
        _trim(mine, now, WINDOW_VISITOR)
        _trim(_everyone, now, WINDOW_GLOBAL)
        if per_visitor and len(mine) >= per_visitor:
            minutes = _wait_minutes(mine, now, WINDOW_VISITOR)
            raise HTTPException(429, f"Batas pesan demo tercapai. Coba lagi dalam {minutes} menit.")
        if per_hour and len(_everyone) >= per_hour:
            minutes = _wait_minutes(_everyone, now, WINDOW_GLOBAL)
            raise HTTPException(429, f"Demo sedang ramai. Coba lagi dalam {minutes} menit.")
        mine.append(now)
        _everyone.append(now)
        if len(_visitors) > 5000:  # drop idle visitors so the table cannot grow without bound
            for key, q in list(_visitors.items()):
                _trim(q, now, WINDOW_VISITOR)
                if not q:
                    del _visitors[key]


def reset() -> None:
    with _lock:
        _visitors.clear()
        _everyone.clear()
