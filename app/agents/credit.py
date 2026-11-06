"""Credit check at order time: prices from the price list, policy decides ALLOW / HOLD / APPROVAL."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app import events, policy, templates, tracing
from app.agents.common import reply, request_approval, touch
from app.models import Customer, SalesOrder
from app.tools import credit as credit_tools
from app.tools.base import rupiah
from app.tools.orders import price_order, reserve_stock, set_order_status


class CreditState(TypedDict, total=False):
    order_id: int
    notes: list[str]
    snapshot: dict
    decision: dict
    discount: dict


def _s(config) -> Session:
    return config["configurable"]["session"]


def unknown_item_notes(order: SalesOrder) -> list[str]:
    return [f"\"{ln.raw_text}\" belum ada di katalog kami, admin akan menghubungi." for ln in order.lines if not ln.product_id]


def confirm_order(s: Session, order: SalesOrder, notes: list[str], actor: str, send: bool = True) -> bool:
    res = reserve_stock(s, order_id=order.id)
    if not res.ok:
        tracing.step("error", "Stok berubah saat reservasi, kembali ke Fulfillment", {"error": res.message}, ok=False)
        set_order_status(s, order_id=order.id, status="MATCHED", actor=actor)
        events.emit(s, "order.matched", "sales_order", order.id, {"retry": True})
        return False
    set_order_status(s, order_id=order.id, status="CONFIRMED", actor=actor)
    touch(s, order)
    if send:
        customer = s.get(Customer, order.customer_id)
        reply(s, customer, templates.order_confirmation(customer.owner_name, order, notes))
    events.emit(s, "order.confirmed", "sales_order", order.id, {})
    return True


def price(state: CreditState, config) -> CreditState:
    s = _s(config)
    price_order(s, order_id=state["order_id"])
    order = s.get(SalesOrder, state["order_id"])
    customer = s.get(Customer, order.customer_id)
    d = policy.evaluate_discount(s, tier=customer.tier, requested_pct=order.requested_discount_pct)
    if order.requested_discount_pct:
        tracing.step("policy", f"Permintaan diskon: {d.outcome}", d.to_dict(), ok=d.outcome != "DENY")
    return {"discount": d.to_dict()}


def assess(state: CreditState, config) -> CreditState:
    s = _s(config)
    order = s.get(SalesOrder, state["order_id"])
    customer = s.get(Customer, order.customer_id)
    snap = credit_tools.get_credit_snapshot(s, customer_id=customer.id).data
    d = credit_tools.evaluate(s, customer, order.total, snap)
    return {"snapshot": snap, "decision": d.to_dict()}


def route(state: CreditState) -> str:
    outcome = state["decision"]["outcome"]
    if outcome == "ALLOW" and state["discount"]["outcome"] == "APPROVAL":
        return "approval"
    return {"ALLOW": "allow", "HOLD": "hold", "APPROVAL": "approval"}[outcome]


def allow(state: CreditState, config) -> CreditState:
    s = _s(config)
    order = s.get(SalesOrder, state["order_id"])
    notes = list(state.get("notes", [])) + unknown_item_notes(order)
    if state["discount"]["outcome"] == "DENY":
        notes.append("Untuk diskon, harga mengikuti daftar harga toko. Permintaannya sudah kami teruskan ke supervisor.")
        request_approval(s, "discount_note", "sales_order", order.id, " ".join(state["discount"]["reasons"]),
                         {"requested_pct": order.requested_discount_pct})
    if confirm_order(s, order, notes, "credit_agent"):
        tracing.summarize(f"{order.number} dikonfirmasi otomatis, total {rupiah(order.total)}")
    return {}


def hold(state: CreditState, config) -> CreditState:
    s = _s(config)
    order = s.get(SalesOrder, state["order_id"])
    customer = s.get(Customer, order.customer_id)
    reasons = " ".join(state["decision"]["reasons"])
    set_order_status(s, order_id=order.id, status="ON_HOLD_CREDIT", actor="credit_agent", note=reasons)
    overdue = state["snapshot"]["overdue_invoices"] or [{"number": "Total piutang", "amount": state["snapshot"]["outstanding"], "days": 0}]
    reply(s, customer, templates.hold_notice(customer.owner_name, order, overdue), category="order")
    tracing.summarize(f"{order.number} ditahan: {reasons}")
    return {}


def approval(state: CreditState, config) -> CreditState:
    s = _s(config)
    order = s.get(SalesOrder, state["order_id"])
    customer = s.get(Customer, order.customer_id)
    if state["decision"]["outcome"] == "APPROVAL":
        kind, reasons = "credit", state["decision"]["reasons"]
    else:
        kind, reasons = "discount", state["discount"]["reasons"]
    set_order_status(s, order_id=order.id, status="PENDING_APPROVAL", actor="credit_agent", note=" ".join(reasons))
    request_approval(s, kind, "sales_order", order.id, " ".join(reasons),
                     {"notes": state.get("notes", []), "total": order.total,
                      "requested_discount_pct": order.requested_discount_pct})
    reply(s, customer, templates.pending_approval(customer.owner_name, order))
    tracing.summarize(f"{order.number} menunggu supervisor: {' '.join(reasons)}")
    return {}


def _build():
    g = StateGraph(CreditState)
    for n, fn in [("price", price), ("assess", assess), ("allow", allow), ("hold", hold), ("approval", approval)]:
        g.add_node(n, fn)
    g.add_edge(START, "price")
    g.add_edge("price", "assess")
    g.add_conditional_edges("assess", route, ["allow", "hold", "approval"])
    for n in ("allow", "hold", "approval"):
        g.add_edge(n, END)
    return g.compile()


GRAPH = _build()


@events.on("order.stock_checked")
def on_stock_checked(s: Session, ev) -> None:
    order = s.get(SalesOrder, ev.entity_id)
    with tracing.agent_run(s, "credit", event_id=ev.id, entity_type="sales_order", entity_id=order.id,
                           customer_id=order.customer_id):
        GRAPH.invoke({"order_id": order.id, "notes": ev.payload.get("notes", [])}, config={"configurable": {"session": s}})


def release_held_orders(s: Session, customer_id: int) -> list[SalesOrder]:
    """Re-runs the credit policy on held orders after a payment; returns the orders that were released."""
    released = []
    held = s.query(SalesOrder).filter_by(customer_id=customer_id, status="ON_HOLD_CREDIT").order_by(SalesOrder.created_at).all()
    customer = s.get(Customer, customer_id)
    for order in held:
        snap = credit_tools.snapshot(s, customer_id)
        d = credit_tools.evaluate(s, customer, order.total, snap)
        if d.outcome == "ALLOW":
            if confirm_order(s, order, unknown_item_notes(order), "credit_agent", send=False):
                order.hold_reason = None
                released.append(order)
                tracing.step("decision", f"{order.number} dilepas dari tahanan kredit", d.to_dict())
    return released
