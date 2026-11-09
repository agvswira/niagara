"""Login, role permissions, and personal-data masking at the API boundary."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import db

db.configure("sqlite:///:memory:")

from app import auth, events, sim  # noqa: E402
from app.api import app  # noqa: E402
from app.llm.client import LLMService, set_llm  # noqa: E402
from app.models import Approval  # noqa: E402
from app.privacy import redact  # noqa: E402
from app.seed import reset_and_seed  # noqa: E402

PASSWORD = "niagara-demo"


@pytest.fixture()
def client(monkeypatch):
    import app.api as api_module

    # Run agent work inline so no background thread outlives the test and touches the next test's database.
    monkeypatch.setattr(api_module, "drain_bg", events.drain)
    monkeypatch.setenv("NIAGARA_ROLE_ACCOUNTS", "1")  # these tests check role separation, so create all accounts
    set_llm(LLMService("heuristic"))
    reset_and_seed(datetime(2026, 11, 9, 8, 0))
    auth._failures.clear()
    with TestClient(app) as c:
        yield c


def login(c: TestClient, username: str) -> None:
    r = c.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text


def test_api_requires_login(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/orders").status_code == 401


def test_wrong_password_is_rejected_and_locks_after_repeats(client):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "salah"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 429


def test_only_supervisor_changes_policy(client):
    login(client, "admin")
    r = client.put("/api/policies/credit.max_overdue_days", json={"value": 20})
    assert r.status_code == 403
    login(client, "supervisor")
    assert client.put("/api/policies/credit.max_overdue_days", json={"value": 20}).status_code == 200


def test_approval_queue_is_filtered_by_role(client):
    with db.session_scope() as s:
        sim.inbound(s, 9, "gulaku 2 karung")  # Toko Anugrah is level 0, so this becomes an order_review card
        from app.tools.payments import import_bank_mutations
        res = import_bank_mutations(s, rows=[{"bank": "BRI", "date": "2026-11-09", "amount": 999_000,
                                              "description": "SETORAN TUNAI", "ref": "X1"}]).data
        events.emit(s, "bank.mutations_imported", payload={"tx_ids": res["created"]})
    events.drain()
    login(client, "admin")
    kinds = {a["kind"] for a in client.get("/api/approvals").json()}
    assert "order_review" in kinds and "unmatched_payment" not in kinds
    login(client, "ar")
    kinds = {a["kind"] for a in client.get("/api/approvals").json()}
    assert "unmatched_payment" in kinds and "order_review" not in kinds
    with db.session_scope() as s:
        review_id = s.query(Approval).filter_by(kind="order_review").first().id
    assert client.post(f"/api/approvals/{review_id}/decide", json={"decision": "approve"}).status_code == 403


def test_decision_is_recorded_under_the_signed_in_user(client):
    with db.session_scope() as s:
        sim.inbound(s, 9, "gulaku 2 karung")
    events.drain()
    login(client, "admin")
    aid = client.get("/api/approvals").json()[0]["id"]
    assert client.post(f"/api/approvals/{aid}/decide", json={"decision": "approve", "by": "orang lain"}).status_code == 200
    with db.session_scope() as s:
        assert s.get(Approval, aid).decided_by == "admin"


def test_redact_masks_contact_and_identity_data():
    text = "Pak Budi, hubungi 081234567890 atau budi@toko.id, rekening 1234567890, NIK 3507010101010001"
    masked, found = redact(text, ["Budi"])
    for secret in ("081234567890", "budi@toko.id", "1234567890", "3507010101010001", "Budi"):
        assert secret not in masked
    assert found == {"email": 1, "telepon": 1, "nik": 1, "rekening": 1, "nama": 1}
    assert redact("indomie goreng 2 dus Rp248.000", [])[1] == {}
