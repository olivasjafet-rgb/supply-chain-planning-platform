"""Data access layer.

Reads the star schema in ``data/warehouse/``. In production this is the only
module that changes when the source moves -- CSV today, SQL Server or NetSuite
tomorrow; everything downstream keeps working against the same frames.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import Config

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "data" / "warehouse"

PERIOD_COLUMNS = {"month", "eta_month", "as_of_month", "order_arrival_month"}


def _read(name: str, path: Path | None = None) -> pd.DataFrame:
    df = pd.read_csv((path or WAREHOUSE) / f"{name}.csv")
    for col in df.columns:
        if col in PERIOD_COLUMNS:
            df[col] = pd.PeriodIndex(df[col], freq="M")
    return df


def load_warehouse(path: Path | None = None) -> dict[str, pd.DataFrame]:
    """Load every table of the star schema."""
    names = [
        "dim_product", "dim_supplier", "dim_location", "dim_calendar",
        "fact_sales_history", "fact_inventory", "fact_open_orders",
    ]
    return {n: _read(n, path) for n in names}


def planning_history(tables: dict[str, pd.DataFrame], cfg: Config) -> pd.DataFrame:
    """Sales history collapsed to the planning grain, with the hierarchy attached.

    One row per planning key per month. Months with no sales are filled with
    zero so that averages and seasonal indices are not silently computed over a
    shorter series than they appear to be.
    """
    item_key = cfg.planning_keys[-1]
    levels = cfg.products.above(cfg.planning_level)

    sales = tables["fact_sales_history"]
    # Sales arrive at SKU grain. If the profile plans coarser, attach the
    # planning level before aggregating rather than after.
    if item_key not in sales.columns:
        sales = sales.merge(tables["dim_product"][["sku", item_key]], on="sku", how="left")
    grain = sales.groupby([item_key, "month"], as_index=False).agg(
        units=("units", "sum"),
        revenue_usd=("revenue_usd", "sum"),
        cogs_usd=("cogs_usd", "sum"),
    )

    months = pd.period_range(sales["month"].min(), sales["month"].max(), freq="M")
    items = tables["dim_product"][[item_key] + levels].drop_duplicates(subset=[item_key])
    full = items.merge(pd.DataFrame({"month": months}), how="cross")

    out = full.merge(grain, on=[item_key, "month"], how="left")
    for col in ("units", "revenue_usd", "cogs_usd"):
        out[col] = out[col].fillna(0.0)
    return out.sort_values([item_key, "month"]).reset_index(drop=True)


def account_history(tables: dict[str, pd.DataFrame], cfg: Config) -> pd.DataFrame:
    """Sales history at planning-item x account, used for the allocation mix.

    Sales land at SKU grain. When the plan runs coarser, the mix has to be
    rebuilt at the planning grain -- an account's share of a category is not the
    average of its shares of that category's SKUs.
    """
    item_key = cfg.planning_keys[-1]
    sales = tables["fact_sales_history"]
    if item_key not in sales.columns:
        sales = sales.merge(tables["dim_product"][["sku", item_key]], on="sku", how="left")
    return (
        sales.groupby([item_key, "account", "month"], as_index=False)[
            ["units", "revenue_usd"]
        ].sum()
    )


def to_sql(tables: dict[str, pd.DataFrame], db_path: Path) -> None:
    """Materialise the star schema into SQLite.

    Present so the same model can be queried with SQL -- and so the Power BI
    export has a single, typed source rather than a folder of loose CSVs.
    """
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        for name, df in tables.items():
            frame = df.copy()
            for col in frame.columns:
                if str(frame[col].dtype).startswith("period"):
                    frame[col] = frame[col].astype(str)
            frame.to_sql(name, conn, if_exists="replace", index=False)


# -----------------------------------------------------------------------------
# Grain adaptation
# -----------------------------------------------------------------------------
#  The source data is always at its natural transaction grain (SKU). The plan
#  may be run coarser -- some businesses plan at category or class level and let
#  the buyer split the order. When the configured planning level sits above the
#  data's grain, the master and the balances have to be rolled up to match, and
#  the attributes have to be aggregated the way each one actually behaves:
#
#      lead time     -> demand-weighted mean  (a plan for the group waits for
#                       the mix that group actually buys, not for its slowest
#                       or its fastest member)
#      unit cost     -> demand-weighted mean
#      case pack     -> max      (respect the coarsest packaging constraint)
#      MOQ           -> sum      (a group order must clear every member's floor)
#
#  Taking a plain average of lead times here is the classic error: it silently
#  under-buffers every long-lead item in the group.
# -----------------------------------------------------------------------------

def _weighted(values: pd.Series, weights: pd.Series) -> float:
    w = weights.fillna(0)
    if w.sum() <= 0:
        return float(values.mean())
    return float((values * w).sum() / w.sum())


def product_master_at_grain(
    tables: dict[str, pd.DataFrame], cfg: Config, history: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Product master expressed at the configured planning grain."""
    products = tables["dim_product"]
    leaf = cfg.planning_level
    source_leaf = "sku"

    if leaf == source_leaf:
        return products

    levels = cfg.products.through(leaf)
    # Weights must come from SKU-grain sales: `history` has already been rolled
    # up to the planning grain by this point and no longer has a SKU column.
    sales = tables.get("fact_sales_history")
    if sales is not None and source_leaf in sales.columns:
        weights = sales.groupby(source_leaf, as_index=False)["units"].sum()
    else:
        weights = products.assign(units=products["base_monthly_units"])[[source_leaf, "units"]]
    p = products.merge(weights, on=source_leaf, how="left")
    p["units"] = p["units"].fillna(0)

    rows = []
    for key, g in p.groupby(levels, sort=False):
        key = key if isinstance(key, tuple) else (key,)
        row = dict(zip(levels, key))
        row.update(
            {
                "sku_name": f"{row[leaf]} ({len(g)} SKUs)",
                "supplier_id": g.loc[g["units"].idxmax(), "supplier_id"]
                if g["units"].sum() > 0 else g["supplier_id"].iloc[0],
                "lead_time_days": _weighted(g["lead_time_days"], g["units"]),
                "lead_time_std_days": _weighted(g["lead_time_std_days"], g["units"]),
                "fob_unit_cost": _weighted(g["fob_unit_cost"], g["units"]),
                "landed_unit_cost": _weighted(g["landed_unit_cost"], g["units"]),
                "retail_price": _weighted(g["retail_price"], g["units"]),
                "case_pack": int(g["case_pack"].max()),
                "moq_units": int(g["moq_units"].sum()),
                "origin_expense_rate": _weighted(g["origin_expense_rate"], g["units"]),
                "sourcing_type": g.loc[g["units"].idxmax(), "sourcing_type"]
                if g["units"].sum() > 0 else g["sourcing_type"].iloc[0],
                "sku_count": len(g),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def balances_at_grain(
    tables: dict[str, pd.DataFrame], cfg: Config
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """On-hand inventory and open orders rolled up to the planning grain."""
    leaf = cfg.planning_level
    inv, po = tables["fact_inventory"], tables["fact_open_orders"]
    if leaf == "sku":
        return inv, po

    mapping = tables["dim_product"][["sku", leaf]]
    inv = inv.merge(mapping, on="sku", how="left").groupby(
        [leaf, "node", "as_of_month"], as_index=False
    )[["on_hand_units", "on_hand_value_usd"]].sum()
    po = po.merge(mapping, on="sku", how="left").groupby(
        [leaf, "supplier_id", "eta_month", "status"], as_index=False
    )[["units", "fob_value_usd", "origin_expense_usd"]].sum()
    po["po_number"] = ["PO-GRP-" + str(i) for i in range(len(po))]
    return inv, po
