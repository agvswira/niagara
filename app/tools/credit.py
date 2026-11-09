from datetime import timedelta

from sqlalchemy.orm import Session

from app import clock, policy, tracing
from app.models import Customer, Invoice, PromiseToPay, SalesOrder
from app.tools.base import tool

UNPAID = ("OPEN", "OVERDUE", "PARTIALLY_PAID", "PROMISED")


def snapshot(s: Session, customer_id: int) -> dict:
    today = clock.today(s)
    invs = s.query(Invoice).filter(Invoice.customer_id == customer_id, Invoice.status.in_(UNPAID)).all()
    outstanding = sum(i.amount - i.paid_amount for i in invs)
    overdue = [i for i in invs if i.due_date < today]
    max_overdue = max(((today - i.due_date).days for i in overdue), default=0)
    hist = (s.query(SalesOrder).filter(SalesOrder.customer_id == customer_id, SalesOrder.total > 0,
                                       SalesOrder.status.in_(["DELIVERED", "INVOICED", "CONFIRMED", "SCHEDULED"]))
            .order_by(SalesOrder.created_at.desc()).limit(10).all())
    avg = sum(o.total for o in hist) / len(hist) if hist else 0
    broken = (s.query(PromiseToPay).filter(PromiseToPay.customer_id == customer_id, PromiseToPay.status == "BROKEN",
                                           PromiseToPay.promised_date >= today - timedelta(days=90)).count())
    return {
        "outstanding": outstanding,
        "overdue_amount": sum(i.amount - i.paid_amount for i in overdue),
        "overdue_invoices": [{"number": i.number, "amount": i.amount - i.paid_amount, "days": (today - i.due_date).days,
                              "issue_date": i.issue_date.isoformat()}
                             for i in sorted(overdue, key=lambda i: i.due_date)],
        "max_overdue_days": max_overdue,
        "avg_order_value": round(avg),
        "broken_promises": broken,
        "credit_balance": s.get(Customer, customer_id).credit_balance,
    }


@tool("get_credit_snapshot", "read")
def get_credit_snapshot(s: Session, customer_id: int) -> dict:
    return snapshot(s, customer_id)


def evaluate(s: Session, customer: Customer, order_total: int, snap: dict) -> policy.Decision:
    d = policy.evaluate_credit(s, customer=customer, order_total=order_total, outstanding=snap["outstanding"],
                               max_overdue_days=snap["max_overdue_days"], avg_order_value=snap["avg_order_value"],
                               broken_promises=snap["broken_promises"])
    tracing.step("policy", f"Evaluasi kredit: {d.outcome}", d.to_dict(), ok=d.outcome == "ALLOW")
    return d


def release_amount(s: Session, customer: Customer, order_total: int, snap: dict) -> int | None:
    """Smallest payment that lets a held order through, assuming it pays the oldest invoices first.

    Covers both hold rules: invoices late beyond the policy limit, and outstanding plus the order above the
    credit limit. Returns None when paying alone is not enough (broken promises need a person).
    """
    if snap["broken_promises"] >= 2:
        return None
    limit_days = policy.get(s, "credit.max_overdue_days")
    too_late = sum(i["amount"] for i in snap["overdue_invoices"] if i["days"] > limit_days)
    over_limit = max(0, snap["outstanding"] + order_total - customer.credit_limit)
    return max(too_late, over_limit)
