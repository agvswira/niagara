"""Cash application: identify the payer, then match the amount to open invoices (exact, combination, fee tolerance, FIFO)."""

from datetime import date
from itertools import combinations

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from app import clock, policy
from app.models import BankTransaction, Customer, Invoice, PaymentAllocation, PaymentProof
from app.tools.base import ToolError, tool
from app.tools.credit import UNPAID
from app.tools.orders import audit


@tool("record_payment_proof", "write")
def record_payment_proof(s: Session, customer_id: int, message_id: int, bank: str | None, amount: int | None,
                         tx_date: str | None, sender_name: str | None, ref: str | None) -> dict:
    existing = s.query(PaymentProof).filter_by(message_id=message_id).first()
    if existing:
        return {"proof_id": existing.id, "idempotent_hit": True}
    p = PaymentProof(customer_id=customer_id, message_id=message_id, bank=(bank or "").upper(), amount=amount or 0,
                     tx_date=date.fromisoformat(tx_date) if tx_date else None, sender_name=sender_name or "",
                     ref=ref or "")
    s.add(p)
    s.flush()
    return {"proof_id": p.id, "idempotent_hit": False}


@tool("import_bank_mutations", "write")
def import_bank_mutations(s: Session, rows: list[dict]) -> dict:
    created, skipped = [], 0
    for r in rows:
        if s.query(BankTransaction).filter_by(ref=r["ref"]).first():
            skipped += 1
            continue
        tx = BankTransaction(bank=r["bank"].upper(), tx_date=date.fromisoformat(r["date"]), amount=int(r["amount"]),
                             description=r["description"], ref=r["ref"])
        s.add(tx)
        s.flush()
        created.append(tx.id)
    return {"created": created, "skipped_duplicates": skipped}


@tool("identify_payer", "read")
def identify_payer(s: Session, tx_id: int) -> dict:
    """Proof sent over chat wins; otherwise fuzzy-match the transfer description against account names."""
    tx = s.get(BankTransaction, tx_id)
    tol_days = policy.get(s, "payment.date_tolerance_days")
    for p in s.query(PaymentProof).filter(PaymentProof.status == "AWAITING_MUTATION"):
        same_amount = p.amount == tx.amount
        close_date = p.tx_date is None or abs((p.tx_date - tx.tx_date).days) <= tol_days
        same_bank = not p.bank or p.bank == tx.bank
        if same_amount and close_date and same_bank:
            return {"customer_id": p.customer_id, "method": "bukti_transfer", "proof_id": p.id, "score": 100}
    best = None
    for c in s.query(Customer).all():
        score = max(fuzz.token_set_ratio(tx.description.upper(), c.bank_account_name.upper()),
                    fuzz.token_set_ratio(tx.description.upper(), c.name.upper()))
        if best is None or score > best[1]:
            best = (c, score)
    if best and best[1] >= 80:
        return {"customer_id": best[0].id, "method": "nama_rekening", "proof_id": None, "score": best[1]}
    return {"customer_id": None, "method": "tidak_dikenal", "proof_id": None, "score": best[1] if best else 0}


@tool("plan_allocation", "read")
def plan_allocation(s: Session, customer_id: int, amount: int, max_combo: int = 8) -> dict:
    fee_tol = policy.get(s, "payment.fee_tolerance")
    invs = (s.query(Invoice).filter(Invoice.customer_id == customer_id, Invoice.status.in_(UNPAID))
            .order_by(Invoice.due_date).all())
    if not invs:
        return {"method": "tidak_ada_tagihan", "allocations": [], "unapplied": amount}
    open_amt = [(i, i.amount - i.paid_amount) for i in invs]
    for i, due in open_amt:
        if due == amount:
            return {"method": "cocok_persis", "allocations": [{"invoice_id": i.id, "number": i.number, "amount": due}],
                    "unapplied": 0}
    oldest = open_amt[:max_combo]
    for k in range(2, len(oldest) + 1):
        for combo in combinations(oldest, k):
            total = sum(d for _, d in combo)
            if 0 <= total - amount <= fee_tol:
                allocs = [{"invoice_id": i.id, "number": i.number, "amount": d} for i, d in combo]
                return {"method": "cocok_kombinasi" if total == amount else "kombinasi_biaya_transfer",
                        "allocations": allocs, "unapplied": 0, "fee_writeoff": total - amount}
    for i, due in open_amt:
        if 0 < due - amount <= fee_tol:
            return {"method": "biaya_transfer", "allocations": [{"invoice_id": i.id, "number": i.number, "amount": due}],
                    "unapplied": 0, "fee_writeoff": due - amount}
    remaining, allocs = amount, []
    for i, due in open_amt:
        if remaining <= 0:
            break
        take = min(due, remaining)
        allocs.append({"invoice_id": i.id, "number": i.number, "amount": take})
        remaining -= take
    return {"method": "sebagian_fifo", "allocations": allocs, "unapplied": max(0, remaining)}


@tool("allocate_payment", "financial")
def allocate_payment(s: Session, tx_id: int, customer_id: int, allocations: list[dict], fee_writeoff: int = 0,
                     actor: str = "cash_application_agent") -> dict:
    tx = s.get(BankTransaction, tx_id)
    if tx.status == "MATCHED":
        raise ToolError("already_matched", "Mutasi ini sudah dialokasikan.")
    applied = 0
    for idx, a in enumerate(allocations):
        inv = s.get(Invoice, a["invoice_id"])
        if inv.customer_id != customer_id:
            raise ToolError("wrong_customer", "Invoice bukan milik pelanggan ini.")
        writeoff = fee_writeoff if idx == len(allocations) - 1 else 0
        before = {"paid": inv.paid_amount, "status": inv.status}
        inv.paid_amount += a["amount"]
        if inv.paid_amount >= inv.amount:
            inv.paid_amount = inv.amount
            inv.status = "PAID"
        else:
            inv.status = "PARTIALLY_PAID"
        s.add(PaymentAllocation(bank_tx_id=tx.id, invoice_id=inv.id, amount=a["amount"] - writeoff, fee_writeoff=writeoff))
        audit(s, actor, "allocate_payment", "invoice", inv.id, before=before, after={"paid": inv.paid_amount, "status": inv.status})
        applied += a["amount"] - writeoff
    tx.status, tx.customer_id = "MATCHED", customer_id
    s.flush()
    return {"applied": applied, "invoices": [a["number"] for a in allocations]}


def unmatched(s: Session, tx: BankTransaction, note: str, candidates: list[dict]) -> None:
    tx.status, tx.match_note, tx.candidates = "UNMATCHED", note, candidates
    s.flush()


def today(s: Session):
    return clock.today(s)
