"""Replenishment engine -- time-phased min/max planning.

For every planning item the engine rolls the inventory position forward month by
month over the horizon, and cuts an order whenever the position projected at the
arrival date would fall below the reorder point.

    safety stock  = z * sqrt( LT * sigma_demand^2  +  demand^2 * sigma_LT^2 )
    reorder point = demand over lead time + safety stock          <- the "min"
    order-up-to   = reorder point + demand over the review period <- the "max"
    order qty     = order-up-to - inventory position, rounded to case pack / MOQ

The safety-stock formula carries both sources of variability. Using only demand
variance is the single most common way to under-stock a long-lead import: when a
95-day ocean lead time swings +/- 15 days, the lead-time term dominates.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .config import Config

# Normal service-level multipliers (one-tailed).
Z_TABLE = {
    0.50: 0.00, 0.80: 0.84, 0.85: 1.04, 0.90: 1.28, 0.925: 1.44,
    0.95: 1.645, 0.975: 1.96, 0.98: 2.05, 0.99: 2.33, 0.995: 2.58,
}


def z_for(service_level: float) -> float:
    """Nearest tabulated z. Explicit table beats a scipy dependency here."""
    return Z_TABLE[min(Z_TABLE, key=lambda k: abs(k - service_level))]


def demand_statistics(history: pd.DataFrame, cfg: Config, months: int = 12) -> pd.DataFrame:
    """Mean and standard deviation of monthly demand over the recent window."""
    keys = cfg.planning_keys
    cutoff = history["month"].max() - months
    recent = history[history["month"] > cutoff]
    stats = recent.groupby(keys, as_index=False)["units"].agg(
        avg_monthly_demand="mean", demand_std="std", months_observed="count"
    )
    stats["demand_std"] = stats["demand_std"].fillna(0.0)
    stats["cv"] = np.where(
        stats["avg_monthly_demand"] > 0,
        stats["demand_std"] / stats["avg_monthly_demand"],
        0.0,
    )
    stats["demand_class"] = pd.cut(
        stats["cv"], [-0.01, 0.25, 0.60, np.inf], labels=["Stable", "Variable", "Erratic"]
    )
    return stats


def policy_parameters(
    stats: pd.DataFrame, products: pd.DataFrame, cfg: Config
) -> pd.DataFrame:
    """Safety stock, min (reorder point) and max (order-up-to) per item."""
    keys = cfg.planning_keys
    rep = cfg.replenishment
    review_days = float(rep["review_period_days"])
    method = rep["safety_stock_method"]
    z = z_for(cfg.service_level)

    p = stats.merge(
        products[
            keys[-1:] + ["lead_time_days", "lead_time_std_days", "case_pack",
                         "moq_units", "landed_unit_cost", "supplier_id"]
        ],
        on=keys[-1],
        how="left",
    )

    p["daily_demand"] = p["avg_monthly_demand"] / 30.0
    p["daily_demand_std"] = p["demand_std"] / math.sqrt(30.0)
    p["lead_time_months"] = p["lead_time_days"] / 30.0
    p["demand_during_lt"] = p["daily_demand"] * p["lead_time_days"]

    if method == "service_level":
        demand_var = p["lead_time_days"] * p["daily_demand_std"] ** 2
        lt_var = (p["daily_demand"] ** 2) * (p["lead_time_std_days"] ** 2)
        p["safety_stock"] = (z * np.sqrt(demand_var + lt_var)).round(0)
        p["safety_stock_basis"] = f"service level {cfg.service_level:.0%} (z={z})"
    elif method == "coverage_months":
        months = float(rep["coverage_months_default"])
        p["safety_stock"] = (p["avg_monthly_demand"] * months).round(0)
        p["safety_stock_basis"] = f"{months} months of coverage"
    else:
        raise ValueError(f"unknown safety_stock_method: {method!r}")

    p["reorder_point"] = (p["demand_during_lt"] + p["safety_stock"]).round(0)
    p["order_up_to"] = (
        p["reorder_point"] + p["daily_demand"] * review_days
    ).round(0)
    p["safety_stock_value_usd"] = (p["safety_stock"] * p["landed_unit_cost"]).round(2)
    p["safety_stock_weeks"] = np.where(
        p["avg_monthly_demand"] > 0,
        p["safety_stock"] / p["avg_monthly_demand"] * 4.33,
        np.nan,
    ).round(1)
    return p


def _round_order(qty: float, case_pack: int, moq: int, cfg: Config) -> int:
    """Apply case-pack and minimum-order constraints. Never rounds down to zero."""
    rep = cfg.replenishment
    if qty <= 0:
        return 0
    if rep.get("respect_moq", True) and qty < moq:
        qty = moq
    if rep.get("respect_case_pack", True) and case_pack > 1:
        qty = math.ceil(qty / case_pack) * case_pack
    return int(qty)


def run_mrp(
    forecast: pd.DataFrame,
    policy: pd.DataFrame,
    on_hand: pd.DataFrame,
    open_orders: pd.DataFrame,
    cfg: Config,
) -> pd.DataFrame:
    """Time-phased projection and order plan.

    The loop runs **month outer, item inner**. That ordering matters: a monthly
    cash cap is a constraint shared across every item competing for the same
    budget, so it can only be applied once all of that month's candidate orders
    are on the table. Applying it afterwards, as a filter over a finished plan,
    produces a plan that reports the saving without the consequence -- purchases
    fall, but the projected inventory still contains the receipts that were
    cancelled, so service looks untouched. The service impact of a cash cap is
    the entire reason to model one.

    Returns one row per item per month with opening/closing inventory, the
    scheduled and planned receipts, the projected coverage, and any stockout.
    """
    keys = cfg.planning_keys
    item_key = keys[-1]
    months = sorted(forecast["month"].unique())
    horizon = len(months)
    cap = cfg.replenishment.get("max_monthly_purchase_usd")

    demand = (
        forecast.groupby([item_key, "month"], as_index=False)["final_forecast"]
        .sum()
        .pivot(index=item_key, columns="month", values="final_forecast")
        .reindex(columns=months)
        .fillna(0.0)
    )
    scheduled = (
        open_orders.groupby([item_key, "eta_month"], as_index=False)["units"]
        .sum()
        .pivot(index=item_key, columns="eta_month", values="units")
        .reindex(index=demand.index, columns=months)
        .fillna(0.0)
    )
    opening_stock = (
        on_hand.groupby(item_key)["on_hand_units"].sum().reindex(demand.index).fillna(0.0)
    )
    pol = policy.set_index(item_key)

    items = [i for i in demand.index if i in pol.index]
    d = {i: demand.loc[i].to_numpy(dtype=float) for i in items}
    sched = {i: scheduled.loc[i].to_numpy(dtype=float) for i in items}
    planned = {i: np.zeros(horizon) for i in items}
    closing = {i: float(opening_stock.get(i, 0.0)) for i in items}
    lt_months = {
        i: max(1, int(math.ceil(pol.loc[i, "lead_time_days"] / 30.0))) for i in items
    }

    rows: list[dict] = []
    for m in range(horizon):
        candidates: list[dict] = []
        month_rows: dict = {}

        for i in items:
            pi = pol.loc[i]
            opening = closing[i]
            receipts = sched[i][m] + planned[i][m]
            new_closing = opening + receipts - d[i][m]
            stockout_units = max(0.0, -new_closing)
            closing_pos = max(0.0, new_closing)
            closing[i] = closing_pos

            forward = d[i][m + 1: m + 13]
            months_cover = (
                closing_pos / forward.mean() if len(forward) and forward.mean() > 0 else np.nan
            )

            month_rows[i] = {
                item_key: i,
                "month": months[m],
                "opening_units": round(opening, 0),
                "forecast_units": round(d[i][m], 0),
                "scheduled_receipts": round(sched[i][m], 0),
                "planned_receipts": round(planned[i][m], 0),
                "closing_units": round(closing_pos, 0),
                "stockout_units": round(stockout_units, 0),
                "reorder_point": pi["reorder_point"],
                "safety_stock": pi["safety_stock"],
                "order_up_to": pi["order_up_to"],
                "months_of_cover": round(months_cover, 2) if pd.notna(months_cover) else None,
                "weeks_of_supply": round(months_cover * 4.33, 1) if pd.notna(months_cover) else None,
                "unit_cost_usd": pi["landed_unit_cost"],
                "supplier_id": pi["supplier_id"],
                "lead_time_days": pi["lead_time_days"],
            }

            # --- candidate order, which would land in `arrival` --------------
            arrival = m + lt_months[i]
            if arrival >= horizon:
                continue
            future_receipts = (
                sched[i][m + 1: arrival + 1].sum() + planned[i][m + 1: arrival + 1].sum()
            )
            future_demand = d[i][m + 1: arrival + 1].sum()
            position = closing_pos + future_receipts - future_demand
            if position >= pi["reorder_point"]:
                continue

            qty = _round_order(
                pi["order_up_to"] - position,
                int(pi["case_pack"]) if pd.notna(pi["case_pack"]) else 1,
                int(pi["moq_units"]) if pd.notna(pi["moq_units"]) else 0,
                cfg,
            )
            if qty <= 0:
                continue
            candidates.append(
                {
                    "item": i,
                    "qty": qty,
                    "arrival": arrival,
                    "value": qty * pi["landed_unit_cost"],
                    # Urgency = position as a fraction of the reorder point.
                    # Ranking by absolute weeks of cover instead would starve
                    # fast movers, whose cover is always low by construction.
                    "urgency": position / pi["reorder_point"] if pi["reorder_point"] > 0 else 0.0,
                }
            )

        # --- allocate this month's budget across the candidates -------------
        candidates.sort(key=lambda c: c["urgency"])
        spent = 0.0
        for c in candidates:
            deferred = cap is not None and spent + c["value"] > cap
            i = c["item"]
            if deferred:
                month_rows[i]["order_placed_units"] = 0
                month_rows[i]["order_deferred_units"] = c["qty"]
                month_rows[i]["order_arrival_month"] = None
            else:
                spent += c["value"]
                planned[i][c["arrival"]] += c["qty"]
                month_rows[i]["order_placed_units"] = c["qty"]
                month_rows[i]["order_deferred_units"] = 0
                month_rows[i]["order_arrival_month"] = months[c["arrival"]]

        for i in items:
            row = month_rows[i]
            row.setdefault("order_placed_units", 0)
            row.setdefault("order_deferred_units", 0)
            row.setdefault("order_arrival_month", None)
            rows.append(row)

    plan = pd.DataFrame(rows)
    plan["order_value_usd"] = (plan["order_placed_units"] * plan["unit_cost_usd"]).round(2)
    plan["deferred_value_usd"] = (plan["order_deferred_units"] * plan["unit_cost_usd"]).round(2)
    plan["closing_value_usd"] = (plan["closing_units"] * plan["unit_cost_usd"]).round(2)
    plan["is_stockout"] = plan["stockout_units"] > 0
    return plan.sort_values([item_key, "month"]).reset_index(drop=True)


def order_book(plan: pd.DataFrame, products: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """The actionable output: what to buy, from whom, when to place, when it lands."""
    item_key = cfg.planning_keys[-1]
    orders = plan[plan["order_placed_units"] > 0].copy()
    cols = [c for c in cfg.planning_keys[:-1] if c in products.columns]
    orders = orders.merge(
        products[[item_key, "sku_name", "case_pack", "moq_units"] + cols],
        on=item_key, how="left",
    )
    orders = orders.rename(
        columns={"month": "place_in_month", "order_placed_units": "order_units"}
    )
    keep = (
        [item_key, "sku_name"] + cols
        + ["supplier_id", "lead_time_days", "place_in_month", "order_arrival_month",
           "order_units", "case_pack", "moq_units", "unit_cost_usd", "order_value_usd",
           "reorder_point", "safety_stock", "order_up_to", "weeks_of_supply"]
    )
    return orders[keep].sort_values(["place_in_month", "order_value_usd"], ascending=[True, False])


def frozen_window(plan: pd.DataFrame, policy: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Split projected stockouts into the ones policy can fix and the ones it cannot.

    An order placed in the first month of the cycle does not arrive until its
    lead time has elapsed. For a 95-day ocean lane that is month 4. Any shortage
    before then was determined by the opening inventory and the orders already
    on the water -- it is *locked*, and no safety-stock setting will move it.
    This is why raising the target service level from 95% to 99% can leave the
    projected shortage list completely unchanged: the model is not ignoring the
    policy, the policy simply has no reach into the frozen window.

    Separating the two matters because they carry different recommendations:

        locked      -> expedite, air-freight, substitute, or shape demand
        addressable -> a replenishment parameter change will fix it

    Reporting both under one "stockout" heading invites a planner to fix the
    wrong problem.
    """
    item_key = cfg.planning_keys[-1]
    months = sorted(plan["month"].unique())
    month_index = {m: i for i, m in enumerate(months)}

    lt = policy.set_index(item_key)["lead_time_days"]
    shortages = plan[plan["stockout_units"] > 0].copy()
    if shortages.empty:
        return shortages.assign(horizon_month=[], lead_time_months=[], classification=[])

    shortages["horizon_month"] = shortages["month"].map(month_index)
    shortages["lead_time_months"] = shortages[item_key].map(
        lambda i: max(1, int(math.ceil(lt.get(i, 30) / 30.0)))
    )
    shortages["classification"] = np.where(
        shortages["horizon_month"] < shortages["lead_time_months"],
        "Locked (inside frozen window)",
        "Addressable by policy",
    )
    shortages["recommended_action"] = np.where(
        shortages["classification"].str.startswith("Locked"),
        "Expedite / air freight / shape demand",
        "Review safety stock and reorder point",
    )
    cols = [item_key, "month", "horizon_month", "lead_time_days", "lead_time_months",
            "stockout_units", "classification", "recommended_action"]
    return shortages[cols].sort_values(["classification", "stockout_units"], ascending=[True, False])
