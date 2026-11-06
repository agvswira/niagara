"""Catalog matching: customer alias, global alias, fuzzy search, then LLM rerank. The LLM can only pick, never invent."""

import re
from collections import Counter, defaultdict
from statistics import median

from rapidfuzz import fuzz, process
from sqlalchemy.orm import Session

from app.llm.client import get_llm
from app.models import Product, ProductAlias, ProductUom, SalesOrder, SalesOrderLine
from app.tools.base import ToolError, tool

SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(ml|l|lt|ltr|liter|kg|gr|g)?\b")


def norm(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^a-z0-9.,' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _sizes(text: str) -> set[str]:
    out = set()
    for num, unit in SIZE_RE.findall(norm(text)):
        val = float(num.replace(",", "."))
        if unit in ("l", "lt", "ltr", "liter"):
            val *= 1000
        elif unit == "kg":
            val *= 1000
        if val >= 1:
            out.add(str(int(val)))
    return out


def usual_basket_data(s: Session, customer_id: int, last_n: int = 6) -> list[dict]:
    orders = (s.query(SalesOrder).filter(SalesOrder.customer_id == customer_id,
                                         SalesOrder.status.in_(["DELIVERED", "CONFIRMED", "SCHEDULED", "INVOICED"]))
              .order_by(SalesOrder.created_at.desc()).limit(last_n).all())
    if not orders:
        return []
    counts: Counter = Counter()
    qtys: dict[int, list[float]] = defaultdict(list)
    uoms: dict[int, Counter] = defaultdict(Counter)
    for o in orders:
        for ln in o.lines:
            if ln.product_id:
                counts[ln.product_id] += 1
                qtys[ln.product_id].append(ln.qty)
                uoms[ln.product_id][ln.uom] += 1
    basket = []
    for pid, n in counts.items():
        if n / len(orders) >= 0.5:
            p = s.get(Product, pid)
            basket.append({"product_id": pid, "name": p.name, "qty": float(max(1, int(median(qtys[pid]) + 0.5))),
                           "uom": uoms[pid].most_common(1)[0][0], "frequency": round(n / len(orders), 2)})
    return sorted(basket, key=lambda b: -b["frequency"])


@tool("get_usual_basket", "read")
def get_usual_basket(s: Session, customer_id: int) -> list[dict]:
    return usual_basket_data(s, customer_id)


GENERIC_BRANDS = {"beras", "telur"}


def _brand_named(brand: str, q: str, tokens: set[str]) -> bool:
    b = brand.lower()
    if b in GENERIC_BRANDS:
        return False
    first = b.split()[0]
    return b in q or (len(first) > 3 and first in tokens) or b.replace(" ", "") in tokens


def _candidates(s: Session, raw: str, customer_id: int, limit: int = 5) -> list[dict]:
    products = {p.id: p for p in s.query(Product).all()}
    choices: dict[str, int] = {}
    for p in products.values():
        choices[norm(p.name)] = p.id
        choices[norm(f"{p.brand} {p.variant}")] = p.id
    for a in s.query(ProductAlias).filter((ProductAlias.customer_id.is_(None)) | (ProductAlias.customer_id == customer_id)):
        choices[norm(a.alias)] = a.product_id
    q = norm(raw)
    best: dict[int, float] = {}
    for text, score, _ in process.extract(q, list(choices.keys()), scorer=fuzz.WRatio, limit=40):
        pid = choices[text]
        best[pid] = max(best.get(pid, 0), score)
    want = _sizes(raw)
    tokens = set(q.split())
    named_brands = {p.brand.lower() for p in products.values() if _brand_named(p.brand, q, tokens)}
    out = []
    for pid, score in best.items():
        p = products[pid]
        if want:
            have = _sizes(p.name + " " + p.variant)
            score = score + 6 if want & have else score - 18
        if named_brands:
            score = score + 8 if p.brand.lower() in named_brands else score - 12
        out.append({"product_id": pid, "sku": p.sku, "name": p.name, "score": round(min(score, 100), 1)})
    return sorted(out, key=lambda c: -c["score"])[:limit]


@tool("match_catalog", "read")
def match_catalog(s: Session, raw: str, customer_id: int) -> dict:
    q = norm(raw)
    if not q:
        raise ToolError("empty", "Teks barang kosong.")
    alias = (s.query(ProductAlias).filter(ProductAlias.alias == q, ProductAlias.customer_id == customer_id).first())
    if alias:
        p = s.get(Product, alias.product_id)
        return {"product_id": p.id, "name": p.name, "confidence": 0.99, "method": "alias_pelanggan", "candidates": []}
    alias = (s.query(ProductAlias).filter(ProductAlias.alias == q, ProductAlias.customer_id.is_(None))
             .order_by(ProductAlias.confirmations.desc()).first())
    if alias and alias.confirmations >= 2:
        p = s.get(Product, alias.product_id)
        return {"product_id": p.id, "name": p.name, "confidence": 0.97, "method": "alias_global", "candidates": []}

    cands = _candidates(s, raw, customer_id)
    if not cands or cands[0]["score"] < 55:
        return {"product_id": None, "name": None, "confidence": 0.0, "method": "tidak_ditemukan", "candidates": cands}
    usual_ids = {b["product_id"] for b in usual_basket_data(s, customer_id)}
    top = cands[0]
    margin = top["score"] - (cands[1]["score"] if len(cands) > 1 else 0)
    conf = min(0.95, top["score"] / 100) * (1.0 if margin >= 15 else 0.85 if margin >= 8 else 0.7)
    if top["product_id"] in usual_ids:
        conf = min(0.97, conf + 0.08)
    choice, method = top, "fuzzy"
    if conf < 0.9:
        rr, source = get_llm().rerank(raw, cands, usual_ids)
        if rr.choice == -1:
            return {"product_id": None, "name": None, "confidence": 0.3, "method": f"rerank_{source}_tolak",
                    "candidates": cands, "reason": rr.reason}
        picked = cands[rr.choice]
        if source != "heuristic":
            conf = rr.confidence * (0.95 if picked["product_id"] == top["product_id"] else 0.9)
            # The text itself does not separate the top candidates and history does not either, so whatever the
            # LLM picked is a guess. Cap it below the ask threshold: the customer decides, not the model.
            tied = [c for c in cands if top["score"] - c["score"] < 8]
            if len(tied) >= 2 and picked["product_id"] not in usual_ids:
                conf = min(conf, 0.7)
        choice, method = picked, f"rerank_{source}"
    return {"product_id": choice["product_id"], "name": choice["name"], "confidence": round(conf, 3),
            "method": method, "candidates": cands}


def find_product_in_basket(s: Session, raw: str, basket: list[dict]) -> dict | None:
    """Maps a phrase like 'aqua' to the basket item it refers to."""
    if not basket:
        return None
    names = {b["name"]: b for b in basket}
    hit = process.extractOne(norm(raw), list(names.keys()), scorer=fuzz.partial_ratio)
    if hit and hit[1] >= 70:
        return names[hit[0]]
    return None


def resolve_uom(s: Session, product_id: int, unit: str | None, basket: list[dict]) -> tuple[str, int, bool]:
    """Returns (uom, factor, assumed). Unknown units fall back to the customer's usual uom, else the largest pack."""
    uoms = {u.uom: u.factor for u in s.query(ProductUom).filter_by(product_id=product_id)}
    p = s.get(Product, product_id)
    if unit == "lusin":
        return p.base_uom, 12, False
    if unit in uoms:
        return unit, uoms[unit], False
    if unit == "pcs":
        return p.base_uom, 1, False
    if unit == "dus":
        big = [u for u in uoms if u != p.base_uom]
        if big:
            return big[0], uoms[big[0]], unit not in uoms
    usual = next((b for b in basket if b["product_id"] == product_id), None)
    if usual and usual["uom"] in uoms:
        return usual["uom"], uoms[usual["uom"]], True
    big = max(uoms.items(), key=lambda kv: kv[1])
    return big[0], big[1], True


def learn_alias(s: Session, raw: str, product_id: int, customer_id: int, source: str) -> None:
    q = norm(raw)
    if not q:
        return
    row = s.query(ProductAlias).filter_by(alias=q, customer_id=customer_id, product_id=product_id).first()
    if row:
        row.confirmations += 1
    else:
        s.add(ProductAlias(product_id=product_id, alias=q, customer_id=customer_id, source=source, confirmations=1))
    distinct_customers = (s.query(ProductAlias.customer_id)
                          .filter(ProductAlias.alias == q, ProductAlias.product_id == product_id,
                                  ProductAlias.customer_id.isnot(None)).distinct().count())
    if distinct_customers >= 3 and not s.query(ProductAlias).filter_by(alias=q, customer_id=None, product_id=product_id).first():
        s.add(ProductAlias(product_id=product_id, alias=q, customer_id=None, source="promoted", confirmations=distinct_customers))
    s.flush()
