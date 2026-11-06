"""Offline parser for Indonesian order chats. Baseline for the eval and fallback when the LLM is unavailable."""

import json
import re
from datetime import date

from app.llm.schemas import OrderLine, PaymentProof, Promise, Replacement, RerankResult, Understanding

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
COMPLAINT_RE = re.compile(r"\b(rusak|pecah|bocor|kurang kirim|salah kirim|komplain|kadaluarsa|expired|penyok)\b")
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


def parse_order_lines(text: str) -> tuple[list[OrderLine], list[Replacement], list[str], bool]:
    t = _normalize(text)
    uses_usual = bool(USUAL_RE.search(t))
    t = USUAL_RE.sub(" ", t)
    replacements = [Replacement(from_item=_strip_filler(a), to_item=_strip_filler(b)) for a, b in REPLACE_RE.findall(t)]
    t = REPLACE_RE.sub(" , ", t)
    removals = [_strip_filler(r) for r in REMOVE_RE.findall(t)]
    t = REMOVE_RE.sub(" , ", t)
    t = DISCOUNT_RE.sub(" ", t)
    t = re.sub(r"(abaikan|ignore|lupakan|override)[^:,.\n]*[:,.\n]?", " , ", t)
    chunks = re.split(r"\n|,|;|\+|&|\btapi\b|\bdan\b|\bsama\b|\bterus\b|\bkalih\b|\bambek\b|\bkaro\b|\blan\b|\bplus\b|\./", t)
    lines = []
    for ch in chunks:
        orphan = _orphan_qty(ch)
        if orphan and replacements and replacements[-1].qty is None:
            replacements[-1].qty, replacements[-1].unit = orphan
            continue
        ln = _parse_chunk(ch)
        if ln:
            lines.append(ln)
    return lines, replacements, removals, uses_usual


def parse_promise(text: str, today: date) -> Promise | None:
    t = _normalize(text)
    days = None
    phrase = None
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
            month = today.month % 12 + 1
            year = today.year + (1 if today.month == 12 else 0)
            target = date(year, month, min(d, 28))
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


def understand(text: str, kind: str = "text", media_path: str | None = None, ctx: dict | None = None) -> Understanding:
    ctx = ctx or {}
    today = ctx.get("today") or date.today()
    meta = read_png_meta(media_path) if kind == "image" else None
    if meta and meta.get("type") == "transfer":
        return Understanding(intent="bukti_bayar", payment_proof=PaymentProof(
            bank=meta.get("bank"), amount=meta.get("amount"), date=meta.get("date"),
            sender_name=meta.get("sender_name"), ref=meta.get("ref")))
    if meta and meta.get("type") == "nota":
        text = (text or "") + "\n" + meta.get("text", "")
    if kind == "image" and not meta:
        return Understanding(intent="lainnya", language_note="Gambar tidak bisa dibaca di mode offline.")
    if kind == "audio":
        return Understanding(intent="lainnya", language_note="Pesan suara butuh mode LLM.")

    t = _normalize(text)
    suspicious = bool(SUSPICIOUS_RE.search(t))
    disc = DISCOUNT_RE.search(t)
    discount = float(disc.group(1).replace(",", ".")) if disc else None
    lines, repl, removals, usual = parse_order_lines(text)
    base = dict(requested_discount_pct=discount, suspicious_instruction=suspicious)

    if ctx.get("pending_question"):
        nums = [int(n) for n in re.findall(r"(?<![\d.])(\d{1,3})(?![\d.])", t)]
        agrees = True if YES_RE.search(t) else False if NO_RE.search(t) else None
        short = len(t.split()) <= 8
        if (nums and short) or agrees is not None:
            return Understanding(intent="jawab_pertanyaan", choices=nums if short else [], agrees=agrees, **base)

    explicit = [ln for ln in lines if ln.note is None]
    promise = parse_promise(t, today)
    if promise and (PAY_WORD_RE.search(t) or ctx.get("in_collection")) and not explicit and not usual:
        return Understanding(intent="janji_bayar", promise=promise, **base)
    if PAY_CLAIM_RE.search(t) and not explicit:
        return Understanding(intent="klaim_bayar", **base)
    if COMPLAINT_RE.search(t):
        return Understanding(intent="komplain", **base)
    if STATUS_RE.search(t) and not explicit:
        return Understanding(intent="tanya_status", **base)
    if CHANGE_RE.search(t) and ctx.get("has_open_order"):
        return Understanding(intent="ubah_order", lines=lines, removals=removals, **base)
    if lines or usual or repl:
        return Understanding(intent="order_baru", lines=lines, uses_usual_order=usual, replacements=repl,
                             removals=removals, **base)
    return Understanding(intent="lainnya", **base)


def rerank(raw: str, candidates: list[dict], usual_ids: set[int]) -> RerankResult:
    if not candidates:
        return RerankResult(choice=-1, confidence=0.0, reason="Tidak ada kandidat.")
    best = max(range(len(candidates)), key=lambda i: candidates[i]["score"] + (6 if candidates[i]["product_id"] in usual_ids else 0))
    return RerankResult(choice=best, confidence=candidates[best]["score"] / 100,
                        reason="Skor kemiripan tertinggi" + (" dan sering dibeli pelanggan." if candidates[best]["product_id"] in usual_ids else "."))
