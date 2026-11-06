"""Supervisor Agent: morning brief, human decisions coming back from the approval queue, and the autonomy ladder."""

from datetime import timedelta

from sqlalchemy.orm import Session

from app import clock, events, policy, templates, tracing
from app.agents.common import reply
from app.agents.credit import confirm_order, unknown_item_notes
from app.models import (KV, Approval, BankTransaction, Customer, Invoice, PromiseToPay, SalesOrder, SalesOrderLine,
                        Stock, Product)
from app.tools.base import rupiah
from app.tools.credit import UNPAID
from app.tools.orders import set_order_status
from app.tools.payments import allocate_payment, plan_allocation

MINUTES_PER_ORDER = 4  # manual handling time assumption, shown as an assumption in the dashboard
MINUTES_PER_PAYMENT = 3


def build_brief(s: Session) -> dict:
    today = clock.today(s)
    start = clock.now(s).replace(hour=0, minute=0, second=0) - timedelta(days=1)
    live = s.query(SalesOrder).filter(SalesOrder.is_history.is_(False))
    recent = live.filter(SalesOrder.created_at >= start).all()
    auto = [o for o in recent if o.auto_processed and not o.was_edited]
    waiting = {st: live.filter(SalesOrder.status == st).count()
               for st in ("NEEDS_CLARIFICATION", "SUBSTITUTION_OFFERED", "PENDING_REVIEW", "PENDING_APPROVAL", "ON_HOLD_CREDIT")}
    held_total = sum(o.total for o in live.filter(SalesOrder.status == "ON_HOLD_CREDIT"))
    approvals = {}
    for a in s.query(Approval).filter_by(status="PENDING"):
        approvals[a.kind] = approvals.get(a.kind, 0) + 1
    unpaid = s.query(Invoice).filter(Invoice.status.in_(UNPAID)).all()
    due_today = sum(i.amount - i.paid_amount for i in unpaid if i.due_date == today)
    overdue = sum(i.amount - i.paid_amount for i in unpaid if i.due_date < today)
    promises_today = s.query(PromiseToPay).filter_by(promised_date=today, status="OPEN").all()
    unmatched = s.query(BankTransaction).filter_by(status="UNMATCHED").all()
    matched_recent = s.query(BankTransaction).filter(BankTransaction.status == "MATCHED",
                                                     BankTransaction.tx_date >= today - timedelta(days=1)).count()
    low_stock = []
    for st in s.query(Stock).all():
        free = st.qty_on_hand - st.qty_reserved
        backordered = sum(ln.backorder_base for ln in s.query(SalesOrderLine).filter_by(product_id=st.product_id))
        if free <= 0 or backordered > 0:
            low_stock.append({"product": s.get(Product, st.product_id).name, "tersedia": free, "backorder": backordered})
    risky = []
    for c in s.query(Customer).all():
        late = [i for i in unpaid if i.customer_id == c.id and (today - i.due_date).days > 14]
        if c.payment_score < 0.7 or late:
            risky.append({"customer": c.name, "skor_bayar": round(c.payment_score, 2),
                          "telat_14_hari": rupiah(sum(i.amount - i.paid_amount for i in late))})
    minutes = len(auto) * MINUTES_PER_ORDER + matched_recent * MINUTES_PER_PAYMENT
    return {
        "tanggal": today.isoformat(),
        "hari": templates.tanggal(today),
        "order_24_jam": len(recent),
        "order_otomatis": len(auto),
        "persen_otomatis": round(100 * len(auto) / len(recent)) if recent else None,
        "menunggu": waiting,
        "nilai_ditahan": held_total,
        "approval_menunggu": approvals,
        "jatuh_tempo_hari_ini": due_today,
        "total_lewat_jatuh_tempo": overdue,
        "janji_bayar_hari_ini": [{"customer": s.get(Customer, p.customer_id).name, "nominal": p.amount} for p in promises_today],
        "mutasi_belum_cocok": {"jumlah": len(unmatched), "nominal": sum(t.amount for t in unmatched)},
        "stok_perlu_perhatian": low_stock[:8],
        "pelanggan_berisiko": risky[:8],
        "estimasi_menit_dihemat": minutes,
        "asumsi": f"{MINUTES_PER_ORDER} menit per order manual, {MINUTES_PER_PAYMENT} menit per pencocokan transfer manual.",
    }


@events.on("schedule.brief")
def on_brief(s: Session, ev) -> None:
    with tracing.agent_run(s, "supervisor", event_id=ev.id):
        brief = build_brief(s)
        brief["penagihan"] = ev.payload.get("collection", {})
        row = s.get(KV, "brief:latest")
        if row is None:
            s.add(KV(key="brief:latest", value=brief))
        else:
            row.value = brief
        tracing.step("think", "Brief pagi disusun", brief)
        tracing.summarize(f"Brief {brief['hari']}: {sum(brief['approval_menunggu'].values())} approval menunggu,"
                          f" lewat jatuh tempo {rupiah(brief['total_lewat_jatuh_tempo'])}")


def recompute_autonomy(s: Session, customer: Customer) -> None:
    orders = (s.query(SalesOrder).filter(SalesOrder.customer_id == customer.id, SalesOrder.is_history.is_(False),
                                         SalesOrder.status.notin_(["CANCELLED"]))
              .order_by(SalesOrder.created_at.desc()).limit(50).all())
    if not orders:
        return
    last = orders[0]
    before = customer.autonomy_level
    if last.was_edited and last.auto_processed and customer.autonomy_level > 0:
        customer.autonomy_level -= 1
    else:
        rules = policy.get(s, "autonomy.promote")
        nxt = str(customer.autonomy_level + 1)
        if nxt in rules:
            min_orders, max_edit = rules[nxt]
            edit_rate = sum(o.was_edited for o in orders) / len(orders)
            if len(orders) >= min_orders and edit_rate <= max_edit:
                customer.autonomy_level += 1
    if customer.autonomy_level != before:
        tracing.step("decision", f"Level otonomi {customer.name}: {before} ke {customer.autonomy_level}",
                     {"alasan": "Order otomatis dikoreksi admin" if customer.autonomy_level < before
                      else "Riwayat akurat memenuhi syarat naik level"})


@events.on("approval.decided")
def on_decided(s: Session, ev) -> None:
    a = s.get(Approval, ev.entity_id)
    approved = a.status == "APPROVED"
    with tracing.agent_run(s, "supervisor", event_id=ev.id, entity_type=a.entity_type, entity_id=a.entity_id):
        tracing.step("decision", f"Keputusan manusia: {a.kind} {'disetujui' if approved else 'ditolak'}",
                     {"oleh": a.decided_by, "alasan_awal": a.reason})
        if a.entity_type == "sales_order":
            _order_decision(s, a, approved)
        elif a.kind == "unmatched_payment" and approved:
            _manual_payment(s, a)
        elif a.kind == "promise_review":
            p = s.get(PromiseToPay, a.entity_id)
            customer = s.get(Customer, p.customer_id)
            p.status = "OPEN" if approved else "REJECTED"
            if approved:
                reply(s, customer, templates.promise_ack(customer.owner_name, p.amount, p.promised_date), category="order")
            else:
                reply(s, customer, f"{customer.owner_name}, untuk tanggal tersebut belum bisa kami setujui."
                                   f" Boleh dibantu pembayaran paling lambat {templates.tanggal(clock.today(s) + timedelta(days=7))}?")


def _order_decision(s: Session, a: Approval, approved: bool) -> None:
    order = s.get(SalesOrder, a.entity_id)
    customer = s.get(Customer, order.customer_id)
    tracing.link("sales_order", order.id, customer.id)
    if a.kind == "order_review":
        if approved:
            set_order_status(s, order_id=order.id, status="MATCHED", actor=a.decided_by or "admin")
            events.emit(s, "order.matched", "sales_order", order.id, {"reviewed": True})
            recompute_autonomy(s, customer)
        else:
            set_order_status(s, order_id=order.id, status="CANCELLED", actor=a.decided_by or "admin")
            reply(s, customer, templates.handoff(customer.owner_name))
        return
    if a.kind in ("credit", "discount"):
        if not approved:
            set_order_status(s, order_id=order.id, status="CANCELLED", actor=a.decided_by or "supervisor")
            reply(s, customer, f"{customer.owner_name}, pesanan {order.number} belum bisa kami proses."
                               f" Admin kami akan menghubungi untuk detailnya.")
            return
        notes = list((a.payload or {}).get("notes", [])) + unknown_item_notes(order)
        if a.kind == "discount" and order.requested_discount_pct:
            pct = order.requested_discount_pct
            order.total = int(round(order.total * (1 - pct / 100)))
            notes.append(f"Diskon {pct:g}% disetujui supervisor.")
        confirm_order(s, order, notes, a.decided_by or "supervisor")


def _manual_payment(s: Session, a: Approval) -> None:
    cid = (a.payload or {}).get("customer_id")
    if not cid:
        return
    tx = s.get(BankTransaction, a.entity_id)
    tx.status = "NEW"
    plan = plan_allocation(s, customer_id=cid, amount=tx.amount).data
    if plan["allocations"]:
        allocate_payment(s, tx_id=tx.id, customer_id=cid, allocations=plan["allocations"],
                         fee_writeoff=plan.get("fee_writeoff", 0), actor=a.decided_by or "staf_ar")
        from app.agents.credit import release_held_orders
        released = release_held_orders(s, cid)
        customer = s.get(Customer, cid)
        reply(s, customer, templates.payment_applied(customer.owner_name, tx.amount, [x["number"] for x in plan["allocations"]],
                                                     [(o.number, o.delivery_date) for o in released]))
