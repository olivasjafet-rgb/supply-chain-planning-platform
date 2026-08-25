"""Supply-chain KPIs and segmentation.

Deliberately few metrics, each tied to a decision:

  weeks of supply   -> is this item long or short right now?
  inventory turns   -> how hard is the working capital working?
  GMROI             -> how much gross margin per dollar of inventory?
  fill rate         -> what did the customer actually experience?
  ABC / XYZ         -> which items deserve a planner's attention at all?

ABC segments by value contribution; XYZ segments by demand variability. The
cross of the two decides the policy: AX items get tight, low-buffer control;
CZ items get a simple rule and no meetings.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config


def abc_xyz(history: pd.DataFrame, stats: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Cross-classify items by revenue contribution (ABC) and variability (XYZ)."""
    item_key = cfg.planning_keys[-1]
    cutoff = history["month"].max() - 12
    recent = history[history["month"] > cutoff]

    value = (
        recent.groupby(item_key, as_index=False)["revenue_usd"].sum()
        .sort_values("revenue_usd", ascending=False)
    )
    total = value["revenue_usd"].sum()
    value["revenue_share"] = value["revenue_usd"] / total if total else 0.0
    value["cumulative_share"] = value["revenue_share"].cumsum()
    value["abc"] = np.select(
        [value["cumulative_share"] <= 0.80, value["cumulative_share"] <= 0.95],
        ["A", "B"],
        default="C",
    )

    xyz = stats[[item_key, "cv", "demand_class"]].copy()
    xyz["xyz"] = np.select(
        [xyz["cv"] <= 0.25, xyz["cv"] <= 0.60], ["X", "Y"], default="Z"
    )

    out = value.merge(xyz, on=item_key, how="left")
    out["segment"] = out["abc"] + out["xyz"].fillna("Z")
    out["review_cadence"] = np.select(
        [out["segment"].isin(["AX", "AY"]), out["abc"] == "A", out["abc"] == "B"],
        ["Weekly", "Weekly", "Monthly"],
        default="Quarterly",
    )
    return out


def inventory_kpis(
    plan: pd.DataFrame, products: pd.DataFrame, policy: pd.DataFrame, cfg: Config
) -> pd.DataFrame:
    """Item-level KPI sheet over the planning horizon.

    The health flag is judged against each item's **own** supply chain, not a
    single company-wide band. A 95-day ocean import that holds 18 weeks of cover
    is running correctly; a 14-day domestic soil SKU holding 18 weeks is not.
    Comparing both to one global threshold produces a report that is wrong about
    half the catalogue.
    """
    item_key = cfg.planning_keys[-1]

    agg = plan.groupby(item_key, as_index=False).agg(
        forecast_units=("forecast_units", "sum"),
        avg_inventory_usd=("closing_value_usd", "mean"),
        ending_inventory_usd=("closing_value_usd", "last"),
        stockout_months=("is_stockout", "sum"),
        stockout_units=("stockout_units", "sum"),
        min_weeks_of_supply=("weeks_of_supply", "min"),
        avg_weeks_of_supply=("weeks_of_supply", "mean"),
        purchase_value_usd=("order_value_usd", "sum"),
        unit_cost_usd=("unit_cost_usd", "first"),
    )
    agg["cogs_usd"] = (agg["forecast_units"] * agg["unit_cost_usd"]).round(2)

    p = products[[item_key, "sku_name", "retail_price"] + cfg.products.above(cfg.planning_level)]
    agg = agg.merge(p, on=item_key, how="left")

    agg["revenue_usd"] = (agg["forecast_units"] * agg["retail_price"] * 0.65).round(2)
    agg["gross_margin_usd"] = (agg["revenue_usd"] - agg["cogs_usd"]).round(2)
    agg["gross_margin_pct"] = np.where(
        agg["revenue_usd"] > 0, agg["gross_margin_usd"] / agg["revenue_usd"], np.nan
    )
    agg["inventory_turns"] = np.where(
        agg["avg_inventory_usd"] > 0, agg["cogs_usd"] / agg["avg_inventory_usd"], np.nan
    )
    agg["gmroi"] = np.where(
        agg["avg_inventory_usd"] > 0,
        agg["gross_margin_usd"] / agg["avg_inventory_usd"],
        np.nan,
    )

    item_key_ = cfg.planning_keys[-1]
    agg = agg.merge(
        policy[[item_key_, "lead_time_days", "safety_stock_weeks", "demand_class"]],
        on=item_key_, how="left",
    )
    # Cover the item ought to carry: its lead time plus its own safety buffer.
    agg["expected_weeks_of_supply"] = (
        agg["lead_time_days"] / 7.0 + agg["safety_stock_weeks"].fillna(0)
    ).round(1)
    agg["cover_vs_expected"] = np.where(
        agg["expected_weeks_of_supply"] > 0,
        agg["avg_weeks_of_supply"] / agg["expected_weeks_of_supply"],
        np.nan,
    )
    agg["health"] = np.select(
        [
            agg["stockout_months"] > 0,
            agg["cover_vs_expected"] > 1.50,
            agg["cover_vs_expected"] < 0.75,
        ],
        ["Stockout risk", "Overstocked", "Thin cover"],
        default="Healthy",
    )
    return agg.sort_values("revenue_usd", ascending=False).reset_index(drop=True)


def network_summary(
    plan: pd.DataFrame, allocation: pd.DataFrame, kpis: pd.DataFrame, cfg: Config
) -> dict[str, float]:
    """The handful of numbers that belong at the top of a dashboard."""
    horizon_months = plan["month"].nunique()
    cogs = float((plan["forecast_units"] * plan["unit_cost_usd"]).sum())
    avg_inv = float(plan.groupby("month")["closing_value_usd"].sum().mean())
    requirement = float(allocation["requirement_units"].sum())
    allocated = float(allocation["allocated_units"].sum())

    return {
        "horizon_months": horizon_months,
        "forecast_units": float(plan["forecast_units"].sum()),
        "planned_purchases_usd": float(plan["order_value_usd"].sum()),
        "cogs_usd": cogs,
        "avg_inventory_usd": avg_inv,
        "ending_inventory_usd": float(
            plan.loc[plan["month"] == plan["month"].max(), "closing_value_usd"].sum()
        ),
        "inventory_turns_annualised": (cogs / avg_inv) * (12 / horizon_months) if avg_inv else np.nan,
        "avg_weeks_of_supply": float(plan["weeks_of_supply"].mean()),
        "fill_rate": allocated / requirement if requirement else np.nan,
        "items_with_stockout": int((kpis["stockout_months"] > 0).sum()),
        "items_overstocked": int((kpis["health"] == "Overstocked").sum()),
        "gmroi": float(
            kpis["gross_margin_usd"].sum() / avg_inv
        ) if avg_inv else np.nan,
    }


def monthly_summary(plan: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Network position by month -- the spine of every chart in the dashboard."""
    m = plan.groupby("month", as_index=False).agg(
        forecast_units=("forecast_units", "sum"),
        receipts_units=("scheduled_receipts", "sum"),
        planned_receipts_units=("planned_receipts", "sum"),
        closing_units=("closing_units", "sum"),
        closing_value_usd=("closing_value_usd", "sum"),
        purchases_usd=("order_value_usd", "sum"),
        stockout_units=("stockout_units", "sum"),
        items_in_stockout=("is_stockout", "sum"),
    )
    m["cogs_usd"] = (
        plan.assign(c=plan["forecast_units"] * plan["unit_cost_usd"])
        .groupby("month")["c"].sum().to_numpy()
    )
    m["weeks_of_supply"] = np.where(
        m["cogs_usd"] > 0,
        m["closing_value_usd"] / (m["cogs_usd"] / 4.33),
        np.nan,
    ).round(1)
    return m
