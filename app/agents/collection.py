"""Collection side of the Credit & Collection Agent: daily dunning, promise-to-pay tracking, escalation."""

from collections import defaultdict
from datetime import timedelta

from sqlalchemy.orm import Session

from app import clock, events, policy, templates, tracing
from app.agents.common import reply, request_approval
from app.models import BankTransaction, Customer, Invoice, PaymentAllocation, PromiseToPay
from app.tools.base import rupiah
from app.tools.credit import UNPAID

REMIND_EVERY_DAYS = 3


def stage_for(days_to_due: int) -> int | None:
    if 0 < days_to_due <= 3:
        return 0
    if -7 <= days_to_due <= 0:
        return 1
    if -30 <= days_to_due < -7:
        return 2
    if days_to_due < -30:
        return 3
    return None


def _paid_since(s: Session, customer_id: int, since) -> int:
    rows = (s.query(PaymentAllocation).join(BankTransaction, PaymentAllocation.bank_tx_id == BankTransaction.id)
            .filter(BankTransaction.customer_id == customer_id, BankTransaction.tx_date >= since).all())
    return sum(r.amount + r.fee_writeoff for r in rows)


def check_promises(s: Session) -> list[str]:
    today = clock.today(s)
    log = []
    for p in s.query(PromiseToPay).filter(PromiseToPay.status == "OPEN", PromiseToPay.promised_date < today):
        customer = s.get(Customer, p.customer_id)
        created = p.promised_date - timedelta(days=30)
        if _paid_since(s, p.customer_id, created) >= p.amount * 0.95:
            p.status = "KEPT"
            customer.payment_score = min(1.0, customer.payment_score + 0.05)
            log.append(f"{customer.name}: janji bayar ditepati")
        else:
            p.status = "BROKEN"
            customer.payment_score = max(0.0, customer.payment_score - 0.15)
            reply(s, customer, templates.promise_broken(customer.owner_name, p.amount, p.promised_date), category="collection")
            log.append(f"{customer.name}: janji bayar {rupiah(p.amount)} tidak ditepati, skor turun")
    if log:
        tracing.step("think", "Cek janji bayar yang jatuh tempo", log)
    return log


def daily_dunning(s: Session) -> dict:
    today = clock.today(s)
    now = clock.now(s)
    promised_customers = {p.customer_id for p in s.query(PromiseToPay).filter(PromiseToPay.status == "OPEN",
                                                                               PromiseToPay.promised_date >= today)}
    by_customer: dict[int, list[tuple[Invoice, int]]] = defaultdict(list)
    for inv in s.query(Invoice).filter(Invoice.status.in_(UNPAID)):
        st = stage_for((inv.due_date - today).days)
        if st is None:
            continue
        if inv.due_date < today and inv.status == "OPEN":
            inv.status = "OVERDUE"
        by_customer[inv.customer_id].append((inv, st))
    sent, skipped, escalated = 0, [], 0
    for cid, items in by_customer.items():
        customer = s.get(Customer, cid)
        if cid in promised_customers:
            skipped.append(f"{customer.name}: ada janji bayar aktif")
            continue
        stage = max(st for _, st in items)
        fresh = [inv for inv, st in items if st > inv.collection_stage
                 or inv.last_reminder_at is None
                 or (st >= 1 and (now - inv.last_reminder_at).days >= REMIND_EVERY_DAYS)]
        if not fresh:
            continue
        rows = [{"number": inv.number, "amount": inv.amount - inv.paid_amount, "due_date": inv.due_date,
                 "days_late": max(0, (today - inv.due_date).days)} for inv, _ in items]
        ok = reply(s, customer, templates.collection_reminder(customer.owner_name, stage, rows), category="collection")
        if ok:
            sent += 1
            sent_at = clock.now(s)
            for inv, st in items:
                inv.collection_stage, inv.last_reminder_at = max(inv.collection_stage, st), sent_at
            # the agent works through the list one shop at a time, so reminders do not all carry the same minute
            clock.advance(s, minutes=1 + cid % 3)
        if stage == 3:
            request_approval(s, "escalation", "customer", cid,
                             f"Tagihan {customer.name} lewat 30 hari. Rekomendasi: stop suplai dan minta sales berkunjung.",
                             {"invoices": [r["number"] for r in rows]})
            escalated += 1
    summary = {"pengingat_terkirim": sent, "dilewati": skipped, "eskalasi": escalated}
    tracing.step("decision", f"Penagihan harian: {sent} pesan, {escalated} eskalasi", summary)
    return summary


@events.on("schedule.daily")
def on_daily(s: Session, ev) -> None:
    with tracing.agent_run(s, "collection", event_id=ev.id):
        check_promises(s)
        summary = daily_dunning(s)
        tracing.summarize(f"Penagihan {clock.today(s)}: {summary['pengingat_terkirim']} pengingat, {summary['eskalasi']} eskalasi")
    events.emit(s, "schedule.brief", payload={"collection": summary})


@events.on("collection.reply")
def on_reply(s: Session, ev) -> None:
    cid = ev.payload["customer_id"]
    promise = ev.payload["promise"]
    customer = s.get(Customer, cid)
    today = clock.today(s)
    with tracing.agent_run(s, "collection", event_id=ev.id, entity_type="customer", entity_id=cid, customer_id=cid):
        invs = (s.query(Invoice).filter(Invoice.customer_id == cid, Invoice.status.in_(UNPAID))
                .order_by(Invoice.due_date).all())
        outstanding = sum(i.amount - i.paid_amount for i in invs if i.due_date <= today) or sum(i.amount - i.paid_amount for i in invs)
        if not invs:
            reply(s, customer, templates.handoff(customer.owner_name))
            return
        amount = promise.get("amount") or int(outstanding * (promise.get("amount_pct") or 100) / 100)
        from app.agents.intake import resolve_days

        days = resolve_days(s, promise.get("date_text"), promise.get("days_from_today"))
        days = 3 if days is None else max(0, days)
        due = today + timedelta(days=days)
        tracing.step("think", "Janji bayar terbaca", {"tanggal": due.isoformat(), "nominal": amount,
                                                      "dari_total_jatuh_tempo": outstanding, "ungkapan": promise.get("date_text")})
        max_days = policy.get(s, "collection.max_promise_days")
        p = PromiseToPay(customer_id=cid, invoice_id=invs[0].id, promised_date=due, amount=amount,
                         source_message_id=ev.entity_id, status="OPEN")
        s.add(p)
        s.flush()
        if days > max_days:
            tracing.step("policy", "Janji bayar terlalu jauh, minta persetujuan",
                         {"rule": "collection.max_promise_days", "batas_hari": max_days, "diminta_hari": days}, ok=False)
            p.status = "REVIEW"
            request_approval(s, "promise_review", "promise", p.id,
                             f"{customer.name} janji bayar {rupiah(amount)} dalam {days} hari (batas {max_days} hari).",
                             {"amount": amount, "date": due.isoformat()})
            reply(s, customer, templates.promise_review(customer.owner_name))
        else:
            reply(s, customer, templates.promise_ack(customer.owner_name, amount, due))
        tracing.summarize(f"Janji bayar {customer.name}: {rupiah(amount)} pada {due.isoformat()}")
