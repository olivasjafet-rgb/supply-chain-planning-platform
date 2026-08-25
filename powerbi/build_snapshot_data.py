"""Snapshot dataset for the Direct Sales Inventory dashboard.

Mirrors the shape of the production report: one row per SKU, *today's* position
against its own min/max, with a central DC and inbound transit as the two
sources of cover. Purely simulated (fixed seed), sized so the composition
donuts read like a real catalogue rather than a toy.

Classification (the production rules, translated):
    made-to-order            -> not stocked; counted on the available side
    on hand >= min           -> In range, or Excess above max
    below min:
        DC covers >= 60%     -> Transfer from DC
        transit covers >=60% -> In transit
        otherwise            -> Buy
"""
import json
from pathlib import Path

import numpy as np

RNG = np.random.default_rng(20260825)
OUT = Path(__file__).parent / "snapshot_data.json"

CATS = [
    # division, category, n, cost range, monthly demand range
    ("Indoor Growing", "Mushroom Kits", 60, (5.5, 14.0), (300, 9000)),
    ("Indoor Growing", "Herb Kits", 55, (4.0, 12.0), (250, 6500)),
    ("Indoor Growing", "Water Garden", 35, (4.0, 30.0), (150, 3500)),
    ("Outdoor Gardening", "Seeds", 70, (0.8, 6.5), (400, 14000)),
    ("Outdoor Gardening", "Soils & Media", 55, (3.0, 9.5), (800, 22000)),
    ("Outdoor Gardening", "Garden Kits", 45, (8.0, 34.0), (120, 2600)),
]
BASE = {
    "Mushroom Kits": ["Oyster Grow Kit", "Pink Oyster Kit", "Lion's Mane Kit",
                      "Shiitake Kit", "Grow Kit 2-Pack", "Holiday Gift Box",
                      "Mushroom Spray Bottle", "Kit Refill Block"],
    "Herb Kits": ["Basil Windowsill Kit", "Cilantro Kit", "Mint Kit",
                  "Herb Trio Gift Set", "Microgreens Kit", "Rosemary Kit",
                  "Herb Scissors Set", "Seed Starting Tray"],
    "Water Garden": ["3-Gal Aquaponics", "Refill & Seed Kit", "Countertop Grow Kit",
                     "Fish Food Pack", "Pump Replacement", "LED Light Bar"],
    "Seeds": ["Heirloom Tomato Seeds", "Variety Pack 12ct", "Culinary Herb Pack",
              "Wildflower Mix", "Kids Starter Kit", "Pepper Seeds", "Carrot Seeds",
              "Lettuce Blend", "Sunflower Seeds", "Pollinator Mix"],
    "Soils & Media": ["Potting Mix 12qt", "Seed Starting Mix 8qt", "Raised Bed Mix",
                      "Compost Blend 8qt", "Coco Coir Brick", "Perlite 8qt",
                      "Worm Castings 4qt"],
    "Garden Kits": ["Raised Bed Kit", "Planter Box", "Pizza Garden Kit",
                    "Grow Your Own Set", "Tool Set 3pc", "Watering Can"],
}
VAR = ["", " - Organic", " - 2 Pack", " - Large", " - Refill", " - Classic",
       " - Value Size", " - Gift Ed.", " - Mini", " - Pro"]

rows, i = [], 0
for div, cat, n, (c0, c1), (d0, d1) in CATS:
    for k in range(n):
        i += 1
        name = BASE[cat][k % len(BASE[cat])] + VAR[(k // len(BASE[cat])) % len(VAR)]
        cost = round(float(RNG.uniform(c0, c1)), 2)
        demand = float(RNG.uniform(d0, d1))               # units / month
        avg4m = demand * cost                             # avg monthly cost of sales $
        mno = bool(RNG.random() < 0.14)                   # made to order
        min_u = 0.0 if mno else demand * float(RNG.uniform(0.8, 1.5))
        max_u = 0.0 if mno else min_u * float(RNG.uniform(1.6, 2.4))

        if mno:
            on = demand * float(RNG.uniform(0, 0.15))
        else:
            b = RNG.random()
            if b < 0.44:      on = min_u * float(RNG.uniform(0.0, 0.92))   # short
            elif b < 0.82:    on = float(RNG.uniform(min_u, max_u))        # in range
            else:             on = max_u * float(RNG.uniform(1.05, 1.65))  # excess

        gap_u = max(min_u - on, 0.0)
        dc_u = tr_u = 0.0
        if gap_u > 0:
            r = RNG.random()
            if r < 0.46:   dc_u = gap_u * float(RNG.uniform(0.7, 1.6))     # DC covers
            elif r < 0.62: tr_u = gap_u * float(RNG.uniform(0.8, 1.4))     # transit covers
            else:
                dc_u = gap_u * float(RNG.uniform(0.0, 0.5))
                tr_u = gap_u * float(RNG.uniform(0.0, 0.4))
        else:
            dc_u = demand * float(RNG.uniform(0.1, 0.9))
            tr_u = demand * float(RNG.uniform(0.0, 0.4)) if RNG.random() < 0.3 else 0.0

        over = max(on - max_u, 0.0) if not mno else 0.0
        active = bool(RNG.random() < (0.35 if over > 0 else 0.85))
        abc = "A" if RNG.random() < 0.2 else ("B" if RNG.random() < 0.42 else "C")

        gap_d, dc_d, tr_d = gap_u * cost, dc_u * cost, tr_u * cost
        if mno:                 st = "Made to order"
        elif gap_u <= 0:        st = "Excess" if over > 0 else "In range"
        elif dc_d >= 0.6 * gap_d: st = "Transfer from DC"
        elif tr_d >= 0.6 * gap_d: st = "In transit"
        else:                   st = "Buy"

        rows.append([f"BTR-{4000+i}", name, div, cat, abc, st,
                     round(on * cost), round(min_u * cost), round(max_u * cost),
                     round(gap_d), round(dc_d), round(tr_d),
                     round(over * cost), round(avg4m), int(active)])

cols = ["sku", "product", "division", "category", "abc", "status",
        "onHand", "reqMin", "reqMax", "gap", "dc", "transit",
        "excess", "avg4m", "active"]
OUT.write_text(json.dumps({"cols": cols, "rows": rows}, separators=(",", ":")),
               encoding="utf-8")

# --- verification ---------------------------------------------------------
from collections import Counter
st = Counter(r[5] for r in rows)
print(f"{len(rows)} SKUs, {OUT.stat().st_size//1024} KB")
for k, v in st.most_common():
    print(f"  {k:18s} {v:4d}  ({v/len(rows):.0%})")
short = [r for r in rows if r[5] in ("Buy", "Transfer from DC", "In transit")]
print(f"\nshortfall $ : Buy {sum(r[9] for r in rows if r[5]=='Buy')/1e3:,.1f}K"
      f" | DC {sum(r[9] for r in rows if r[5]=='Transfer from DC')/1e3:,.1f}K"
      f" | transit {sum(r[9] for r in rows if r[5]=='In transit')/1e3:,.1f}K")
exc = [r for r in rows if r[5] == "Excess"]
print(f"excess $    : {sum(r[12] for r in exc)/1e3:,.1f}K "
      f"(no-sale share {sum(r[12] for r in exc if not r[14])/max(sum(r[12] for r in exc),1):.0%})")
rec = [r for r in short if r[10] >= r[9] and r[9] > 0]
print(f"recoverable with a DC transfer: {len(rec)} SKUs, "
      f"${sum(r[9] for r in rec)/1e3:,.1f}K")
