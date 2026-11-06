"""Customer-facing messages. Every number and product name is filled from the database, never written by the LLM."""

from datetime import date

from app.tools.base import rupiah

HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def tanggal(d: date) -> str:
    return f"{HARI[d.weekday()]} {d.day} {BULAN[d.month - 1]}"


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
        parts.append(f"Dikirim {tanggal(order.delivery_date)}.")
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


def hold_notice(owner: str, order, overdue: list[dict]) -> str:
    rows = "\n".join(f"• {i['number']}  {rupiah(i['amount'])} (telat {i['days']} hari)" for i in overdue[:5])
    return (f"{owner}, pesanan {order.number} ({rupiah(order.total)}) sudah kami terima. Pengirimannya kami tahan dulu"
            f" karena masih ada tagihan yang lewat jatuh tempo:\n\n{rows}\n\nBegitu pembayaran masuk, pesanan langsung"
            f" kami jadwalkan. Kalau sudah transfer, kirim foto buktinya di sini.")


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


def payment_applied(owner: str, amount: int, invoices: list[str], released: list[tuple[str, date | None]]) -> str:
    msg = f"Pembayaran {rupiah(amount)} sudah masuk dan kami catat untuk {', '.join(invoices)}. Terima kasih {owner}."
    for number, d in released:
        msg += f"\n\nPesanan {number} sekarang kami jadwalkan kirim {tanggal(d)}." if d else f"\n\nPesanan {number} kami proses sekarang."
    return msg


def still_held(orders, outstanding: int, limit: int) -> str:
    numbers = ", ".join(o.number for o in orders)
    return (f"Pesanan {numbers} masih kami tahan: sisa tagihan {rupiah(outstanding)}, sedangkan batas kredit toko"
            f" {rupiah(limit)}. Begitu sisa tagihan turun di bawah batas, pesanan langsung kami jadwalkan.")


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


def complaint_ack(owner: str) -> str:
    return f"Maaf atas kendalanya, {owner}. Keluhan ini sudah kami teruskan ke admin, nanti dihubungi langsung."


def handoff(owner: str) -> str:
    return f"Pesan sudah kami terima, {owner}. Admin kami akan membalas."


def unreadable_media(owner: str) -> str:
    return f"{owner}, gambarnya belum bisa kami baca. Boleh ketik daftar pesanannya, atau kirim foto yang lebih jelas?"
