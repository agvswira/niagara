"""Offline parser for Indonesian order chats. Baseline for the eval and fallback when the LLM is unavailable."""

import json
import re
from datetime import date

from app.llm.schemas import (DeliveryIssue, DeliveryRequest, OrderChange, OrderLine, PaymentProof, Promise, Replacement, RerankResult,
                             Understanding)

UNIT_MAP = {
    "dus": "dus", "dos": "dus", "doz": "dus", "karton": "dus", "krt": "dus", "ktn": "dus", "ctn": "dus", "box": "dus",
    "pcs": "pcs", "pc": "pcs", "biji": "pcs", "bj": "pcs", "bks": "pcs", "bungkus": "pcs", "buah": "pcs", "bh": "pcs",
    "botol": "botol", "btl": "botol",
    "pak": "pak", "pack": "pak", "pck": "pak", "pax": "pak",
    "renteng": "renteng", "rtg": "renteng", "rent": "renteng",
    "karung": "karung", "krg": "karung", "sak": "karung",
    "kg": "kg", "kilo": "kg",
    "peti": "peti",
    "kaleng": "kaleng", "klg": "kaleng",
    "pouch": "pouch", "refill": "pouch",
    "gelas": "gelas", "cup": "gelas",
    "kotak": "kotak",
    "lusin": "lusin",
}
SIZE_UNITS = {"ml", "l", "lt", "ltr", "liter", "gr", "g", "gram"}
FILLER = {
    "tolong", "kirim", "kirimin", "kirimkan", "ya", "yaa", "yah", "mas", "mbak", "mba", "pak", "bu", "kak", "order", "pesan",
    "pesen", "mau", "minta", "juga", "lagi", "untuk", "buat", "besok", "dong", "aja", "saja", "nggih", "gan", "pagi",
    "siang", "sore", "malam", "halo", "hallo", "assalamualaikum", "permisi", "makasih", "terima", "kasih", "tks", "thx",
    "trims", "nya", "ini", "itu", "say", "sis", "bos", "boss", "koh", "ko", "cik", "ci", "hari", "the", "tapi", "terus",
    "trus", "sekalian", "skalian", "ok", "oke", "sip", "kintun", "tulung", "njaluk", "mohon",
}
NUMBER_WORDS = [
    ("dua puluh", 20), ("lima belas", 15), ("dua belas", 12), ("sebelas", 11), ("sepuluh", 10), ("sembilan", 9),
    ("delapan", 8), ("tujuh", 7), ("enam", 6), ("lima", 5), ("empat", 4), ("tiga", 3), ("dua", 2), ("satu", 1),
    ("selusin", 12), ("setengah lusin", 6),
]
USUAL_RE = re.compile(r"\b(yang|yg)\s+biasa\b|\b(kayak|kaya|seperti|spt|sama kayak)\s+(biasa|kemarin|kemaren|minggu lalu)\b|\border biasa\b")
REPLACE_RE = re.compile(r"([a-z][a-z0-9 ]*?)\s+(?:diganti|ganti jadi|ganti|tukar)\s+([a-z][a-z0-9 ]*?)(?=$|[,.\n]| aja| saja| ya\b| dulu)")
REMOVE_RE = re.compile(r"(?:tanpa|gak usah|ga usah|nggak usah|gausah|skip)\s+([a-z][a-z0-9 ]*?)(?=$|[,.\n]| aja| saja| ya\b)")
DISCOUNT_RE = re.compile(r"(?:diskon|potongan|disc|discount)\s*(?:jadi\s*)?(\d+(?:[.,]\d+)?)\s*%?")
SUSPICIOUS_RE = re.compile(r"abaikan|ignore|lupakan (semua )?aturan|system prompt|kamu sekarang|override|instruksi sebelumnya|jailbreak")
PAY_CLAIM_RE = re.compile(r"\b(tf|transfer|trf|sudah bayar|udah bayar|sdh bayar|sudah saya bayar|sudah ditransfer|sudah tf|udah tf|bukti)\b")
PAY_WORD_RE = re.compile(r"\b(bayar|dibayar|transfer|tf|lunas|lunasi|cicil|nyicil|tagihan|nota)\b")
COMPLAINT_RE = re.compile(r"\b(rusak|pecah|bocor|kurang kirim|salah kirim|komplain|kadaluarsa|expired|penyok"
                          r"|belum sampai|belum datang|tidak sampai)\b")
DAMAGE_WORDS = r"(?:rusak|pecah|bocor|penyok|kadaluarsa|kedaluwarsa|expired|sobek)"
QTY = r"(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>[a-z]+)?"
ISSUE_PATTERNS = [
    ("kurang", re.compile(rf"^(?P<item>[a-z][a-z0-9 ]*?)\s+(?:kurang kirim|kurang)\s+{QTY}$")),
    ("kurang", re.compile(rf"^(?:kurang kirim|kurang)\s+{QTY}\s+(?P<item>[a-z][a-z0-9 ]+)$")),
    ("rusak", re.compile(rf"^(?P<item>[a-z][a-z0-9 ]*?)\s+{QTY}\s+{DAMAGE_WORDS}$")),
    ("rusak", re.compile(rf"^{QTY}\s+(?P<item>[a-z][a-z0-9 ]*?)\s+{DAMAGE_WORDS}$")),
    ("rusak", re.compile(rf"^(?P<item>[a-z][a-z0-9 ]*?)\s+{DAMAGE_WORDS}\s+{QTY}$")),
    ("tidak_sampai", re.compile(r"^(?:pesanan|barang|kiriman|order)?\s*(?:kemarin\s+)?(?:belum|tidak|gak|ga)\s+(?:sampai|datang)$")),
]
CASH_RE = re.compile(r"\b(?:titip|titipkan|kasih|kasihkan|bayar|serahkan|setor)\w*\s+(?:\w+\s+)?(?:ke|sama|lewat|via)\s+"
                     r"(?:sales|salesnya|mas sales|kurir|sopir)\b|\b(?:tunai|cash)\b")
STATUS_RE = re.compile(r"\b(kapan (dikirim|sampai|datang)|sudah dikirim|udah dikirim|status|posisi barang)\b")
CHANGE_RE = re.compile(r"^\s*(tambah|tambahin|kurangi|kurangin|batal|cancel|ralat)\b")
YES_RE = re.compile(r"^\s*(ya+|iya+|yes|oke+|ok|boleh|setuju|gpp|nggak papa|ga papa|siap|lanjut|gas)\b")
NO_RE = re.compile(r"^\s*(tidak|gak|nggak|ga|engga|enggak|jangan|batal|no)\b")
WEEKDAYS = {"senin": 0, "selasa": 1, "rabu": 2, "kamis": 3, "jumat": 4, "jum'at": 4, "sabtu": 5, "minggu": 6}
NUM_UNIT_RE = re.compile(r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*([a-z']+)?")


def _normalize(text: str) -> str:
    t = text.lower().replace("\r", "")
    t = re.sub(r"[“”\"*_~]", " ", t)
    # Number words become digits only when a unit follows, so "dua" inside a product name is left alone.
    units = "|".join(sorted(UNIT_MAP, key=len, reverse=True))
    for word, n in NUMBER_WORDS:
        t = re.sub(rf"\b{word}\s+(?=({units})\b)", f"{n} ", t)
    return t


def _strip_filler(text: str) -> str:
    return " ".join(t for t in text.split() if t not in FILLER and t not in ("yang", "yg")).strip()


def _orphan_qty(chunk: str) -> tuple[float, str | None] | None:
    """A chunk like '2 dus aja' that carries a quantity but no product name."""
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*([a-z]+)?\s*", _strip_filler(chunk) or "")
    if not m:
        return None
    return float(m.group(1).replace(",", ".")), UNIT_MAP.get(m.group(2) or "")


def _parse_chunk(chunk: str) -> OrderLine | None:
    c = " " + chunk.strip() + " "
    qty, unit, span = None, None, None
    pairs = [(m, UNIT_MAP.get((m.group(2) or "").strip("'"))) for m in NUM_UNIT_RE.finditer(c)]
    packaging = [(m, u) for m, u in pairs if u and u != "kg"]
    kg_pairs = [(m, u) for m, u in pairs if u == "kg"]
    if packaging:
        m, unit = packaging[-1]
        qty, span = float(m.group(1).replace(",", ".")), m.span()
    elif kg_pairs and not any(w in c for w in ("gula", "gulaku")):
        m, unit = kg_pairs[-1]
        qty, span = float(m.group(1).replace(",", ".")), m.span()
    else:
        mx = re.search(r"(?:x\s*(\d+))|(?:(\d+)\s*x(?![a-z]))", c)
        if mx:
            qty, span = float(mx.group(1) or mx.group(2)), mx.span()
        else:
            lead = re.match(r"\s*(\d+)\s+(?=[a-z])", c)
            if lead and int(lead.group(1)) < 100:
                qty, span = float(lead.group(1)), lead.span()
            else:
                trail = re.search(r"\s(\d+)\s*$", c)
                if trail and int(trail.group(1)) < 100 and not re.search(r"(aqua|minerale?)\s+\d+\s*$", c):
                    qty, span = float(trail.group(1)), trail.span()
    name = c if span is None else c[: span[0]] + " " + c[span[1]:]
    tokens = [t for t in re.split(r"[\s\-:]+", name) if t]
    tokens = [t for t in tokens if t not in FILLER and not re.fullmatch(r"\d+[.)]", t)]
    if tokens and tokens[0] in ("yang", "yg"):
        tokens = tokens[1:]
    raw = " ".join(tokens).strip(" .,")
    if not raw or len(raw) < 3 or raw in SIZE_UNITS:
        return None
    return OrderLine(raw=raw, qty=qty if qty is not None else 1.0, unit=unit,
                     note=None if qty is not None else "jumlah tidak disebut")


CANCEL_WORDS = r"(?:batal(?:kan|in)?|cancel|gak jadi|ga jadi|nggak jadi|ndak jadi|tidak jadi|hapus|coret)"
CANCEL_ALL_RE = re.compile(rf"^\s*{CANCEL_WORDS}(?:\s+(?:semua|aja|saja|dulu|ya|mas|pak|bu|deh|pesanan(?:nya)?|order(?:an|nya)?|yang tadi|yg tadi|pesan))*\s*[.!]*\s*$")
REMOVE_ITEM_RE = re.compile(rf"^\s*{CANCEL_WORDS}\s+(?P<item>[a-z][a-z0-9 ]+?)\s*$")
SET_QTY_RE = re.compile(r"^\s*(?:(?:kurangi|kurangin|ubah|ralat|ganti)\s+)?(?P<item>[a-z][a-z0-9 ]*?)\s+(?:jadi|menjadi)\s+(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>[a-z]+)?\s*$")
DELTA_RE = re.compile(r"^\s*(?P<op>kurangi|kurangin|tambah|tambahin|tambahkan)\s+(?P<rest>.+)$")
DELIVERY_RE = re.compile(
    r"(?P<neg>jangan|jgn)?\s*(?:di)?(?:kirim|antar)(?:nya|in|kan)?\s+(?P<before>sebelum\s+)?(?:hari\s+|pas\s+)?"
    r"(?P<when>besok|lusa|senin|selasa|rabu|kamis|jumat|jum'at|sabtu|minggu|(?:tgl|tanggal)\s*\d{1,2})(?P<next>\s+depan)?")
DELIVERY_AFTER_RE = re.compile(
    r"(?P<before>)(?P<when>besok|lusa|senin|selasa|rabu|kamis|jumat|jum'at|sabtu|(?:tgl|tanggal)\s*\d{1,2})(?P<next>\s+depan)?"
    r"\s+(?:tolong\s+)?(?:di)?(?:kirim|antar)(?:nya|in|kan)?\b")
NON_ITEM_RE = re.compile(r"\b(tf|transfer|trf|bayar|lunas|bukti|tagihan|separo|setengah|kapan|status|rusak|pecah|komplain"
                         r"|besok|lusa|senin|selasa|rabu|kamis|jumat|sabtu|minggu depan|akhir bulan)\b")


def _date_phrase(t: str, today: date) -> tuple[int | None, str | None]:
    days = phrase = None
    if "lusa" in t:
        days, phrase = 2, "lusa"
    elif "besok" in t:
        days, phrase = 1, "besok"
    elif "hari ini" in t or "nanti sore" in t or "nanti malam" in t:
        days, phrase = 0, "hari ini"
    m = re.search(r"(?:tgl|tanggal)\s*(\d{1,2})", t)
    if m:
        d = int(m.group(1))
        target = today.replace(day=min(d, 28))
        if target < today:
            target = date(today.year + (today.month == 12), today.month % 12 + 1, min(d, 28))
        days, phrase = (target - today).days, m.group(0)
    for name, wd in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", t) and not (name == "minggu" and re.search(r"minggu (depan|ini|lalu)", t)):
            delta = (wd - today.weekday()) % 7 or 7
            next_week = bool(re.search(rf"{name}\s+depan|minggu depan", t))
            # "kamis depan" said on a Monday means next week's Thursday, not this week's.
            if next_week and delta <= 3:
                delta += 7
            days, phrase = delta, name + (" depan" if next_week else "")
    if days is None and "minggu depan" in t:
        days, phrase = 7, "minggu depan"
    if days is None and "akhir bulan" in t:
        nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        days, phrase = (nxt - today).days - 1, "akhir bulan"
    return days, phrase


def parse_delivery(t: str, today: date) -> tuple[DeliveryRequest | None, str]:
    """Returns the requested delivery day and the text with that phrase removed."""
    m = DELIVERY_RE.search(t) or DELIVERY_AFTER_RE.search(t)
    if not m:
        return None, t
    days, phrase = _date_phrase(m.group(0), today)
    if days is None:
        return None, t
    req = DeliveryRequest(date_text=m.group(0).strip(), days_from_today=days, not_before=bool(m.group("before")))
    return req, t[: m.start()] + " , " + t[m.end():]


def parse_message(text: str, today: date | None = None) -> dict:
    today = today or date.today()
    t = _normalize(text)
    delivery, t = parse_delivery(t, today)
    uses_usual = bool(USUAL_RE.search(t))
    t = USUAL_RE.sub(" ", t)
    replacements = [Replacement(from_item=_strip_filler(a), to_item=_strip_filler(b)) for a, b in REPLACE_RE.findall(t)]
    t = REPLACE_RE.sub(" , ", t)
    removals = [_strip_filler(r) for r in REMOVE_RE.findall(t)]
    t = REMOVE_RE.sub(" , ", t)
    t = DISCOUNT_RE.sub(" ", t)
    t = re.sub(r"(abaikan|ignore|lupakan|override)[^:,.\n]*[:,.\n]?", " , ", t)
    chunks = re.split(r"\n|,|;|\+|&|[.?!](?=\s|$)|\btapi\b|\bdan\b|\bsama\b|\bterus\b|\bkalih\b|\bambek\b|\bkaro\b|\blan\b|\bplus\b", t)
    lines: list[OrderLine] = []
    changes: list[OrderChange] = []
    issues: list[DeliveryIssue] = []
    for ch in chunks:
        bare = _strip_filler(ch)
        if not bare:
            continue
        issue = _parse_issue(bare)
        if issue:
            issues.append(issue)
            continue
        if CASH_RE.search(bare):
            continue
        if CANCEL_ALL_RE.match(bare):
            changes.append(OrderChange(op="batal_semua"))
            continue
        m = REMOVE_ITEM_RE.match(bare)
        if m:
            changes.append(OrderChange(op="hapus", item=_strip_filler(m.group("item"))))
            continue
        m = SET_QTY_RE.match(bare)
        if m:
            changes.append(OrderChange(op="ubah_jumlah", item=_strip_filler(m.group("item")),
                                       qty=float(m.group("qty").replace(",", ".")), unit=UNIT_MAP.get(m.group("unit") or "")))
            continue
        m = DELTA_RE.match(bare)
        if m:
            ln = _parse_chunk(m.group("rest"))
            if ln:
                op = "tambah" if m.group("op").startswith("tambah") else "kurangi"
                changes.append(OrderChange(op=op, item=ln.raw, qty=ln.qty, unit=ln.unit))
                continue
        orphan = _orphan_qty(ch)
        if orphan and replacements and replacements[-1].qty is None:
            replacements[-1].qty, replacements[-1].unit = orphan
            continue
        ln = _parse_chunk(ch)
        if ln is None:
            continue
        # Payment, status, or complaint sentences are not products unless they carry a quantity.
        if ln.note and NON_ITEM_RE.search(ch):
            continue
        lines.append(ln)
    return {"lines": lines, "replacements": replacements, "removals": removals, "uses_usual": uses_usual,
            "changes": changes, "delivery": delivery, "issues": issues, "text": t}


def _parse_issue(bare: str) -> DeliveryIssue | None:
    for kind, rx in ISSUE_PATTERNS:
        m = rx.match(bare)
        if not m:
            continue
        gd = m.groupdict()
        item = _strip_filler(gd.get("item") or "")
        item = re.sub(r"^(?:barang|kiriman)\s+", "", item) or None
        qty = float(gd["qty"].replace(",", ".")) if gd.get("qty") else None
        return DeliveryIssue(kind=kind, item=item, qty=qty, unit=UNIT_MAP.get(gd.get("unit") or ""))
    return None


def parse_cash_amount(t: str) -> int | None:
    ma = re.search(r"(\d+(?:[.,]\d+)?)\s*(jt|juta)", t)
    if ma:
        return int(float(ma.group(1).replace(",", ".")) * 1_000_000)
    mr = re.search(r"(\d+)\s*(rb|ribu)", t)
    if mr:
        return int(mr.group(1)) * 1000
    mrp = re.search(r"rp\.?\s*([\d.]{4,})", t)
    if mrp:
        return int(mrp.group(1).replace(".", ""))
    return None


def parse_order_lines(text: str) -> tuple[list[OrderLine], list[Replacement], list[str], bool]:
    p = parse_message(text)
    return p["lines"], p["replacements"], p["removals"], p["uses_usual"]


def parse_promise(text: str, today: date) -> Promise | None:
    t = _normalize(text)
    days, phrase = _date_phrase(t, today)
    if days is None:
        return None
    pct = None
    if re.search(r"separo|setengah|separuh|sebagian", t):
        pct = 50.0
    mp = re.search(r"(\d+)\s*%", t)
    if mp:
        pct = float(mp.group(1))
    if re.search(r"\blunas", t) and pct is None:
        pct = 100.0
    amount = None
    ma = re.search(r"(\d+(?:[.,]\d+)?)\s*(jt|juta)", t)
    if ma:
        amount = int(float(ma.group(1).replace(",", ".")) * 1_000_000)
    mr = re.search(r"(\d+)\s*(rb|ribu)", t)
    if mr and amount is None:
        amount = int(mr.group(1)) * 1000
    mrp = re.search(r"rp\.?\s*([\d.]{4,})", t)
    if mrp and amount is None:
        amount = int(mrp.group(1).replace(".", ""))
    if pct is None and amount is None:
        pct = 100.0
    return Promise(date_text=phrase, days_from_today=days, amount_pct=pct, amount=amount)


def read_png_meta(path: str | None) -> dict | None:
    """Simulator images carry their ground truth in a PNG text chunk; offline mode reads that instead of OCR."""
    if not path:
        return None
    try:
        from PIL import Image

        with Image.open(path) as im:
            raw = getattr(im, "text", {}).get("niagara")
        return json.loads(raw) if raw else None
    except Exception:
        return None


PRIORITY = ["bukti_bayar", "jawab_pertanyaan", "ubah_order", "order_baru", "klaim_bayar", "janji_bayar",
            "tanya_status", "komplain", "lainnya"]


def understand(text: str, kind: str = "text", media_path: str | None = None, ctx: dict | None = None) -> Understanding:
    ctx = ctx or {}
    today = ctx.get("today") or date.today()
    meta = read_png_meta(media_path) if kind == "image" else None
    proof = None
    if meta and meta.get("type") == "transfer":
        proof = PaymentProof(bank=meta.get("bank"), amount=meta.get("amount"), date=meta.get("date"),
                             sender_name=meta.get("sender_name"), ref=meta.get("ref"))
    elif meta and meta.get("type") == "nota":
        text = (text or "") + "\n" + meta.get("text", "")
    elif kind == "image":
        return Understanding(intent="lainnya", language_note="Gambar tidak bisa dibaca di mode offline.")
    if kind == "audio":
        return Understanding(intent="lainnya", language_note="Pesan suara butuh mode LLM.")

    t = _normalize(text or "")
    suspicious = bool(SUSPICIOUS_RE.search(t))
    disc = DISCOUNT_RE.search(t)
    base = dict(requested_discount_pct=float(disc.group(1).replace(",", ".")) if disc else None,
                suspicious_instruction=suspicious)
    p = parse_message(text or "", today)
    lines, changes = p["lines"], p["changes"]
    explicit = [ln for ln in lines if ln.note is None]

    if ctx.get("pending_question") and not proof and not explicit and not changes:
        nums = [int(n) for n in re.findall(r"(?<![\d.])(\d{1,3})(?![\d.])", t)]
        agrees = True if YES_RE.search(t) else False if NO_RE.search(t) else None
        if (nums and len(t.split()) <= 8) or agrees is not None:
            return Understanding(intent="jawab_pertanyaan", choices=nums, agrees=agrees, **base)

    # A "tambah" with no open order is simply a new order.
    if changes and not ctx.get("has_open_order"):
        lines = lines + [OrderLine(raw=c.item, qty=c.qty or 1, unit=c.unit) for c in changes if c.op == "tambah" and c.item]
        changes = [c for c in changes if c.op != "tambah"]

    intents: list[str] = []
    if proof:
        intents.append("bukti_bayar")
    if changes:
        intents.append("ubah_order")
    if lines or p["uses_usual"] or p["replacements"]:
        intents.append("order_baru")
    cash = bool(CASH_RE.search(t)) and not proof
    if (PAY_CLAIM_RE.search(t) or cash) and not proof:
        intents.append("klaim_bayar")
    promise = parse_promise(p["text"], today)
    if promise and (PAY_WORD_RE.search(t) or (ctx.get("in_collection") and not explicit and not p["uses_usual"])):
        intents.append("janji_bayar")
    else:
        promise = None
    if STATUS_RE.search(t):
        intents.append("tanya_status")
    if COMPLAINT_RE.search(t) or p["issues"]:
        intents.append("komplain")
    if not intents:
        intents.append("lainnya")
    intents.sort(key=PRIORITY.index)
    return Understanding(intent=intents[0], also_intents=intents[1:], lines=lines, changes=changes,
                         requested_delivery=p["delivery"], uses_usual_order=p["uses_usual"],
                         replacements=p["replacements"], removals=p["removals"], promise=promise,
                         payment_proof=proof, issues=p["issues"], cash_paid_to_sales=cash,
                         cash_amount=parse_cash_amount(t) if cash else None, **base)


def rerank(raw: str, candidates: list[dict], usual_ids: set[int]) -> RerankResult:
    if not candidates:
        return RerankResult(choice=-1, confidence=0.0, reason="Tidak ada kandidat.")
    best = max(range(len(candidates)), key=lambda i: candidates[i]["score"] + (6 if candidates[i]["product_id"] in usual_ids else 0))
    return RerankResult(choice=best, confidence=candidates[best]["score"] / 100,
                        reason="Skor kemiripan tertinggi" + (" dan sering dibeli pelanggan." if candidates[best]["product_id"] in usual_ids else "."))
