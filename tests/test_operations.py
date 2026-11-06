"""Priority-2 operations: backorders, delivery claims, saldo titipan, cash and giro, new contacts, stale questions."""

from datetime import datetime

import pytest

from app import db

db.configure("sqlite:///:memory:")

from app import clock, events, onboarding, sim  # noqa: E402
from app.llm.client import LLMService, set_llm  # noqa: E402
from app.models import (Approval, BankTransaction, Claim, Conversation, CreditNote, Customer, Invoice,  # noqa: E402
                        Message, SalesOrder, Stock)
from app.seed import reset_and_seed  # noqa: E402
from app.tools.credit import snapshot  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    set_llm(LLMService("heuristic"))
    reset_and_seed(datetime(2026, 11, 9, 8, 0))
    yield


def say(cid: int, text: str) -> None:
    with db.session_scope() as s:
        sim.inbound(s, cid, text)
    events.drain()


def replies(cid: int) -> list[str]:
    with db.session_scope() as s:
        conv = s.query(Conversation).filter_by(customer_id=cid).first()
        return [m.text for m in s.query(Message).filter_by(conversation_id=conv.id, direction="out").order_by(Message.id)]


def decide(kind: str, approve: bool = True, **payload) -> None:
    with db.session_scope() as s:
        a = s.query(Approval).filter_by(kind=kind, status="PENDING").order_by(Approval.id.desc()).first()
        assert a is not None, f"no pending {kind}"
        a.payload = {**(a.payload or {}), **payload}
        a.status, a.decided_by = ("APPROVED" if approve else "REJECTED"), "tester"
        events.emit(s, "approval.decided", "approval", a.id, {})
    events.drain()


def advance(days: int) -> None:
    with db.session_scope() as s:
        sim.advance_days(s, days)
    events.drain()


def test_backorder_becomes_follow_up_order_when_stock_arrives():
    with db.session_scope() as s:
        s.query(Stock).filter_by(product_id=1).one().qty_on_hand = 40  # one dus left
    say(1, "indomie goreng 3 dus")
    assert "Mau diganti" in replies(1)[-1]
    say(1, "tidak")  # decline the substitute, so the shortfall becomes a backorder
    with db.session_scope() as s:
        o = s.query(SalesOrder).filter_by(customer_id=1, is_history=False).one()
        assert o.lines[0].backorder_base == 80
    with db.session_scope() as s:
        from app.tools.orders import receive_stock
        receive_stock(s, product_id=1, qty_base=400, ref="GR1", actor="tester")
        events.emit(s, "stock.received", "product", 1, {})
    events.drain()
    with db.session_scope() as s:
        child = s.query(SalesOrder).filter(SalesOrder.parent_order_id.isnot(None)).one()
        assert child.status == "CONFIRMED" and child.lines[0].qty_base == 80
    assert "susulan" in replies(1)[-1]


def test_order_with_nothing_in_stock_waits_instead_of_confirming():
    say(13, "minyak fortune 2 liter 2 dus")  # Toko Lestari does not accept substitutes
    with db.session_scope() as s:
        o = s.query(SalesOrder).filter_by(customer_id=13, is_history=False).one()
        assert o.status == "BACKORDERED" and o.delivery_date is None
    assert "sedang kosong" in replies(13)[-1]
    with db.session_scope() as s:
        from app.tools.orders import receive_stock
        receive_stock(s, product_id=16, qty_base=60, ref="GR2", actor="tester")
        events.emit(s, "stock.received", "product", 16, {})
    events.drain()
    assert "susulan" in replies(13)[-1]


def test_customer_reports_short_delivery_and_gets_credit_note():
    say(1, "indomie goreng 2 dus, aqua tgg 3 dus")
    advance(3)
    with db.session_scope() as s:
        inv = s.query(Invoice).join(SalesOrder, Invoice.order_id == SalesOrder.id).filter(SalesOrder.customer_id == 1,
                                                                                           SalesOrder.is_history.is_(False)).one()
        before, stock_before = inv.amount, s.query(Stock).filter_by(product_id=1).one().qty_on_hand
    say(1, "mas indomie goreng kurang 1 dus")
    assert "sudah kami catat" in replies(1)[-1]
    decide("retur")
    with db.session_scope() as s:
        cn = s.query(CreditNote).one()
        inv = s.get(Invoice, cn.invoice_id)
        unit_price = next(ln.price for ln in s.get(SalesOrder, inv.order_id).lines if ln.product_id == 1)
        assert cn.amount == 40 * unit_price and inv.amount == before - cn.amount
        assert s.query(Stock).filter_by(product_id=1).one().qty_on_hand == stock_before + 40
    assert "Nota kredit" in replies(1)[-1]


def test_damaged_goods_are_written_off_not_restocked():
    say(1, "aqua tgg 3 dus")
    advance(3)
    with db.session_scope() as s:
        stock_before = s.query(Stock).filter_by(product_id=7).one().qty_on_hand
    say(1, "aqua tgg 2 dus pecah")
    decide("retur")
    with db.session_scope() as s:
        assert s.query(Stock).filter_by(product_id=7).one().qty_on_hand == stock_before
        assert s.query(Claim).one().status == "APPROVED"


def test_refused_order_voids_invoice_and_returns_stock():
    from app.tools.claims import create_claim

    say(1, "gulaku 2 karung")
    advance(3)
    with db.session_scope() as s:
        o = s.query(SalesOrder).filter_by(customer_id=1, is_history=False).one()
        stock_before = s.query(Stock).filter_by(product_id=17).one().qty_on_hand
        res = create_claim(s, order_id=o.id, kind="ditolak", source="sopir")
        from app.agents.common import request_approval
        from app import tracing  # noqa: F401
        request_approval(s, "retur", "claim", res.data["claim_id"], "ditolak", {})
    decide("retur")
    with db.session_scope() as s:
        o = s.query(SalesOrder).filter_by(customer_id=1, is_history=False).one()
        assert o.status == "RETURNED"
        assert s.query(Invoice).filter_by(order_id=o.id).one().status == "VOID"
        assert s.query(Stock).filter_by(product_id=17).one().qty_on_hand == stock_before + 40


def test_overpayment_becomes_balance_and_pays_next_invoice():
    with db.session_scope() as s:
        outstanding = snapshot(s, 1)["outstanding"]
        sim.customer_pays(s, 1, outstanding + 300_000)
    events.drain()
    with db.session_scope() as s:
        sim.import_feed(s)
    events.drain()
    with db.session_scope() as s:
        assert s.get(Customer, 1).credit_balance == 300_000
    assert "saldo titipan" in replies(1)[-1]
    say(1, "indomie goreng 1 dus")
    advance(3)
    with db.session_scope() as s:
        inv = s.query(Invoice).join(SalesOrder, Invoice.order_id == SalesOrder.id).filter(
            SalesOrder.customer_id == 1, SalesOrder.is_history.is_(False)).one()
        assert inv.status == "PAID"
        assert s.get(Customer, 1).credit_balance == 300_000 - inv.amount


def test_cash_claim_from_chat_is_confirmed_by_ar():
    say(6, "sudah saya titip ke sales 2 juta ya")
    assert "setoran sales" in replies(6)[-1]
    decide("klaim_tunai", confirmed_amount=2_000_000)
    with db.session_scope() as s:
        tx = s.query(BankTransaction).filter_by(kind="tunai").one()
        assert tx.status == "MATCHED" and tx.customer_id == 6


def test_giro_clears_on_due_date_and_can_bounce():
    from app.tools.payments import bounce_payment, record_manual_payment

    with db.session_scope() as s:
        amount = s.query(Invoice).filter(Invoice.customer_id == 6, Invoice.status == "OVERDUE").order_by(Invoice.due_date).first().amount
        res = record_manual_payment(s, customer_id=6, kind="giro", amount=amount, actor="ar", giro_due="2026-11-11")
        tx_id = res.data["tx_id"]
        assert res.data["status"] == "GIRO_PENDING"
    advance(1)
    with db.session_scope() as s:
        assert s.get(BankTransaction, tx_id).status == "GIRO_PENDING"
    advance(1)
    with db.session_scope() as s:
        assert s.get(BankTransaction, tx_id).status == "MATCHED"
        res = bounce_payment(s, tx_id=tx_id, actor="ar")
        assert res.ok and res.data["reopened"]
        events.emit(s, "payment.bounced", "bank_transaction", tx_id, res.data)
    events.drain()
    assert "ditolak bank" in replies(6)[-1]


def test_unknown_number_is_registered_and_verified():
    with db.session_scope() as s:
        c = onboarding.inbound_from_unknown(s, "simulator", "081299990000", "indomie goreng 2 dus")
        cid = c.id
    events.drain()
    texts = replies(cid)
    assert any("belum terdaftar" in t for t in texts)
    with db.session_scope() as s:
        assert s.query(Approval).filter_by(kind="pelanggan_baru", entity_id=cid).count() == 1
        assert s.query(SalesOrder).filter_by(customer_id=cid).one().status == "PENDING_REVIEW"
    decide("pelanggan_baru", fields={"name": "Toko Contoh Baru", "owner_name": "Bu Contoh", "area": "Klojen",
                                     "tier": "C", "credit_limit": 3_000_000})
    with db.session_scope() as s:
        c = s.get(Customer, cid)
        assert c.verified and c.name == "Toko Contoh Baru"
    assert "sudah terdaftar" in replies(cid)[-1]


def test_old_question_is_not_answered_by_a_new_number_and_gets_closed():
    say(8, "aqua 4 dus")
    with db.session_scope() as s:
        clock.advance(s, hours=30)
    say(8, "2 dus indomie soto")
    with db.session_scope() as s:
        orders = s.query(SalesOrder).filter_by(customer_id=8, is_history=False).order_by(SalesOrder.id).all()
        assert len(orders) == 2 and orders[0].status == "NEEDS_CLARIFICATION"
    advance(2)
    with db.session_scope() as s:
        first = s.query(SalesOrder).filter_by(customer_id=8, is_history=False).order_by(SalesOrder.id).first()
        assert first.status == "CANCELLED"
    assert any("belum ada jawaban" in t for t in replies(8))
