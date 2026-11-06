from datetime import datetime

from sqlalchemy.orm import Session

from app import clock, tracing
from app.models import Approval, Customer, SalesOrder
from app.tools.messaging import send_message

OPEN_ORDER_STATES = ("RECEIVED", "NEEDS_CLARIFICATION", "PENDING_REVIEW", "MATCHED", "SUBSTITUTION_OFFERED",
                     "STOCK_CHECKED", "ON_HOLD_CREDIT", "PENDING_APPROVAL", "CONFIRMED", "BACKORDERED")
WAITING_STATES = ("NEEDS_CLARIFICATION", "SUBSTITUTION_OFFERED")


def reply(s: Session, customer: Customer, text: str, category: str = "order") -> bool:
    res = send_message(s, customer_id=customer.id, text=text, category=category)
    if res.ok:
        tracing.step("message", "Pesan terkirim ke pelanggan", {"text": text, "category": category})
    return res.ok


def request_approval(s: Session, kind: str, entity_type: str, entity_id: int, reason: str, payload: dict | None = None) -> Approval:
    existing = (s.query(Approval).filter_by(kind=kind, entity_type=entity_type, entity_id=entity_id, status="PENDING").first())
    if existing:
        return existing
    a = Approval(kind=kind, entity_type=entity_type, entity_id=entity_id, reason=reason, payload=payload or {},
                 created_at=clock.now(s))
    s.add(a)
    s.flush()
    tracing.step("decision", f"Minta persetujuan manusia: {kind}", {"reason": reason, "approval_id": a.id})
    return a


def latest_open_order(s: Session, customer_id: int, states=OPEN_ORDER_STATES) -> SalesOrder | None:
    return (s.query(SalesOrder).filter(SalesOrder.customer_id == customer_id, SalesOrder.status.in_(states),
                                       SalesOrder.is_history.is_(False))
            .order_by(SalesOrder.created_at.desc()).first())


def touch(s: Session, order: SalesOrder) -> None:
    order.updated_at: datetime = clock.now(s)
