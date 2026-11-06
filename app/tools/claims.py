"""Delivery claims and credit notes. Money moves only after a person approves the claim."""

from sqlalchemy.orm import Session

from app import clock, policy
from app.models import Claim, CreditNote, Customer, Invoice, SalesOrder, SalesOrderLine, Stock
from app.tools.base import ToolError, tool
from app.tools.orders import audit

KIND_LABEL = {"kurang": "kurang kirim", "rusak": "barang rusak", "ditolak": "ditolak toko"}


def recent_delivered_order(s: Session, customer_id: int) -> SalesOrder | None:
    return (s.query(SalesOrder).filter(SalesOrder.customer_id == customer_id, SalesOrder.is_history.is_(False),
                                       SalesOrder.status.in_(["INVOICED", "DELIVERED"]))
            .order_by(SalesOrder.delivery_date.desc(), SalesOrder.id.desc()).first())


@tool("create_claim", "write")
def create_claim(s: Session, order_id: int, kind: str, source: str, line_id: int | None = None,
                 qty_base: int | None = None, note: str = "") -> dict:
    order = s.get(SalesOrder, order_id)
    if order is None or order.status not in ("INVOICED", "DELIVERED"):
        raise ToolError("not_delivered", "Klaim hanya untuk order yang sudah dikirim.")
    max_days = policy.get(s, "claims.max_days_after_delivery")
    age = (clock.today(s) - order.delivery_date).days if order.delivery_date else 0
    if age > max_days and kind != "ditolak":
        raise ToolError("too_late", f"Order dikirim {age} hari lalu, batas klaim {max_days} hari.")
    if kind == "ditolak":
        amount, product_id, qty = order.total, None, 0
    else:
        line = s.get(SalesOrderLine, line_id) if line_id else None
        if line is None or line.order_id != order.id or not line.product_id:
            raise ToolError("no_line", "Barang yang dikeluhkan tidak ada di order ini.")
        claimed = sum(c.qty_base for c in s.query(Claim).filter(Claim.order_id == order.id, Claim.product_id == line.product_id,
                                                                 Claim.status.in_(["OPEN", "APPROVED"])))
        qty = min(qty_base or line.qty_base, line.qty_base - claimed)
        if qty <= 0:
            raise ToolError("already_claimed", "Seluruh jumlah barang ini sudah diklaim.")
        amount, product_id = qty * line.price, line.product_id
    c = Claim(order_id=order.id, customer_id=order.customer_id, kind=kind, product_id=product_id, qty_base=qty,
              amount=amount, source=source, note=note[:500], created_at=clock.now(s))
    s.add(c)
    s.flush()
    return {"claim_id": c.id, "amount": amount, "qty_base": qty, "order": order.number}


def _invoice_for(s: Session, order_id: int) -> Invoice | None:
    return s.query(Invoice).filter_by(order_id=order_id).first()


@tool("issue_credit_note", "financial")
def issue_credit_note(s: Session, claim_id: int, actor: str) -> dict:
    """Reduces the order's invoice first; anything already paid becomes saldo titipan. Stock follows the claim kind."""
    c = s.get(Claim, claim_id)
    if c.status != "OPEN":
        raise ToolError("not_open", "Klaim sudah diproses.")
    order = s.get(SalesOrder, c.order_id)
    inv = _invoice_for(s, order.id)
    to_invoice = 0
    if inv is not None:
        unpaid = max(0, inv.amount - inv.paid_amount)
        to_invoice = min(unpaid, c.amount)
        before = {"amount": inv.amount, "status": inv.status}
        inv.amount -= to_invoice
        if c.kind == "ditolak" and inv.paid_amount == 0 and inv.amount == 0:
            inv.status = "VOID"
        elif inv.paid_amount >= inv.amount:
            inv.status = "PAID"
        audit(s, actor, "credit_note", "invoice", inv.id, before=before, after={"amount": inv.amount, "status": inv.status})
    to_balance = c.amount - to_invoice
    customer = s.get(Customer, c.customer_id)
    if to_balance > 0:
        customer.credit_balance += to_balance
    # Short or refused goods never left our hands, so they go back on the shelf. Damaged goods are written off.
    if c.kind == "kurang":
        st = s.query(Stock).filter_by(product_id=c.product_id).first()
        st.qty_on_hand += c.qty_base
    elif c.kind == "ditolak":
        for ln in order.lines:
            if ln.product_id:
                s.query(Stock).filter_by(product_id=ln.product_id).first().qty_on_hand += ln.qty_base
        order.status = "RETURNED"
    n = s.query(CreditNote).count() + 1
    cn = CreditNote(number=f"CN-{clock.now(s):%y%m%d}-{n:04d}", customer_id=c.customer_id, claim_id=c.id,
                    invoice_id=inv.id if inv else None, amount=c.amount, to_invoice=to_invoice, to_balance=to_balance,
                    created_at=clock.now(s))
    s.add(cn)
    c.status = "APPROVED"
    s.flush()
    return {"credit_note": cn.number, "amount": c.amount, "to_invoice": to_invoice, "to_balance": to_balance,
            "invoice": inv.number if inv else None}
