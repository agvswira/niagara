"""Synthetic distributor dataset: catalog, customers with distinct personas, and ~8 weeks of order and invoice history."""

import random
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import auth, clock, db, policy
from app.models import (
    Customer, CustomerContact, Invoice, PriceList, Product, ProductAlias, ProductUom, SalesOrder,
    SalesOrderLine, Stock,
)

# (sku, brand, name, category, variant, base_uom, {uom: factor}, price_per_base_tier_B, stock_in_base, [aliases])
CATALOG = [
    ("MI-IDG", "Indomie", "Indomie Mi Goreng Original 85g", "mie instan", "goreng", "pcs", {"dus": 40}, 3100, 4000, ["indomie goreng", "mie goreng", "indomi goreng", "idm goreng"]),
    ("MI-IDS", "Indomie", "Indomie Soto Mie 70g", "mie instan", "soto", "pcs", {"dus": 40}, 2900, 2000, ["indomie soto", "mie soto", "indomi soto"]),
    ("MI-IDAB", "Indomie", "Indomie Ayam Bawang 69g", "mie instan", "ayam bawang", "pcs", {"dus": 40}, 2900, 1600, ["indomie ayam bawang", "mie ayam bawang"]),
    ("MI-IDKR", "Indomie", "Indomie Kari Ayam 72g", "mie instan", "kari", "pcs", {"dus": 40}, 3000, 800, ["indomie kari", "mie kari"]),
    ("MI-MSG", "Mie Sedaap", "Mie Sedaap Goreng 90g", "mie instan", "goreng", "pcs", {"dus": 40}, 3000, 2400, ["sedaap goreng", "mie sedap goreng", "sedap goreng"]),
    ("MI-MSS", "Mie Sedaap", "Mie Sedaap Soto 75g", "mie instan", "soto", "pcs", {"dus": 40}, 2800, 1200, ["sedaap soto", "mie sedap soto"]),
    ("AM-AQ600", "Aqua", "Aqua Air Mineral 600ml", "air mineral", "600ml", "botol", {"dus": 24}, 3300, 1440, ["aqua tanggung", "aqua 600", "aqua sedang", "aqua tgg"]),
    ("AM-AQ1500", "Aqua", "Aqua Air Mineral 1500ml", "air mineral", "1500ml", "botol", {"dus": 12}, 5800, 600, ["aqua besar", "aqua 1500", "aqua 1,5"]),
    ("AM-AQ220", "Aqua", "Aqua Air Mineral Gelas 220ml", "air mineral", "gelas", "gelas", {"dus": 48}, 900, 2400, ["aqua gelas", "aqua cup"]),
    ("AM-LM600", "Le Minerale", "Le Minerale 600ml", "air mineral", "600ml", "botol", {"dus": 24}, 3100, 2400, ["le minerale", "leminerale", "le mineral 600", "lemineral"]),
    ("AM-LM1500", "Le Minerale", "Le Minerale 1500ml", "air mineral", "1500ml", "botol", {"dus": 12}, 5500, 480, ["le minerale besar", "le mineral 1500"]),
    ("MG-BML1", "Bimoli", "Bimoli Minyak Goreng 1L Pouch", "minyak goreng", "1L", "pouch", {"dus": 12}, 21500, 360, ["bimoli 1 liter", "bimoli 1l", "minyak bimoli"]),
    ("MG-BML2", "Bimoli", "Bimoli Minyak Goreng 2L Pouch", "minyak goreng", "2L", "pouch", {"dus": 6}, 42000, 180, ["bimoli 2 liter", "bimoli 2l"]),
    ("MG-SN1", "Sania", "Sania Minyak Goreng 1L Pouch", "minyak goreng", "1L", "pouch", {"dus": 12}, 20500, 300, ["sania 1 liter", "minyak sania"]),
    ("MG-SN2", "Sania", "Sania Minyak Goreng 2L Pouch", "minyak goreng", "2L", "pouch", {"dus": 6}, 40500, 120, ["sania 2 liter"]),
    ("MG-FTN2", "Fortune", "Fortune Minyak Goreng 2L Pouch", "minyak goreng", "2L", "pouch", {"dus": 6}, 39000, 0, ["fortune 2 liter", "minyak fortune"]),
    ("GL-GLK1", "Gulaku", "Gulaku Gula Pasir Premium 1kg", "gula", "1kg", "pak", {"karung": 20}, 18500, 400, ["gulaku", "gula gulaku", "gula 1kg"]),
    ("GL-RS1", "Rose Brand", "Rose Brand Gula Pasir 1kg", "gula", "1kg", "pak", {"karung": 20}, 17800, 300, ["gula rose brand", "gula rosebrand"]),
    ("KP-KA165", "Kapal Api", "Kapal Api Special Mix 25g (renteng isi 10)", "kopi", "mix", "renteng", {"dus": 12}, 14500, 240, ["kapal api mix", "kapal api renteng", "kapal api"]),
    ("KP-KAB", "Kapal Api", "Kapal Api Kopi Bubuk Special 165g", "kopi", "bubuk", "pak", {"dus": 24}, 15800, 144, ["kapal api bubuk", "kopi bubuk kapal api"]),
    ("KP-ABC", "ABC", "Kopi ABC Susu 31g (renteng isi 10)", "kopi", "susu", "renteng", {"dus": 12}, 13900, 180, ["abc susu", "kopi abc"]),
    ("KP-TOP", "TOP Coffee", "TOP Coffee Kopi Gula 25g (renteng isi 10)", "kopi", "gula", "renteng", {"dus": 12}, 12500, 120, ["top kopi", "top coffee"]),
    ("TH-SR25", "Sariwangi", "Sariwangi Teh Celup isi 25", "teh", "celup", "kotak", {"dus": 48}, 6200, 480, ["sariwangi", "teh sariwangi", "teh celup"]),
    ("TH-SS", "Sosro", "Teh Botol Sosro Kotak 250ml", "teh", "kotak", "kotak", {"dus": 24}, 3600, 720, ["teh kotak", "teh botol kotak", "tbs kotak"]),
    ("SU-FF", "Frisian Flag", "Frisian Flag Kental Manis Putih 370g", "susu", "kental manis", "kaleng", {"dus": 48}, 11800, 480, ["susu bendera", "frisian flag", "skm bendera"]),
    ("SU-IDM", "Indomilk", "Indomilk Kental Manis Putih 370g", "susu", "kental manis", "kaleng", {"dus": 48}, 11200, 240, ["indomilk", "susu indomilk", "skm indomilk"]),
    ("BS-RM", "Roma", "Roma Kelapa 300g", "biskuit", "kelapa", "pak", {"dus": 12}, 10500, 144, ["roma kelapa", "biskuit roma"]),
    ("BS-KG", "Khong Guan", "Khong Guan Assorted 1600g", "biskuit", "kaleng", "kaleng", {"dus": 4}, 96000, 40, ["khong guan", "kong guan", "kaleng khong guan"]),
    ("DT-RS800", "Rinso", "Rinso Anti Noda Deterjen Bubuk 770g", "deterjen", "bubuk", "pak", {"dus": 12}, 23500, 144, ["rinso", "rinso bubuk", "rinso 800"]),
    ("DT-SKL", "So Klin", "So Klin Deterjen Bubuk 770g", "deterjen", "bubuk", "pak", {"dus": 12}, 19500, 120, ["so klin", "soklin", "soklin bubuk"]),
    ("DT-SNL", "Sunlight", "Sunlight Jeruk Nipis 755ml Pouch", "sabun cuci piring", "pouch", "pouch", {"dus": 12}, 16500, 216, ["sunlight", "sunlight pouch", "sabun cuci piring"]),
    ("SB-LB", "Lifebuoy", "Lifebuoy Sabun Batang Merah 75g", "sabun mandi", "batang", "pcs", {"dus": 72}, 3700, 720, ["lifebuoy", "sabun lifebuoy", "lifeboy"]),
    ("SB-GV", "Giv", "Giv Sabun Batang 76g", "sabun mandi", "batang", "pcs", {"dus": 72}, 3200, 576, ["giv", "sabun giv"]),
    ("SB-PPS", "Pepsodent", "Pepsodent Pencegah Gigi Berlubang 190g", "pasta gigi", "190g", "pcs", {"dus": 48}, 12500, 288, ["pepsodent", "odol pepsodent", "pepsoden"]),
    ("BM-BLB", "Bango", "Bango Kecap Manis 520ml Pouch", "bumbu", "kecap", "pouch", {"dus": 24}, 22500, 144, ["kecap bango", "bango"]),
    ("BM-ABC", "ABC", "Kecap Manis ABC 520ml Pouch", "bumbu", "kecap", "pouch", {"dus": 24}, 20500, 96, ["kecap abc"]),
    ("BM-RKO", "Royco", "Royco Kaldu Ayam 230g", "bumbu", "kaldu", "pak", {"dus": 24}, 9800, 120, ["royco", "royco ayam"]),
    ("BM-MSK", "Masako", "Masako Kaldu Ayam 250g", "bumbu", "kaldu", "pak", {"dus": 24}, 9500, 144, ["masako", "masako ayam"]),
    ("BR-SB5", "Beras", "Beras Setra Ramos 5kg", "beras", "5kg", "karung", {}, 74000, 120, ["beras 5kg", "setra ramos", "beras ramos"]),
    ("TL-TLR", "Telur", "Telur Ayam Ras 1kg", "telur", "1kg", "kg", {"peti": 15}, 28500, 150, ["telur", "telor", "telur ayam"]),
]

TIER_MULT = {"A": 0.97, "B": 1.0, "C": 1.04}

AREAS = ["Klojen", "Lowokwaru", "Blimbing", "Sukun", "Kedungkandang", "Dau", "Singosari"]

# (name, owner, area, tier, limit, terms, persona, allow_sub, autonomy)
CUSTOMERS = [
    ("Toko Sinar Jaya", "Bu Ratna", "Klojen", "A", 25_000_000, 14, "rapi", True, 2),
    ("UD Berkah Abadi", "Pak Hasan", "Lowokwaru", "A", 30_000_000, 30, "rapi", True, 2),
    ("Toko Makmur Sentosa", "Ko Hendra", "Blimbing", "A", 20_000_000, 14, "singkatan", True, 2),
    ("Warung Bu Sri", "Bu Sri", "Sukun", "C", 3_000_000, 7, "singkatan", True, 1),
    ("Toko Rejeki Lancar", "Pak Joko", "Kedungkandang", "B", 10_000_000, 14, "foto", True, 1),
    ("Toko Barokah", "Bu Aminah", "Dau", "B", 8_000_000, 14, "telat_bayar", True, 1),
    ("Toko Mitra Usaha", "Pak Budi", "Singosari", "B", 12_000_000, 14, "ingkar_janji", False, 1),
    ("Toko Sumber Rezeki", "Bu Wati", "Klojen", "B", 9_000_000, 14, "rapi", True, 2),
    ("Toko Anugrah", "Pak Darmo", "Lowokwaru", "C", 4_000_000, 7, "telat_bayar", True, 0),
    ("Grosir Pak Slamet", "Pak Slamet", "Sukun", "A", 35_000_000, 30, "singkatan", True, 2),
    ("Toko Cahaya Baru", "Mbak Rina", "Blimbing", "B", 7_500_000, 14, "foto", True, 1),
    ("Toko Murah Meriah", "Pak Agus", "Kedungkandang", "C", 3_500_000, 7, "singkatan", True, 1),
    ("Toko Lestari", "Bu Yuni", "Dau", "B", 8_500_000, 14, "rapi", False, 1),
    ("Toko Bintang", "Pak Rudi", "Singosari", "B", 9_500_000, 14, "rapi", True, 2),
    ("Warung Mak Ijah", "Mak Ijah", "Sukun", "C", 2_500_000, 7, "singkatan", True, 0),
    ("Toko Harapan Kita", "Pak Yanto", "Lowokwaru", "B", 10_000_000, 14, "telat_bayar", True, 1),
    ("Toko Sejahtera", "Bu Lina", "Klojen", "A", 22_000_000, 14, "rapi", True, 2),
    ("Toko Merdeka", "Pak Wahyu", "Blimbing", "C", 4_500_000, 7, "foto", True, 0),
    ("Toko Sri Rejeki", "Bu Endang", "Kedungkandang", "B", 7_000_000, 14, "rapi", True, 1),
    ("Toko Damai", "Pak Iwan", "Dau", "B", 6_500_000, 14, "singkatan", True, 1),
    ("Toko Kencana", "Bu Dewi", "Singosari", "A", 18_000_000, 30, "rapi", True, 2),
    ("Toko Pelangi", "Mbak Tari", "Klojen", "C", 3_000_000, 7, "foto", True, 0),
    ("Toko Sentosa Abadi", "Pak Herman", "Lowokwaru", "B", 11_000_000, 14, "rapi", True, 1),
    ("Toko Maju Jaya", "Pak Dedi", "Sukun", "B", 9_000_000, 14, "ingkar_janji", True, 1),
    ("Toko Baru Buka", "Mas Fajar", "Blimbing", "C", 5_000_000, 7, "baru", True, 0),
]


def _number(prefix: str, d: datetime, n: int) -> str:
    return f"{prefix}-{d:%y%m%d}-{n:04d}"


def seed(s: Session, start: datetime | None = None, rng_seed: int = 7) -> None:
    rng = random.Random(rng_seed)
    start = start or datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    clock.set_now(s, start)
    policy.seed_policies(s)
    auth.seed_users(s)

    products: list[Product] = []
    for sku, brand, name, cat, variant, base, uoms, price, stock, aliases in CATALOG:
        p = Product(sku=sku, brand=brand, name=name, category=cat, variant=variant, base_uom=base)
        s.add(p)
        s.flush()
        products.append(p)
        s.add(ProductUom(product_id=p.id, uom=base, factor=1))
        for u, f in uoms.items():
            s.add(ProductUom(product_id=p.id, uom=u, factor=f))
        for tier, mult in TIER_MULT.items():
            s.add(PriceList(tier=tier, product_id=p.id, price_per_base=int(round(price * mult, -1))))
        s.add(Stock(product_id=p.id, qty_on_hand=stock))
        for a in aliases:
            s.add(ProductAlias(product_id=p.id, alias=a, source="seed", confirmations=3))
    s.flush()

    price_b = {p.id: c[7] for p, c in zip(products, CATALOG)}
    factor_big = {p.id: (list(c[6].values())[0] if c[6] else 1) for p, c in zip(products, CATALOG)}
    big_uom = {p.id: (list(c[6].keys())[0] if c[6] else c[5]) for p, c in zip(products, CATALOG)}

    order_seq = 0
    inv_seq = 0
    for i, (name, owner, area, tier, limit, terms, persona, allow_sub, autonomy) in enumerate(CUSTOMERS, start=1):
        c = Customer(
            name=name, owner_name=owner, area=area, tier=tier, credit_limit=limit, payment_terms_days=terms,
            allow_substitution=allow_sub, autonomy_level=autonomy, persona=persona, is_new=(persona == "baru"),
            bank_account_name=owner.replace("Bu ", "").replace("Pak ", "").replace("Ko ", "").replace("Mbak ", "")
            .replace("Mak ", "").replace("Mas ", "").upper() + " " + name.split()[-1].upper(),
        )
        s.add(c)
        s.flush()
        s.add(CustomerContact(customer_id=c.id, channel="simulator", handle=f"sim-{c.id}"))

        if persona == "baru":
            continue

        basket = rng.sample(products[:38], k=rng.randint(4, 7))
        basket_qty = {p.id: rng.choice([1, 1, 2, 2, 3, 5]) for p in basket}
        for week in range(8, 0, -1):
            for dow in (1, 4):
                od = start - timedelta(days=week * 7 - dow)
                if od >= start:
                    continue
                order_seq += 1
                o = SalesOrder(
                    number=_number("SO", od, order_seq), customer_id=c.id, status="DELIVERED", total=0,
                    delivery_date=(od + timedelta(days=1)).date(), auto_processed=autonomy > 0, is_history=True,
                    created_at=od, updated_at=od,
                )
                s.add(o)
                s.flush()
                total = 0
                for p in basket:
                    if rng.random() < 0.15:
                        continue
                    qty = max(1, basket_qty[p.id] + rng.choice([-1, 0, 0, 0, 1]))
                    unit_price = int(round(price_b[p.id] * TIER_MULT[tier], -1))
                    qty_base = qty * factor_big[p.id]
                    sub = qty_base * unit_price
                    total += sub
                    s.add(SalesOrderLine(order_id=o.id, product_id=p.id, raw_text=p.name, qty=qty, uom=big_uom[p.id],
                                         qty_base=qty_base, price=unit_price, subtotal=sub, confidence=1.0))
                o.total = total
                inv_seq += 1
                issue = od.date() + timedelta(days=1)
                due = issue + timedelta(days=terms)
                inv = Invoice(number=_number("INV", od, inv_seq), order_id=o.id, customer_id=c.id, amount=total,
                              issue_date=issue, due_date=due, status="PAID", paid_amount=total)
                days_to_due = (due - start.date()).days
                if days_to_due >= 0:
                    inv.status, inv.paid_amount = "OPEN", 0
                elif persona in ("telat_bayar", "ingkar_janji") and days_to_due > -21:
                    inv.status, inv.paid_amount = "OVERDUE", 0
                s.add(inv)
        s.flush()
        # Limits follow real volume: about 1.5x of what a customer normally owes within one payment term.
        totals = [o.total for o in s.query(SalesOrder).filter_by(customer_id=c.id)]
        if totals:
            per_term = sum(totals) / 8 * (terms / 7)
            c.credit_limit = max(c.credit_limit, int(round(per_term * 1.5, -6)))
    s.flush()


def reset_and_seed(start: datetime | None = None) -> None:
    from app.db import Base

    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    with db.session_scope() as s:
        seed(s, start)


if __name__ == "__main__":
    reset_and_seed()
    print("Database seeded.")
