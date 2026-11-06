"""Policy engine: business rules live in the `policies` table and are evaluated by code, never by the LLM."""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models import Policy

DEFAULT_POLICIES: dict[str, tuple[Any, str]] = {
    "credit.max_overdue_days": (14, "Order ditahan bila ada invoice lewat jatuh tempo lebih dari N hari."),
    "credit.new_customer_approval_amount": (5_000_000, "Order pelanggan baru di atas nominal ini butuh approval supervisor."),
    "credit.spike_multiplier": (1.5, "Order di atas N kali rata-rata order pelanggan butuh approval."),
    "pricing.max_discount_pct": ({"A": 5, "B": 3, "C": 0}, "Batas diskon per tier. Permintaan di atas batas diteruskan ke supervisor."),
    "substitution.max_price_diff_pct": (10, "Pengganti hanya boleh dari kategori sama dengan selisih harga maksimal N persen."),
    "messaging.quiet_hours": ([21, 7], "Tidak mengirim pesan penagihan di antara jam ini."),
    "collection.max_msgs_per_day": (1, "Maksimal pesan penagihan per pelanggan per hari."),
    "conversation.question_ttl_hours": (24, "Jawaban atas pertanyaan agen hanya dianggap jawaban dalam N jam."),
    "conversation.close_unanswered_hours": (48, "Order yang menunggu jawaban pelanggan lebih dari N jam ditutup otomatis."),
    "claims.max_days_after_delivery": (3, "Keluhan kurang atau rusak diterima sampai N hari setelah barang dikirim."),
    "credit.unverified_needs_approval": (True, "Order dari nomor baru yang belum diverifikasi butuh persetujuan supervisor."),
    "collection.max_promise_days": (14, "Janji bayar lebih lama dari N hari butuh persetujuan supervisor."),
    "payment.fee_tolerance": (6_500, "Selisih kurang bayar sampai nominal ini dianggap biaya transfer antarbank."),
    "payment.date_tolerance_days": (1, "Selisih tanggal bukti transfer dan mutasi yang masih diterima."),
    "autonomy.thresholds": ({"0": 1.01, "1": 0.95, "2": 0.85}, "Confidence minimum agar order dikonfirmasi otomatis, per level otonomi."),
    "autonomy.promote": ({"1": [20, 0.10], "2": [50, 0.03]}, "Syarat naik level: [jumlah order minimal, tingkat edit maksimal]."),
}


def seed_policies(s: Session) -> None:
    for key, (value, desc) in DEFAULT_POLICIES.items():
        if not s.query(Policy).filter_by(key=key).first():
            s.add(Policy(key=key, value=value, description=desc))
    s.flush()


def get(s: Session, key: str) -> Any:
    row = s.query(Policy).filter_by(key=key).first()
    if row is None:
        return DEFAULT_POLICIES[key][0]
    return row.value


@dataclass
class Decision:
    outcome: str  # ALLOW / HOLD / APPROVAL / DENY
    rules: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "rules": self.rules, "reasons": self.reasons}


def evaluate_credit(s: Session, *, customer, order_total: int, outstanding: int, max_overdue_days: int,
                    avg_order_value: float, broken_promises: int) -> Decision:
    hold_rules, hold_reasons, appr_rules, appr_reasons = [], [], [], []
    limit_days = get(s, "credit.max_overdue_days")
    if max_overdue_days > limit_days:
        hold_rules.append("credit.max_overdue_days")
        hold_reasons.append(f"Ada invoice telat {max_overdue_days} hari (batas {limit_days} hari).")
    if outstanding + order_total > customer.credit_limit:
        hold_rules.append("credit.limit")
        hold_reasons.append(
            f"Piutang Rp{outstanding:,} + order Rp{order_total:,} melebihi limit Rp{customer.credit_limit:,}.".replace(",", ".")
        )
    if broken_promises >= 2:
        hold_rules.append("credit.broken_promises")
        hold_reasons.append(f"{broken_promises} janji bayar tidak ditepati.")
    if hold_rules:
        return Decision("HOLD", hold_rules, hold_reasons)

    if not customer.verified and get(s, "credit.unverified_needs_approval"):
        appr_rules.append("credit.unverified_needs_approval")
        appr_reasons.append("Nomor baru belum diverifikasi supervisor.")
    new_amt = get(s, "credit.new_customer_approval_amount")
    if customer.is_new and order_total > new_amt:
        appr_rules.append("credit.new_customer_approval_amount")
        appr_reasons.append(f"Pelanggan baru dengan order di atas Rp{new_amt:,}.".replace(",", "."))
    mult = get(s, "credit.spike_multiplier")
    if avg_order_value > 0 and order_total > mult * avg_order_value:
        appr_rules.append("credit.spike_multiplier")
        appr_reasons.append(f"Order {order_total / avg_order_value:.1f}x rata-rata pelanggan (batas {mult}x).")
    if appr_rules:
        return Decision("APPROVAL", appr_rules, appr_reasons)
    return Decision("ALLOW", ["credit.ok"], ["Kredit aman."])


def evaluate_discount(s: Session, *, tier: str, requested_pct: float | None) -> Decision:
    if not requested_pct:
        return Decision("ALLOW", [], [])
    cap = get(s, "pricing.max_discount_pct").get(tier, 0)
    if requested_pct <= cap:
        return Decision("APPROVAL", ["pricing.max_discount_pct"],
                        [f"Diminta diskon {requested_pct:g}% (dalam batas tier {tier} {cap}%), tetap butuh persetujuan supervisor."])
    return Decision("DENY", ["pricing.max_discount_pct"],
                    [f"Diminta diskon {requested_pct:g}%, melebihi batas tier {tier} ({cap}%). Harga tetap sesuai daftar harga."])


def autonomy_threshold(s: Session, level: int) -> float:
    return float(get(s, "autonomy.thresholds")[str(level)])


def in_quiet_hours(s: Session, hour: int) -> bool:
    start, end = get(s, "messaging.quiet_hours")
    return hour >= start or hour < end
