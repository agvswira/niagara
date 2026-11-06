"""Masks personal data before customer text leaves for an external LLM. Only counts are logged, never the values."""

import re

PATTERNS = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("telepon", re.compile(r"(?<!\d)(?:\+62|62|0)8\d{7,11}(?!\d)")),
    ("nik", re.compile(r"(?<!\d)\d{16}(?!\d)")),
    ("rekening", re.compile(r"(?<!\d)\d{10,15}(?!\d)")),
]


def redact(text: str, names: list[str] | None = None) -> tuple[str, dict[str, int]]:
    found: dict[str, int] = {}
    out = text or ""
    for label, rx in PATTERNS:
        out, n = rx.subn(f"[{label.upper()}]", out)
        if n:
            found[label] = found.get(label, 0) + n
    for name in sorted({n for n in (names or []) if n and len(n) >= 3}, key=len, reverse=True):
        rx = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        out, n = rx.subn("[NAMA]", out)
        if n:
            found["nama"] = found.get("nama", 0) + n
    return out, found
