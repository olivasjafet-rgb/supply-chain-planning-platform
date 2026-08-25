"""Landed cost and the three-stage lag of a sourcing saving.

An imported unit costs more than its FOB price: freight, handling,
documentation and inspection land on top. Here that surcharge is measured
**empirically per supplier** -- actual origin expenses divided by actual
purchase value -- rather than assumed as one flat rate for everyone. In the live
version of this model the empirical rates ranged from 0% to 6.7% across
suppliers; a single blended assumption misprices every one of them.

The part that surprises people is the timing. Negotiating the surcharge away
does **not** improve the P&L next month. The saving moves through three gates:

    1. PURCHASE   the day the PO is cut          -> cash / commitment
    2. RECEIPT    when the container lands        -> lagged by the lead time
    3. COGS       when the unit is finally sold   -> lagged again by inventory

Under weighted-average costing, gate 3 is governed by a chained ratio:

    ratio(m) = [ Inv(m-1) * ratio(m-1) + Receipts_new(m) ]
               ---------------------------------------------
               [ Inv(m-1)             + Receipts_old(m) ]

Cheaper receipts dilute the average cost of the pool they join; they do not
replace it. That is why a 4-month inventory turn does **not** mean the benefit
is fully in the P&L by month 4 -- the ratio approaches its floor
asymptotically and never quite lands on it.

One consequence catches people out: **the ratio is not monotonic.** A receipt
that carries no surcharge (a domestic supplier, say) enters the pool at a ratio
of 1.0, which is *above* an already-discounted pool average -- so a month
weighted toward domestic supply pushes the blended ratio back up, and the
reported saving goes backwards. Nothing is wrong when that happens; the ratio
tracks the sourcing mix of what was actually received, not a schedule. Reading
a single month's move as progress or regression is the mistake.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config


def supplier_rates(open_orders: pd.DataFrame, suppliers: pd.DataFrame) -> pd.DataFrame:
    """Empirical origin-expense rate per supplier: expenses / purchase value.

    Computed from the transaction history, not from the rate card. Where a
    supplier has no purchase history the published rate is used as a fallback
    and the row is flagged.
    """
    observed = open_orders.groupby("supplier_id", as_index=False).agg(
        purchase_value_usd=("fob_value_usd", "sum"),
        origin_expense_usd=("origin_expense_usd", "sum"),
        orders=("po_number", "count"),
    )
    observed["empirical_rate"] = np.where(
        observed["purchase_value_usd"] > 0,
        observed["origin_expense_usd"] / observed["purchase_value_usd"],
        np.nan,
    )
    out = suppliers.merge(observed, on="supplier_id", how="left")
    out["rate_source"] = np.where(
        out["empirical_rate"].notna(), "empirical", "published (no purchase history)"
    )
    out["effective_rate"] = out["empirical_rate"].fillna(out["origin_expense_rate"])
    out["applies"] = out["effective_rate"] > 0
    return out


def weighted_average_rate(rates: pd.DataFrame) -> float:
    """Purchase-value-weighted surcharge across the book -- the headline %."""
    value = rates["purchase_value_usd"].fillna(0)
    if value.sum() == 0:
        return float(rates["effective_rate"].mean())
    return float((rates["effective_rate"] * value).sum() / value.sum())


def purchase_savings(
    order_book: pd.DataFrame, rates: pd.DataFrame, cfg: Config
) -> pd.DataFrame:
    """Gate 1 -- the cash effect, recognised the month the PO is placed."""
    effective_from = pd.Period(cfg.landed_cost["scenario"]["effective_from"], freq="M")
    ob = order_book.merge(
        rates[["supplier_id", "effective_rate", "applies"]], on="supplier_id", how="left"
    )
    ob["effective_rate"] = ob["effective_rate"].fillna(0.0)
    ob["in_scope"] = (ob["place_in_month"] >= effective_from) & ob["applies"].fillna(False)

    # order_value_usd is landed; strip the surcharge back out to get FOB.
    ob["fob_value_usd"] = ob["order_value_usd"] / (1 + ob["effective_rate"])
    ob["surcharge_usd"] = ob["order_value_usd"] - ob["fob_value_usd"]
    ob["purchase_saving_usd"] = np.where(ob["in_scope"], ob["surcharge_usd"], 0.0)

    return (
        ob.groupby("place_in_month", as_index=False)
        .agg(
            purchase_value_usd=("order_value_usd", "sum"),
            fob_value_usd=("fob_value_usd", "sum"),
            surcharge_usd=("surcharge_usd", "sum"),
            purchase_saving_usd=("purchase_saving_usd", "sum"),
        )
        .rename(columns={"place_in_month": "month"})
    )


def receipt_savings(
    plan: pd.DataFrame, products: pd.DataFrame, rates: pd.DataFrame, cfg: Config
) -> pd.DataFrame:
    """Gate 2 -- the warehouse effect, recognised when goods are received."""
    item_key = cfg.planning_keys[-1]
    effective_from = pd.Period(cfg.landed_cost["scenario"]["effective_from"], freq="M")

    r = plan[[item_key, "month", "scheduled_receipts", "planned_receipts",
              "unit_cost_usd", "supplier_id"]].copy()
    r["receipt_units"] = r["scheduled_receipts"] + r["planned_receipts"]
    r = r.merge(
        rates[["supplier_id", "effective_rate", "applies"]], on="supplier_id", how="left"
    )
    r["effective_rate"] = r["effective_rate"].fillna(0.0)
    r["applies"] = r["applies"].fillna(False)

    r["receipt_value_current"] = (r["receipt_units"] * r["unit_cost_usd"]).round(2)
    # New cost strips the surcharge for in-scope receipts.
    in_scope = (r["month"] >= effective_from) & r["applies"]
    r["new_unit_cost"] = np.where(
        in_scope, r["unit_cost_usd"] / (1 + r["effective_rate"]), r["unit_cost_usd"]
    )
    r["receipt_value_new"] = (r["receipt_units"] * r["new_unit_cost"]).round(2)
    r["receipt_saving_usd"] = (
        r["receipt_value_current"] - r["receipt_value_new"]
    ).round(2)
    return r


def cogs_dilution(
    plan: pd.DataFrame,
    receipts: pd.DataFrame,
    history: pd.DataFrame,
    products: pd.DataFrame,
    cfg: Config,
) -> pd.DataFrame:
    """Gate 3 -- the P&L effect, diluted through weighted-average costing.

    Runs the chained ratio month by month over the whole book. The ratio starts
    at 1.000 (the opening inventory is entirely at old cost) and falls toward,
    but never reaches, the steady-state floor.
    """
    item_key = cfg.planning_keys[-1]
    months = sorted(plan["month"].unique())

    inv = (
        plan.groupby("month", as_index=False)["closing_value_usd"].sum()
        .set_index("month")["closing_value_usd"]
    )
    rec = receipts.groupby("month", as_index=False).agg(
        receipt_value_current=("receipt_value_current", "sum"),
        receipt_value_new=("receipt_value_new", "sum"),
    ).set_index("month")

    # COGS at current cost = forecast units x current unit cost.
    cogs = (
        plan.assign(cogs=lambda d: d["forecast_units"] * d["unit_cost_usd"])
        .groupby("month", as_index=False)["cogs"].sum()
        .set_index("month")["cogs"]
    )

    rows: list[dict] = []
    ratio_prev = 1.0
    inv_prev = float(plan.loc[plan["month"] == months[0], "opening_units"].mul(
        plan.loc[plan["month"] == months[0], "unit_cost_usd"]
    ).sum())

    for m in months:
        r_cur = float(rec["receipt_value_current"].get(m, 0.0))
        r_new = float(rec["receipt_value_new"].get(m, 0.0))
        denom = inv_prev + r_cur
        ratio = ((inv_prev * ratio_prev) + r_new) / denom if denom > 0 else ratio_prev

        cogs_current = float(cogs.get(m, 0.0))
        cogs_new = cogs_current * ratio
        inv_current = float(inv.get(m, 0.0))

        rows.append(
            {
                "month": m,
                "opening_inventory_usd": round(inv_prev, 2),
                "receipts_current_usd": round(r_cur, 2),
                "receipts_new_usd": round(r_new, 2),
                "receipt_saving_usd": round(r_cur - r_new, 2),
                "cost_ratio": round(ratio, 6),
                "cogs_current_usd": round(cogs_current, 2),
                "cogs_new_usd": round(cogs_new, 2),
                "cogs_saving_usd": round(cogs_current - cogs_new, 2),
                "inventory_current_usd": round(inv_current, 2),
                "inventory_new_usd": round(inv_current * ratio, 2),
                "inventory_saving_usd": round(inv_current * (1 - ratio), 2),
            }
        )
        ratio_prev, inv_prev = ratio, inv_current

    out = pd.DataFrame(rows)
    out["cumulative_receipt_saving"] = out["receipt_saving_usd"].cumsum().round(2)
    out["cumulative_cogs_saving"] = out["cogs_saving_usd"].cumsum().round(2)
    # How much of the receipt-level saving has actually reached the P&L.
    out["pnl_realisation_pct"] = np.where(
        out["cumulative_receipt_saving"] > 0,
        out["cumulative_cogs_saving"] / out["cumulative_receipt_saving"],
        np.nan,
    ).round(4)
    return out


def steady_state(rates: pd.DataFrame) -> dict[str, float]:
    """The asymptote: where the cost ratio settles once the pool has turned over.

    Useful because it answers the board question directly -- "what is this worth
    once it is fully diluted?" -- without waiting for the model to converge.
    """
    w = weighted_average_rate(rates)
    floor_ratio = 1 / (1 + w)
    return {
        "weighted_surcharge_rate": w,
        "steady_state_cost_ratio": floor_ratio,
        "margin_points_gained": 1 - floor_ratio,
    }
