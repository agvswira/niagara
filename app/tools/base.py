"""Tool contract shared by every agent: typed result, risk class, automatic trace logging."""

import functools
from dataclasses import dataclass, field
from typing import Any, Callable

from app import tracing

RISK_CLASSES = ("read", "write", "message", "financial")


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error_code: str | None = None
    retryable: bool = False
    message: str = ""
    meta: dict = field(default_factory=dict)


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def _brief(value: Any, limit: int = 600) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return {k: _brief(v, 200) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple, set)):
        return [_brief(v, 200) for v in list(value)[:12]]
    return str(value)[:limit]


def tool(name: str, risk: str, retries: int = 2):
    """Wraps `fn(session, **kwargs) -> data`. Retries only errors marked retryable."""
    assert risk in RISK_CLASSES

    def deco(fn: Callable):
        @functools.wraps(fn)
        def wrapper(s, **kwargs) -> ToolResult:
            attempt = 0
            while True:
                attempt += 1
                with tracing.Timer() as t:
                    try:
                        data = fn(s, **kwargs)
                        res = ToolResult(ok=True, data=data)
                    except ToolError as e:
                        res = ToolResult(ok=False, error_code=e.code, retryable=e.retryable, message=str(e))
                tracing.step("tool", f"{name}", {"risk": risk, "input": _brief(kwargs), "output": _brief(res.data),
                                                 "error": res.message or None, "attempt": attempt},
                             ok=res.ok, duration_ms=t.ms)
                if res.ok or not res.retryable or attempt > retries:
                    return res

        wrapper.tool_name = name
        wrapper.risk = risk
        return wrapper

    return deco


def rupiah(n: int | float) -> str:
    return "Rp" + f"{int(round(n)):,}".replace(",", ".")
