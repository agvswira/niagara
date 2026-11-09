"""Dashboard login, signed session cookies, and role permissions."""

import base64
import hashlib
import hmac
import os
import secrets
import time

from sqlalchemy.orm import Session

from app.models import KV, User

COOKIE = "niagara_session"
SESSION_SECONDS = 12 * 3600
ROLES = {
    "admin_order": "Admin order",
    "staf_ar": "Staf AR",
    "supervisor": "Supervisor",
}
# Which approval cards each role may decide. Supervisor may decide everything.
APPROVALS_BY_ROLE = {
    "admin_order": {"order_review", "unknown_item", "complaint", "handoff", "discount_note", "retur"},
    "staf_ar": {"unmatched_payment", "escalation", "promise_review", "klaim_tunai"},
}
DEMO_USERS = [("admin", "Admin order", "admin_order"), ("ar", "Staf AR", "staf_ar"), ("supervisor", "Supervisor", "supervisor")]

_failures: dict[str, list[float]] = {}
MAX_FAILURES, LOCK_SECONDS = 5, 300


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return hmac.compare_digest(check.hex(), digest_hex)


def seed_users(s: Session) -> None:
    """The first release ships one supervisor account so a demo stays about the agents.

    Role separation (admin order, AR staff) is fully built; NIAGARA_ROLE_ACCOUNTS=1 also creates those accounts.
    """
    password = os.getenv("NIAGARA_DEMO_PASSWORD", "niagara-demo")
    with_roles = os.getenv("NIAGARA_ROLE_ACCOUNTS", "") in ("1", "true", "yes")
    for username, name, role in DEMO_USERS:
        if role != "supervisor" and not with_roles:
            continue
        if not s.query(User).filter_by(username=username).first():
            s.add(User(username=username, display_name=name, role=role, password_hash=hash_password(password)))
    s.flush()


def _secret(s: Session) -> bytes:
    env = os.getenv("NIAGARA_SECRET")
    if env:
        return env.encode()
    row = s.get(KV, "session_secret")
    if row is None:
        row = KV(key="session_secret", value=secrets.token_hex(32))
        s.add(row)
        s.flush()
    return row.value.encode()


def make_token(s: Session, username: str) -> str:
    payload = f"{username}|{int(time.time()) + SESSION_SECONDS}"
    sig = hmac.new(_secret(s), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def read_token(s: Session, token: str | None) -> User | None:
    if not token:
        return None
    try:
        username, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except Exception:
        return None
    expected = hmac.new(_secret(s), f"{username}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected) or int(exp) < time.time():
        return None
    return s.query(User).filter_by(username=username).first()


def locked(username: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(username, []) if now - t < LOCK_SECONDS]
    _failures[username] = recent
    return len(recent) >= MAX_FAILURES


def record_failure(username: str) -> None:
    _failures.setdefault(username, []).append(time.time())


def clear_failures(username: str) -> None:
    _failures.pop(username, None)


def can_decide(role: str, approval_kind: str) -> bool:
    return role == "supervisor" or approval_kind in APPROVALS_BY_ROLE.get(role, set())
