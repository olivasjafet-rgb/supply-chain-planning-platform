"""Distribution planning -- who gets the units when there are not enough.

Two questions this module answers:

  1. **Where should inventory sit?** Network supply is split across fulfilment
     nodes in proportion to the demand each node actually serves, not evenly.
  2. **Who gets cut when supply is short?** Straight pro-rata fair-share is the
     wrong default for retail: an account that drops below its minimum
     presentation stock loses the shelf, and losing the shelf costs far more
     than the units. So minimums are protected first, and only the remainder is
     fair-shared.

Both behaviours are visible in the output (``fill_rate_pct``, ``short_units``,
``protected_minimum``) so a planner can argue with the machine.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config


def account_mix(history: pd.DataFrame, cfg: Config, months: int = 6) -> pd.DataFrame:
    """Recent share of demand by item x account, renormalised to 1.0 per item.

    Six months, not twenty-four: account mix drifts (a new listing at Costco,
    a lost door at Kroger) and stale shares mis-route inventory.
    """
    item_key = cfg.planning_keys[-1]
    cutoff = history["month"].max() - months
    recent = history[history["month"] > cutoff]

    by_acct = recent.groupby([item_key, "account"], as_index=False)["units"].sum()
    total = by_acct.groupby(item_key, as_index=False)["units"].sum().rename(
        columns={"units": "item_total"}
    )
    mix = by_acct.merge(total, on=item_key)
    mix["demand_share"] = np.where(
        mix["item_total"] > 0, mix["units"] / mix["item_total"], 0.0
    )
    return mix[[item_key, "account", "demand_share"]]


def node_requirements(
    plan: pd.DataFrame, mix: pd.DataFrame, locations: pd.DataFrame, cfg: Config
) -> pd.DataFrame:
    """Roll account-level demand up to the node that ships it."""
    item_key = cfg.planning_keys[-1]
    demand = plan[[item_key, "month", "forecast_units"]].merge(mix, on=item_key, how="left")
    demand["account_units"] = (demand["forecast_units"] * demand["demand_share"]).round(0)
    demand = demand.merge(
        locations[["account", "channel", "node"]], on="account", how="left"
    )
    return demand.groupby(
        [item_key, "month", "node"], as_index=False
    )["account_units"].sum().rename(columns={"account_units": "node_demand_units"})


def allocate(
    plan: pd.DataFrame,
    mix: pd.DataFrame,
    locations: pd.DataFrame,
    cfg: Config,
    min_presentation_weeks: float = 1.0,
) -> pd.DataFrame:
    """Allocate each month's available supply across accounts.

    Available supply for a month = opening inventory + receipts. Requirement is
    the account's share of that month's forecast. When supply < requirement the
    engine protects each retail account's minimum presentation stock first, then
    fair-shares what is left.
    """
    item_key = cfg.planning_keys[-1]

    supply = plan[[item_key, "month", "opening_units", "scheduled_receipts",
                   "planned_receipts", "forecast_units", "unit_cost_usd"]].copy()
    supply["available_units"] = (
        supply["opening_units"] + supply["scheduled_receipts"] + supply["planned_receipts"]
    )

    a = supply.merge(mix, on=item_key, how="left").merge(
        locations[["account", "channel", "node", "orders_in_case_packs"]],
        on="account", how="left",
    )
    a["requirement_units"] = (a["forecast_units"] * a["demand_share"]).round(0)

    # Minimum presentation stock: retail doors only, expressed in weeks of demand.
    weekly = a["requirement_units"] / 4.33
    a["protected_minimum"] = np.where(
        a["channel"] == "Retail", (weekly * min_presentation_weeks).round(0), 0.0
    )
    a["protected_minimum"] = np.minimum(a["protected_minimum"], a["requirement_units"])

    out: list[pd.DataFrame] = []
    for (item, month), g in a.groupby([item_key, "month"], sort=False):
        available = float(g["available_units"].iloc[0])
        need = g["requirement_units"].to_numpy(dtype=float)
        floor = g["protected_minimum"].to_numpy(dtype=float)
        g = g.copy()

        if available >= need.sum():
            allocated = need
        else:
            # Pass 1: protect the minimums, pro-rata if even those do not fit.
            if available <= floor.sum():
                allocated = floor * (available / floor.sum()) if floor.sum() > 0 else np.zeros_like(need)
            else:
                remaining = available - floor.sum()
                extra_need = need - floor
                share = np.where(
                    extra_need.sum() > 0, extra_need / extra_need.sum(), 0.0
                )
                allocated = floor + remaining * share

        g["allocated_units"] = np.floor(allocated)
        g["short_units"] = (g["requirement_units"] - g["allocated_units"]).clip(lower=0)
        g["fill_rate_pct"] = np.where(
            g["requirement_units"] > 0,
            g["allocated_units"] / g["requirement_units"],
            1.0,
        )
        out.append(g)

    result = pd.concat(out, ignore_index=True)
    result["allocated_value_usd"] = (
        result["allocated_units"] * result["unit_cost_usd"]
    ).round(2)
    cols = [item_key, "month", "channel", "account", "node", "demand_share",
            "requirement_units", "protected_minimum", "allocated_units",
            "short_units", "fill_rate_pct", "allocated_value_usd"]
    return result[cols].sort_values([item_key, "month", "account"]).reset_index(drop=True)


def service_summary(allocation: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Fill rate by account -- the number a customer-service review opens with."""
    return (
        allocation.groupby(["channel", "account"], as_index=False)
        .agg(
            requirement_units=("requirement_units", "sum"),
            allocated_units=("allocated_units", "sum"),
            short_units=("short_units", "sum"),
        )
        .assign(
            fill_rate=lambda d: np.where(
                d["requirement_units"] > 0,
                d["allocated_units"] / d["requirement_units"],
                1.0,
            )
        )
        .sort_values("requirement_units", ascending=False)
        .reset_index(drop=True)
    )
