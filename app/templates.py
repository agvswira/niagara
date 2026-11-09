"""Customer-facing messages. Every number and product name is filled from the database, never written by the LLM."""

from datetime import date

from app.tools.base import rupiah

HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def tanggal(d: date) -> str:
    return f"{HARI[d.weekday()]} {d.day} {BULAN[d.month - 1]}"


def nota(iso_or_date) -> str:
    """Shop owners remember an invoice by its date, not its code: "nota 6 Okt"."""
    d = date.fromisoformat(iso_or_date) if isinstance(iso_or_date, str) else iso_or_date
    return f"{d.day} {BULAN[d.month - 1]}"


def order_items(order, limit: int = 2) -> str:
    """'3 dus Indomie Mi Soto' or '2 dus Indomie Goreng, 3 dus Aqua dan 1 barang lain'."""
    rows = [f"{qty_text(ln.qty, ln.uom)} {ln.product.name}" for ln in order.lines if ln.product_id and ln.qty_base]
    if not rows:
        return f"pesanan {order.number}"
    head = ", ".join(rows[:limit])
    return head + (f" dan {len(rows) - limit} barang lain" if len(rows) > limit else "")


def account_text() -> str:
    from app.config import settings
    return settings.payment_account or f"rekening {settings.company_name} (rekening simulasi)"


def qty_text(qty: float, uom: str) -> str:
    q = int(qty) if float(qty).is_integer() else qty
    return f"{q} {uom}"


def order_confirmation(owner: str, order, notes: list[str], updated: bool = False) -> str:
    head = f"Siap {owner}, pesanan {order.number} sudah kami {'perbarui' if updated else 'catat'}:"
    rows = []
    for ln in order.lines:
        if not ln.product_id or ln.qty_base == 0:
            continue
        rows.append(f"• {qty_text(ln.qty, ln.uom)} {ln.product.name}  {rupiah(ln.subtotal)}")
    parts = [head, "\n".join(rows), f"Total {rupiah(order.total)}"]
    if order.delivery_date:
        asked = order.requested_delivery is not None and (
            order.delivery_date == order.requested_delivery
            or (order.requested_not_before and order.delivery_date >= order.requested_delivery))
        parts.append(f"Dikirim {tanggal(order.delivery_date)}{', sesuai permintaan' if asked else ''}.")
    parts.extend(notes)
    return "\n\n".join(p for p in parts if p)


def clarification(owner: str, questions: list[dict]) -> str:
    if len(questions) == 1:
        q = questions[0]
        intro = f"{owner}, ada satu barang yang perlu kami pastikan dulu."
        body = _question_body(q)
        hint = "Balas angkanya saja, contoh: 1" if q["type"] == "choose_product" else "Balas jumlahnya saja, contoh: 3"
        return f"{intro}\n\n{body}\n\n{hint}"
    letters = "ABCDEFGH"
    blocks = [f"{letters[i]}. {_question_body(q)}" for i, q in enumerate(questions)]
    example = " ".join("1" for _ in questions)
    return (f"{owner}, ada {len(questions)} barang yang perlu kami pastikan dulu.\n\n" + "\n\n".join(blocks)
            + f"\n\nBalas berurutan dengan angka, contoh: {example}")


def _question_body(q: dict) -> str:
    if q["type"] == "choose_product":
        opts = "\n".join(f"   {i + 1}) {o['name']}" for i, o in enumerate(q["options"]))
        opts += f"\n   {len(q['options']) + 1}) Bukan salah satunya"
        return f"\"{q['raw']}\" maksudnya yang mana?\n{opts}"
    return f"\"{q['raw']}\" mau berapa {q.get('uom', 'dus')}?"


def substitution_offer(owner: str, offers: list[dict]) -> str:
    rows = []
    for o in offers:
        rows.append(f"• {o['from_name']}: stok tinggal {o['available_text']}. Bisa kami ganti {o['to_name']}"
                    f" ({'lebih mahal' if o['price_diff_pct'] > 0 else 'lebih murah' if o['price_diff_pct'] < 0 else 'harga sama'}"
                    f"{'' if o['price_diff_pct'] == 0 else ' ' + str(abs(o['price_diff_pct'])) + '%'}).")
    return (f"{owner}, ada stok yang kurang untuk pesanan ini:\n\n" + "\n".join(rows)
            + "\n\nMau diganti? Balas YA atau TIDAK. Kalau tidak, yang ada kami kirim dulu dan sisanya menyusul saat stok masuk.")


def hold_notice(owner: str, order, snap: dict, rules: list[str], minimum: int | None, credit_limit: int,
                limit_days: int) -> str:
    reasons = []
    if "credit.max_overdue_days" in rules:
        reasons.append(f"ada tagihan yang telat lebih dari {limit_days} hari")
    if "credit.limit" in rules:
        reasons.append(f"total tagihan sudah melewati batas kredit toko ({rupiah(credit_limit)})")
    if "credit.broken_promises" in rules:
        reasons.append("ada janji bayar yang belum ditepati")
    why = " dan ".join(reasons) or "ada tagihan yang perlu diselesaikan dulu"
    parts = [f"{owner}, pesanan {order_items(order)} ({rupiah(order.total)}) sudah kami terima."
             f" Pengirimannya kami tahan dulu karena {why}."]
    overdue = snap["overdue_invoices"]
    if overdue:
        rows = [f"• Nota {nota(i['issue_date'])}  {rupiah(i['amount'])}, telat {i['days']} hari" for i in overdue[:6]]
        if len(overdue) > 6:
            rows.append(f"• dan {len(overdue) - 6} nota lain")
        total = sum(i["amount"] for i in overdue)
        parts.append(f"Tagihan yang sudah lewat jatuh tempo, total *{rupiah(total)}*:\n" + "\n".join(rows))
    else:
        parts.append(f"Sisa tagihan saat ini *{rupiah(snap['outstanding'])}*.")
    if minimum:
        parts.append(f"Supaya pesanan ini bisa dikirim, mohon transfer minimal *{rupiah(minimum)}* ke {account_text()},"
                     f" dimulai dari nota paling lama.")
    else:
        parts.append("Sales kami akan menghubungi untuk mengatur pembayarannya.")
    parts.append("Kalau sudah transfer, kirim foto buktinya di sini. Begitu dana masuk, pesanan langsung kami jadwalkan.")
    parts.append(f"Rujukan: {order.number}")
    return "\n\n".join(parts)


def pending_approval(owner: str, order) -> str:
    return f"{owner}, pesanan {order.number} sudah kami terima dan sedang dicek supervisor. Kami kabari lagi hari ini."


def review_pending(owner: str) -> str:
    return f"Pesanan sudah kami terima, {owner}. Admin kami cek dulu, sebentar lagi kami kirim rinciannya."


def collection_reminder(owner: str, stage: int, invoices: list[dict]) -> str:
    rows = "\n".join(f"• {i['number']}  {rupiah(i['amount'])}, jatuh tempo {tanggal(i['due_date'])}"
                     + (f" (telat {i['days_late']} hari)" if i["days_late"] > 0 else "") for i in invoices)
    total = rupiah(sum(i["amount"] for i in invoices))
    if stage == 0:
        return f"Halo {owner}, kami ingatkan tagihan berikut akan jatuh tempo:\n\n{rows}\n\nTerima kasih."
    if stage == 1:
        return (f"{owner}, tagihan berikut sudah jatuh tempo:\n\n{rows}\n\nTotal {total}. Mohon dibantu pembayarannya,"
                f" lalu kirim foto bukti transfer di sini.")
    if stage == 2:
        return (f"{owner}, tagihan berikut sudah lewat jatuh tempo:\n\n{rows}\n\nTotal {total}."
                f" Boleh dikabari kapan rencana pembayarannya?")
    return (f"{owner}, tagihan berikut sudah lama lewat jatuh tempo:\n\n{rows}\n\nTotal {total}. Pengiriman berikutnya"
            f" kami tahan sampai ada pembayaran. Sales kami akan menghubungi langsung.")


def promise_ack(owner: str, amount: int, d: date) -> str:
    return f"Baik {owner}, kami catat rencana pembayaran {rupiah(amount)} pada {tanggal(d)}. Terima kasih."


def promise_review(owner: str) -> str:
    return f"Baik {owner}, rencana pembayarannya kami sampaikan ke supervisor dulu, nanti kami kabari."


def promise_broken(owner: str, amount: int, d: date) -> str:
    return (f"{owner}, pembayaran {rupiah(amount)} yang direncanakan {tanggal(d)} belum kami terima."
            f" Mohon dikabari kapan bisa ditransfer.")


def proof_received(owner: str, amount: int | None) -> str:
    amt = f" {rupiah(amount)}" if amount else ""
    return f"Terima kasih {owner}, bukti transfer{amt} sudah kami terima. Kami cocokkan dengan mutasi bank dulu, nanti kami kabari."


def payment_applied(owner: str, amount: int, invoices: list[dict], released: list, balance_added: int = 0,
                    outstanding_after: int | None = None) -> str:
    """invoices: [{number, issue_date, paid_off, not_due}]; released: orders now scheduled."""
    parts = [f"Terima kasih {owner}, pembayaran *{rupiah(amount)}* sudah masuk."]
    if invoices:
        labels = []
        for i in invoices:
            note = [] if i["paid_off"] else ["sebagian"]
            if i.get("not_due"):
                note.append("belum jatuh tempo")
            labels.append(nota(i["issue_date"]) + (f" ({', '.join(note)})" if note else ""))
        listed = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f", dan {labels[-1]}"
        parts.append(f"Kami pakai untuk {len(invoices)} nota: {listed}.")
    else:
        parts.append("Belum ada tagihan terbuka, jadi seluruhnya kami simpan sebagai saldo titipan.")
    if outstanding_after is not None:
        parts.append("Semua tagihan sekarang lunas." if outstanding_after <= 0
                     else f"Sisa tagihan: *{rupiah(outstanding_after)}*.")
    if balance_added and invoices:
        parts.append(f"Kelebihan {rupiah(balance_added)} kami simpan sebagai saldo titipan dan otomatis dipakai"
                     f" untuk tagihan berikutnya.")
    for order in released:
        when = f"kami jadwalkan kirim *{tanggal(order.delivery_date)}*" if order.delivery_date else "kami proses sekarang"
        parts.append(f"Pesanan {order_items(order)} {when}.")
    refs = [i["number"] for i in invoices] + [o.number for o in released]
    if refs:
        parts.append("Rujukan: " + ", ".join(refs))
    return "\n\n".join(parts)


def still_held(orders, minimum: int | None) -> str:
    items = "; ".join(order_items(o) for o in orders)
    if minimum:
        return (f"Pesanan {items} masih kami tahan. Supaya bisa dikirim, masih kurang *{rupiah(minimum)}*"
                f" ke {account_text()}.")
    return f"Pesanan {items} masih kami tahan. Sales kami akan menghubungi untuk mengatur pembayarannya."


def proof_already_received(owner: str, amount: int) -> str:
    return (f"{owner}, bukti transfer {rupiah(amount)} sudah kami terima dan sedang dicocokkan dengan mutasi bank."
            f" Nanti kami kabari begitu masuk.")


def ask_for_proof(owner: str) -> str:
    return f"Terima kasih {owner}. Supaya cepat kami cocokkan, boleh kirim foto bukti transfernya di sini?"


STATUS_TEXT = {
    "RECEIVED": "baru kami terima", "NEEDS_CLARIFICATION": "menunggu jawaban Bapak/Ibu", "PENDING_REVIEW": "sedang dicek admin",
    "MATCHED": "sedang diproses", "SUBSTITUTION_OFFERED": "menunggu jawaban soal barang pengganti",
    "STOCK_CHECKED": "sedang diproses", "ON_HOLD_CREDIT": "ditahan sampai tagihan lama dibayar",
    "PENDING_APPROVAL": "sedang dicek supervisor", "CONFIRMED": "sudah dikonfirmasi", "DELIVERED": "sudah dikirim",
    "INVOICED": "sudah dikirim", "CANCELLED": "dibatalkan",
}


def order_status(owner: str, order) -> str:
    when = f", jadwal kirim {tanggal(order.delivery_date)}" if order.delivery_date and order.status in ("CONFIRMED",) else ""
    return f"{owner}, pesanan {order.number} {STATUS_TEXT.get(order.status, order.status.lower())}{when}."


def no_open_order(owner: str) -> str:
    return f"{owner}, belum ada pesanan aktif atas nama toko ini. Mau order apa hari ini?"


def no_earlier_order(owner: str) -> str:
    return (f"{owner}, sebelum pesanan ini belum ada pesanan lain yang masih berjalan."
            f" Pesanan yang barusan sedang kami proses, rinciannya menyusul.")


def complaint_ack(owner: str) -> str:
    return f"Maaf atas kendalanya, {owner}. Keluhan ini sudah kami teruskan ke admin, nanti dihubungi langsung."


def unknown_items(owner: str, items: list[str]) -> str:
    names = ", ".join(f"\"{i}\"" for i in items)
    return (f"{owner}, {names} belum ada di katalog kami. Admin kami cek dulu dan akan menghubungi."
            f" Kalau ada merek atau ukurannya, boleh ditulis di sini.")


def order_cancelled(owner: str, order) -> str:
    return f"Baik {owner}, pesanan {order.number} sudah kami batalkan."


def already_shipped(owner: str, order) -> str:
    return (f"{owner}, pesanan {order.number} sudah dikirim jadi tidak bisa diubah lagi dari sini."
            f" Admin kami akan menghubungi untuk retur atau tambahan pesanan.")


def claim_received(owner: str, order, rows: list[str], notes: list[str]) -> str:
    body = "\n".join(f"• {r}" for r in rows)
    msg = f"Maaf atas kendalanya, {owner}. Keluhan untuk pesanan {order.number} sudah kami catat:\n\n{body}"
    msg += "\n\nAdmin kami cek dulu. Bila disetujui, nilainya langsung memotong tagihan."
    if notes:
        msg += "\n\n" + "\n".join(notes)
    return msg


def claim_approved(owner: str, cn: dict) -> str:
    parts = [f"{owner}, klaim disetujui. Nota kredit {cn['credit_note']} sebesar {rupiah(cn['amount'])}"]
    if cn["to_invoice"] and cn["invoice"]:
        parts.append(f" memotong tagihan {cn['invoice']} sebesar {rupiah(cn['to_invoice'])}")
    if cn["to_balance"]:
        parts.append(f"{' dan' if cn['to_invoice'] else ''} {rupiah(cn['to_balance'])} kami simpan sebagai saldo titipan")
    return "".join(parts) + "."


def claim_rejected(owner: str, order_number: str) -> str:
    return f"{owner}, keluhan untuk pesanan {order_number} belum bisa kami setujui. Admin kami akan menghubungi untuk penjelasannya."


def not_arrived_ack(owner: str, order) -> str:
    when = f" (jadwal {tanggal(order.delivery_date)})" if order.delivery_date else ""
    return f"Maaf, {owner}. Pesanan {order.number}{when} kami cek ke sopir sekarang, nanti kami kabari posisinya."


def cash_claim_ack(owner: str, amount: int | None) -> str:
    amt = f" {rupiah(amount)}" if amount else ""
    return (f"Terima kasih {owner}. Pembayaran tunai{amt} lewat sales kami cocokkan dengan setoran sales dulu,"
            f" nanti kami kabari begitu tercatat.")


def backorder_only(owner: str, order, notes: list[str]) -> str:
    msg = (f"{owner}, pesanan {order.number} sudah kami catat, tapi semua barangnya sedang kosong."
           f" Begitu stok masuk, langsung kami kirim dan kami kabari jadwalnya.")
    return msg + ("\n\n" + "\n".join(notes) if notes else "")


def giro_bounced(owner: str, amount: int, invoices: list[str]) -> str:
    inv = f" Tagihan {', '.join(invoices)} kembali terbuka." if invoices else ""
    return f"{owner}, giro sebesar {rupiah(amount)} ditolak bank.{inv} Mohon dibantu pembayaran pengganti."


def welcome_unknown(handle: str) -> str:
    return ("Halo, terima kasih sudah menghubungi kami. Nomor ini belum terdaftar sebagai pelanggan."
            " Mohon kirim nama toko dan alamatnya. Pesanan tetap kami catat dan diproses setelah tim kami memverifikasi.")


def welcome_verified(owner: str, name: str) -> str:
    return f"Halo {owner}, {name} sudah terdaftar. Pesanan berikutnya langsung kami proses."


def order_closed_unanswered(owner: str, order) -> str:
    return (f"{owner}, pesanan {order.number} kami tutup karena belum ada jawaban. Kalau masih perlu,"
            f" kirim ulang pesanannya ya.")


def handoff(owner: str) -> str:
    return f"Pesan sudah kami terima, {owner}. Admin kami akan membalas."


def unreadable_media(owner: str) -> str:
    return f"{owner}, gambarnya belum bisa kami baca. Boleh ketik daftar pesanannya, atau kirim foto yang lebih jelas?"
