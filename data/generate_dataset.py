"""Synthetic dataset generator -- a Back to the Roots shaped business.

Why synthetic: this repository is a portfolio piece. The modelling techniques
are the ones I use on a live 400-class import/distribution book; the numbers
here are simulated so that nothing proprietary leaves my employer. The shapes
are deliberately realistic:

  * gardening demand peaks in spring, grow-kit gifting peaks in Q4;
  * imported kits carry 75-110 day lead times, domestic soil carries 10-21;
  * retail accounts order in case-pack multiples, DTC does not;
  * a handful of SKUs are intentionally messy (short history, erratic demand)
    so the planning logic has to cope with them.

Run:  python data/generate_dataset.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402

RNG = np.random.default_rng(20260824)

HISTORY_START = pd.Period("2024-09", freq="M")
HISTORY_END = pd.Period("2026-08", freq="M")  # 24 months of actuals

# -----------------------------------------------------------------------------
# Suppliers -- anonymised. Origin expense rate is the % of purchase value that
# arrives as freight/handling/documentation on top of FOB.
# -----------------------------------------------------------------------------
SUPPLIERS = [
    # id,    country,       lead_time, lt_std, origin_rate, payment_terms
    ("SUP-01", "China",         95, 12, 0.058, "30% deposit / 70% on B/L"),
    ("SUP-02", "China",        102, 15, 0.061, "T/T 30 days"),
    ("SUP-03", "Vietnam",       88, 11, 0.049, "L/C at sight"),
    ("SUP-04", "India",        110, 18, 0.067, "T/T 45 days"),
    ("SUP-05", "Mexico",        35,  6, 0.022, "Net 30"),
    ("SUP-06", "United States", 14,  3, 0.000, "Net 30"),
    ("SUP-07", "United States", 10,  2, 0.000, "Net 45"),
    ("SUP-08", "United States", 21,  4, 0.000, "Net 30"),
    ("SUP-09", "Canada",        18,  4, 0.008, "Net 30"),
]

# -----------------------------------------------------------------------------
# Product master
#   sku_id, sku_name, division, category, subcategory, supplier,
#   fob_unit_cost, retail_price, case_pack, moq_units, base_monthly_units
# -----------------------------------------------------------------------------
PRODUCTS = [
    # ---- Indoor Growing / Mushroom Kits -------------------------------------
    ("BTR-1001", "Organic Mushroom Grow Kit - Oyster",   "Indoor Growing", "Mushroom Kits", "Single Kits",  "SUP-01",  6.40, 21.99,  6,  3000, 14500),
    ("BTR-1002", "Pink Oyster Mushroom Grow Kit",        "Indoor Growing", "Mushroom Kits", "Single Kits",  "SUP-01",  6.85, 24.99,  6,  2000,  4200),
    ("BTR-1003", "Lion's Mane Mushroom Grow Kit",        "Indoor Growing", "Mushroom Kits", "Single Kits",  "SUP-02",  7.60, 26.99,  6,  2000,  3100),
    ("BTR-1004", "Mushroom Grow Kit 2-Pack",             "Indoor Growing", "Mushroom Kits", "Multi-Packs",  "SUP-01", 12.10, 39.99,  4,  1500,  2600),
    ("BTR-1005", "Mushroom Kit Holiday Gift Box",        "Indoor Growing", "Mushroom Kits", "Gifting",      "SUP-02", 13.75, 44.99,  4,  1200,  1900),
    # ---- Indoor Growing / Water Garden --------------------------------------
    ("BTR-1101", "Water Garden 3-Gallon Aquaponics",     "Indoor Growing", "Water Garden",  "Systems",      "SUP-03", 28.40, 99.99,  2,  1000,  2350),
    ("BTR-1102", "Water Garden Refill & Seed Kit",       "Indoor Growing", "Water Garden",  "Consumables",  "SUP-03",  4.20, 16.99, 12,  2500,  3400),
    ("BTR-1103", "Self-Watering Countertop Grow Kit",    "Indoor Growing", "Water Garden",  "Systems",      "SUP-04", 18.90, 64.99,  4,   800,  1150),
    # ---- Indoor Growing / Herb & Microgreens --------------------------------
    ("BTR-1201", "Windowsill Herb Kit - Basil",          "Indoor Growing", "Herb Kits",     "Single Herb",  "SUP-04",  4.95, 17.99, 12,  2400,  5600),
    ("BTR-1202", "Windowsill Herb Kit - Cilantro",       "Indoor Growing", "Herb Kits",     "Single Herb",  "SUP-04",  4.95, 17.99, 12,  2400,  3900),
    ("BTR-1203", "Windowsill Herb Kit - Mint",           "Indoor Growing", "Herb Kits",     "Single Herb",  "SUP-04",  4.95, 17.99, 12,  1800,  2700),
    ("BTR-1204", "Herb Garden Trio Gift Set",            "Indoor Growing", "Herb Kits",     "Gifting",      "SUP-03", 11.60, 39.99,  4,  1200,  2100),
    ("BTR-1205", "Microgreens Grow Kit",                 "Indoor Growing", "Herb Kits",     "Microgreens",  "SUP-03",  5.80, 19.99, 12,  1500,  1750),
    # ---- Outdoor Gardening / Seeds ------------------------------------------
    ("BTR-2001", "Organic Seed Variety Pack - 12ct",     "Outdoor Gardening", "Seeds",      "Variety Packs","SUP-05",  3.75, 14.99, 12,  3000,  9800),
    ("BTR-2002", "Organic Heirloom Tomato Seeds",        "Outdoor Gardening", "Seeds",      "Vegetable",    "SUP-05",  0.95,  4.99, 24,  6000, 12400),
    ("BTR-2003", "Organic Culinary Herb Seed Pack",      "Outdoor Gardening", "Seeds",      "Herb",         "SUP-05",  1.10,  5.49, 24,  6000,  8600),
    ("BTR-2004", "Organic Wildflower Pollinator Mix",    "Outdoor Gardening", "Seeds",      "Flower",       "SUP-09",  2.40,  9.99, 24,  4000,  6300),
    ("BTR-2005", "Kids Garden Seed Starter Kit",         "Outdoor Gardening", "Seeds",      "Kids",         "SUP-05",  6.20, 22.99,  8,  1500,  2450),
    # ---- Outdoor Gardening / Soils & Media ----------------------------------
    ("BTR-2101", "Organic Potting Mix 12qt",             "Outdoor Gardening", "Soils & Media", "Potting",   "SUP-06",  4.30, 13.99,  6, 10000, 26500),
    ("BTR-2102", "Organic Seed Starting Mix 8qt",        "Outdoor Gardening", "Soils & Media", "Starting",  "SUP-06",  3.60, 11.99,  6,  8000, 14200),
    ("BTR-2103", "Organic Raised Bed Mix 1.5 cu ft",     "Outdoor Gardening", "Soils & Media", "Raised Bed","SUP-07",  8.90, 24.99,  4,  6000, 11800),
    ("BTR-2104", "Organic Compost Blend 8qt",            "Outdoor Gardening", "Soils & Media", "Amendments","SUP-07",  3.10, 10.49,  6,  8000,  7400),
    # ---- Outdoor Gardening / Garden Kits ------------------------------------
    ("BTR-2201", "Raised Bed Starter Kit",               "Outdoor Gardening", "Garden Kits", "Beds",        "SUP-08", 32.50, 109.99, 1,   600,  1250),
    ("BTR-2202", "Windowsill Planter Box",               "Outdoor Gardening", "Garden Kits", "Planters",    "SUP-08", 14.20,  44.99, 4,  1200,  1600),
    ("BTR-2203", "Kids Pizza Garden Grow Kit",           "Outdoor Gardening", "Garden Kits", "Kids",        "SUP-03",  8.40,  29.99, 6,  1500,  2050),
    ("BTR-2204", "Garden Gift Set - Grow Your Own",      "Outdoor Gardening", "Garden Kits", "Gifting",     "SUP-03", 16.80,  54.99, 4,   900,  1400),
]

# -----------------------------------------------------------------------------
# Seasonality -- monthly multiplicative index by category (Jan..Dec).
# Gardening is a spring business; grow kits are a holiday gifting business.
# -----------------------------------------------------------------------------
SEASONALITY = {
    "Mushroom Kits":  [0.80, 0.78, 0.85, 0.82, 0.80, 0.72, 0.70, 0.78, 0.95, 1.25, 1.85, 2.10],
    "Water Garden":   [0.85, 0.88, 1.05, 1.15, 1.20, 1.05, 0.95, 0.95, 1.00, 1.10, 1.40, 1.42],
    "Herb Kits":      [0.90, 1.00, 1.25, 1.30, 1.35, 1.05, 0.85, 0.85, 0.95, 1.05, 1.25, 1.20],
    "Seeds":          [0.95, 1.55, 2.05, 1.90, 1.35, 0.80, 0.55, 0.50, 0.60, 0.70, 0.55, 0.50],
    "Soils & Media":  [0.60, 1.05, 1.85, 2.15, 1.85, 1.25, 0.85, 0.70, 0.75, 0.70, 0.45, 0.40],
    "Garden Kits":    [0.75, 1.15, 1.70, 1.85, 1.55, 1.00, 0.70, 0.65, 0.80, 0.90, 1.05, 0.95],
}

# -----------------------------------------------------------------------------
# Selling network
#   account, channel, node, demand_share, orders_in_case_packs
# -----------------------------------------------------------------------------
ACCOUNTS = [
    ("Home Depot",   "Retail",    "DC-West",    0.205, True),
    ("Lowe's",       "Retail",    "DC-Central", 0.145, True),
    ("Target",       "Retail",    "DC-Central", 0.130, True),
    ("Walmart",      "Retail",    "DC-East",    0.155, True),
    ("Costco",       "Retail",    "DC-West",    0.090, True),
    ("Kroger",       "Retail",    "DC-Central", 0.055, True),
    ("Whole Foods",  "Retail",    "DC-East",    0.045, True),
    ("Amazon",       "Ecommerce", "DC-East",    0.115, False),
    ("DTC Shopify",  "Ecommerce", "DC-West",    0.060, False),
]

NODES = {
    "DC-West":    ("Stockton, CA",     0.40),
    "DC-Central": ("Dallas, TX",       0.33),
    "DC-East":    ("Harrisburg, PA",   0.27),
}

# SKUs whose account mix skews away from the network default.
CHANNEL_SKEW = {
    "Mushroom Kits": {"Amazon": 1.9, "DTC Shopify": 2.2, "Costco": 1.4, "Kroger": 0.4},
    "Water Garden":  {"Amazon": 2.1, "DTC Shopify": 2.4, "Home Depot": 0.7, "Kroger": 0.2},
    "Soils & Media": {"Home Depot": 1.6, "Lowe's": 1.5, "Amazon": 0.3, "DTC Shopify": 0.2},
    "Seeds":         {"Walmart": 1.3, "Kroger": 1.4, "Whole Foods": 1.3},
}

# SKUs deliberately made hard to plan.
NEW_ITEMS = {"BTR-1005": "2025-08", "BTR-2204": "2025-11"}  # first month with sales
ERRATIC = {"BTR-1103", "BTR-2005"}                          # high demand variability


def month_range(start: pd.Period, end: pd.Period) -> list[pd.Period]:
    return list(pd.period_range(start, end, freq="M"))


def build_dimensions() -> dict[str, pd.DataFrame]:
    dim_supplier = pd.DataFrame(
        SUPPLIERS,
        columns=[
            "supplier_id", "origin_country", "lead_time_days",
            "lead_time_std_days", "origin_expense_rate", "payment_terms",
        ],
    )
    dim_supplier["sourcing_type"] = np.where(
        dim_supplier["origin_country"] == "United States", "Domestic", "Import"
    )

    dim_product = pd.DataFrame(
        PRODUCTS,
        columns=[
            "sku", "sku_name", "division", "category", "subcategory", "supplier_id",
            "fob_unit_cost", "retail_price", "case_pack", "moq_units",
            "base_monthly_units",
        ],
    )
    dim_product = dim_product.merge(
        dim_supplier[["supplier_id", "lead_time_days", "lead_time_std_days",
                      "origin_expense_rate", "sourcing_type"]],
        on="supplier_id", how="left",
    )
    # Landed unit cost = FOB + origin-side expenses (the % this project models).
    dim_product["landed_unit_cost"] = (
        dim_product["fob_unit_cost"] * (1 + dim_product["origin_expense_rate"])
    ).round(4)

    dim_location = pd.DataFrame(
        ACCOUNTS,
        columns=["account", "channel", "node", "demand_share", "orders_in_case_packs"],
    )
    dim_location["node_city"] = dim_location["node"].map(lambda n: NODES[n][0])

    dim_calendar = pd.DataFrame({"month": month_range(HISTORY_START, HISTORY_END + 12)})
    dim_calendar["year"] = dim_calendar["month"].apply(lambda p: p.year)
    dim_calendar["month_no"] = dim_calendar["month"].apply(lambda p: p.month)
    dim_calendar["month_name"] = dim_calendar["month"].apply(
        lambda p: p.strftime("%b %Y")
    )
    dim_calendar["quarter"] = dim_calendar["month"].apply(lambda p: f"Q{(p.month - 1)//3 + 1}")
    dim_calendar["is_history"] = dim_calendar["month"] <= HISTORY_END
    dim_calendar["season"] = dim_calendar["month_no"].map(
        {12: "Holiday", 1: "Winter", 2: "Winter", 3: "Spring", 4: "Spring",
         5: "Spring", 6: "Summer", 7: "Summer", 8: "Summer", 9: "Fall",
         10: "Fall", 11: "Holiday"}
    )
    return {
        "dim_supplier": dim_supplier,
        "dim_product": dim_product,
        "dim_location": dim_location,
        "dim_calendar": dim_calendar,
    }


def account_weights(category: str) -> pd.Series:
    """Account demand mix for a category, renormalised to 1.0."""
    base = pd.Series(
        {a[0]: a[3] for a in ACCOUNTS}, name="share", dtype=float
    )
    skew = CHANNEL_SKEW.get(category, {})
    weighted = base * pd.Series(skew).reindex(base.index).fillna(1.0)
    return weighted / weighted.sum()


def build_sales_history(dims: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Monthly units and revenue by SKU x account, with trend + season + noise."""
    months = month_range(HISTORY_START, HISTORY_END)
    rows: list[dict] = []

    for prod in dims["dim_product"].itertuples():
        season = SEASONALITY[prod.category]
        weights = account_weights(prod.category)
        first_month = pd.Period(NEW_ITEMS[prod.sku], freq="M") if prod.sku in NEW_ITEMS else None
        noise_sd = 0.35 if prod.sku in ERRATIC else 0.13
        # Each SKU gets its own gentle trend: growing, flat or declining.
        annual_trend = RNG.normal(0.08, 0.11)

        for i, month in enumerate(months):
            if first_month is not None and month < first_month:
                continue
            trend = (1 + annual_trend) ** (i / 12)
            seasonal = season[month.month - 1]
            shock = RNG.normal(1.0, noise_sd)
            # Retail promotions: occasional large one-month lift.
            promo = 1.0
            if RNG.random() < 0.06:
                promo = RNG.uniform(1.4, 2.1)
            # New items ramp over their first four months.
            ramp = 1.0
            if first_month is not None:
                age = (month - first_month).n
                ramp = min(1.0, 0.35 + 0.22 * age)

            total_units = prod.base_monthly_units * trend * seasonal * shock * promo * ramp
            total_units = max(total_units, 0.0)

            # Split across accounts; add per-account noise so the mix moves.
            acct_noise = RNG.normal(1.0, 0.10, len(weights))
            split = (weights.to_numpy() * acct_noise)
            split = split / split.sum()

            for acct, frac in zip(weights.index, split):
                units = int(round(total_units * frac))
                if units <= 0:
                    continue
                # Retail sells below list; DTC closer to list.
                realisation = 0.58 if acct not in ("Amazon", "DTC Shopify") else 0.86
                rows.append(
                    {
                        "month": month,
                        "sku": prod.sku,
                        "account": acct,
                        "units": units,
                        "revenue_usd": round(units * prod.retail_price * realisation, 2),
                        "cogs_usd": round(units * prod.landed_unit_cost, 2),
                    }
                )

    return pd.DataFrame(rows)


def build_inventory(dims, sales) -> pd.DataFrame:
    """On-hand position at the planning cut-off, by SKU and node.

    Coverage is intentionally uneven: some SKUs are long, some are already
    short. A planning tool that starts from a perfectly balanced position
    proves nothing.
    """
    recent = (
        sales[sales["month"] > HISTORY_END - 3]
        .groupby("sku", as_index=False)["units"].sum()
    )
    recent["monthly_units"] = recent["units"] / 3

    rows = []
    for r in recent.itertuples():
        months_cover = RNG.uniform(0.4, 4.8)
        total = r.monthly_units * months_cover
        for node, (_city, share) in NODES.items():
            node_noise = RNG.uniform(0.7, 1.35)
            rows.append(
                {
                    "as_of_month": HISTORY_END,
                    "sku": r.sku,
                    "node": node,
                    "on_hand_units": int(round(total * share * node_noise)),
                }
            )
    inv = pd.DataFrame(rows)
    return inv.merge(
        dims["dim_product"][["sku", "landed_unit_cost"]], on="sku", how="left"
    ).assign(
        on_hand_value_usd=lambda d: (d["on_hand_units"] * d["landed_unit_cost"]).round(2)
    ).drop(columns="landed_unit_cost")


def build_open_orders(dims, sales) -> pd.DataFrame:
    """Purchase orders already placed and not yet received (the pipeline)."""
    recent = (
        sales[sales["month"] > HISTORY_END - 3]
        .groupby("sku", as_index=False)["units"].sum()
        .assign(monthly_units=lambda d: d["units"] / 3)
    )
    prod = dims["dim_product"].merge(recent[["sku", "monthly_units"]], on="sku")

    rows, po_no = [], 4100
    for p in prod.itertuples():
        # Long-lead imports have more POs in flight than domestic soil.
        n_orders = 3 if p.lead_time_days > 60 else (2 if p.lead_time_days > 25 else 1)
        if RNG.random() < 0.15:
            n_orders = max(0, n_orders - 1)  # some SKUs have nothing on the water
        for k in range(n_orders):
            po_no += 1
            eta = HISTORY_END + 1 + k + (1 if p.lead_time_days > 90 else 0)
            units = int(round(p.monthly_units * RNG.uniform(0.8, 2.2)))
            units = max(units, p.moq_units // 4)
            rows.append(
                {
                    "po_number": f"PO-{po_no}",
                    "sku": p.sku,
                    "supplier_id": p.supplier_id,
                    "node": RNG.choice(list(NODES)),
                    "eta_month": eta,
                    "units": units,
                    "fob_value_usd": round(units * p.fob_unit_cost, 2),
                    "origin_expense_usd": round(
                        units * p.fob_unit_cost * p.origin_expense_rate, 2
                    ),
                    "status": "In transit" if k == 0 else "Confirmed",
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    cfg = load_config()
    print(f"Generating simulated dataset for profile: {cfg.profile_name}")

    dims = build_dimensions()
    sales = build_sales_history(dims)
    inventory = build_inventory(dims, sales)
    open_orders = build_open_orders(dims, sales)

    out = ROOT / "data" / "warehouse"
    out.mkdir(parents=True, exist_ok=True)

    tables = {
        **dims,
        "fact_sales_history": sales,
        "fact_inventory": inventory,
        "fact_open_orders": open_orders,
    }
    for name, df in tables.items():
        df = df.copy()
        for col in df.columns:
            if str(df[col].dtype).startswith("period"):
                df[col] = df[col].astype(str)
        df.to_csv(out / f"{name}.csv", index=False)
        print(f"  {name:22s} {len(df):>7,} rows")

    span = f"{HISTORY_START} .. {HISTORY_END}"
    total_rev = sales["revenue_usd"].sum()
    print(
        f"\nHistory {span} | {sales['sku'].nunique()} SKUs x "
        f"{sales['account'].nunique()} accounts"
    )
    print(f"Simulated net revenue over 24 months: ${total_rev:,.0f}")
    print(f"On-hand at cut-off: ${inventory['on_hand_value_usd'].sum():,.0f}")
    print(f"Open POs in flight: ${open_orders['fob_value_usd'].sum():,.0f}")
    print(f"\nWritten to {out}")


if __name__ == "__main__":
    main()
