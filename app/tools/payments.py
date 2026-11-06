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
    if tx.customer_id and tx.kind in ("tunai", "giro"):
        return {"customer_id": tx.customer_id, "method": "tunai_sales" if tx.kind == "tunai" else "giro",
                "proof_id": None, "score": 100}
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


@tool("record_manual_payment", "financial")
def record_manual_payment(s: Session, customer_id: int, kind: str, amount: int, actor: str, collector: str = "",
                          giro_due: str | None = None, note: str = "") -> dict:
    """Cash handed to a salesperson, or a giro that clears on its due date."""
    if kind not in ("tunai", "giro") or amount <= 0:
        raise ToolError("bad_payment", "Jenis harus tunai atau giro dan nominal lebih dari nol.")
    today = clock.today(s)
    due = date.fromisoformat(giro_due) if giro_due else today
    n = s.query(BankTransaction).count() + 1
    c = s.get(Customer, customer_id)
    desc = (f"SETORAN TUNAI {c.name.upper()} VIA {collector.upper() or 'SALES'}" if kind == "tunai"
            else f"GIRO {c.name.upper()} CAIR {due.isoformat()}")
    tx = BankTransaction(bank="TUNAI" if kind == "tunai" else "GIRO", kind=kind, tx_date=due if kind == "giro" else today,
                         amount=amount, description=desc[:200], ref=f"{kind.upper()}-{today:%y%m%d}-{n:04d}",
                         status="GIRO_PENDING" if kind == "giro" and due > today else "NEW", customer_id=customer_id,
                         match_note=note or None)
    s.add(tx)
    s.flush()
    audit(s, actor, "manual_payment", "bank_transaction", tx.id, after={"jenis": kind, "nominal": amount, "cair": due.isoformat()})
    return {"tx_id": tx.id, "status": tx.status, "ref": tx.ref}


@tool("bounce_payment", "financial")
def bounce_payment(s: Session, tx_id: int, actor: str) -> dict:
    """A giro rejected by the bank: undo its allocations so the invoices are open again."""
    tx = s.get(BankTransaction, tx_id)
    if tx.kind != "giro" or tx.status not in ("GIRO_PENDING", "MATCHED", "NEW"):
        raise ToolError("not_bounceable", "Hanya giro yang belum ditolak yang bisa dibatalkan.")
    reopened = []
    for a in s.query(PaymentAllocation).filter_by(bank_tx_id=tx.id).all():
        inv = s.get(Invoice, a.invoice_id)
        inv.paid_amount = max(0, inv.paid_amount - a.amount - a.fee_writeoff)
        inv.status = "OPEN" if inv.paid_amount == 0 and inv.due_date >= clock.today(s) else (
            "OVERDUE" if inv.paid_amount == 0 else "PARTIALLY_PAID")
        reopened.append(inv.number)
        s.delete(a)
    tx.status = "BOUNCED"
    c = s.get(Customer, tx.customer_id)
    c.payment_score = max(0.0, c.payment_score - 0.2)
    audit(s, actor, "bounce_giro", "bank_transaction", tx.id, after={"invoice_dibuka_lagi": reopened})
    s.flush()
    return {"reopened": reopened, "customer_id": tx.customer_id, "amount": tx.amount}


@tool("add_to_balance", "financial")
def add_to_balance(s: Session, customer_id: int, amount: int, reason: str) -> dict:
    c = s.get(Customer, customer_id)
    before = c.credit_balance
    c.credit_balance += amount
    audit(s, "cash_application_agent", "add_balance", "customer", customer_id, before={"saldo": before},
          after={"saldo": c.credit_balance, "alasan": reason})
    s.flush()
    return {"saldo": c.credit_balance}


def apply_balance(s: Session, invoice: Invoice, actor: str = "cash_application_agent") -> int:
    """Uses the customer's saldo titipan to pay a new invoice. Returns the amount used."""
    c = s.get(Customer, invoice.customer_id)
    use = min(c.credit_balance, invoice.amount - invoice.paid_amount)
    if use <= 0:
        return 0
    c.credit_balance -= use
    invoice.paid_amount += use
    invoice.status = "PAID" if invoice.paid_amount >= invoice.amount else "PARTIALLY_PAID"
    audit(s, actor, "apply_balance", "invoice", invoice.id, after={"dipakai": use, "sisa_saldo": c.credit_balance})
    s.flush()
    return use


def unmatched(s: Session, tx: BankTransaction, note: str, candidates: list[dict]) -> None:
    tx.status, tx.match_note, tx.candidates = "UNMATCHED", note, candidates
    s.flush()


def today(s: Session):
    return clock.today(s)
