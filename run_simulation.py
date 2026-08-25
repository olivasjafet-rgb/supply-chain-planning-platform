"""End-to-end planning run.

    python run_simulation.py

Executes the full chain -- demand plan, replenishment, allocation, landed-cost
scenario, KPIs -- and writes three kinds of output:

    outputs/planning_run.xlsx      one workbook a planner can actually work in
    outputs/powerbi/*.csv          a star schema ready to load into Power BI
    outputs/planning.db            the same model as SQL, for ad-hoc queries

Every number printed to the console is reproduced in the workbook, so the run
log and the deliverable can never disagree.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import allocation as alloc  # noqa: E402
from src import dataio, forecast, kpis, landed_cost, replenishment  # noqa: E402
from src.config import load_config  # noqa: E402
from src.exports import write_excel_workbook, write_powerbi_star_schema  # noqa: E402


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def money(x: float) -> str:
    return f"${x:,.0f}"


def slug(name: str) -> str:
    """Folder-safe profile name, so two profiles never overwrite each other."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main(config_path: str | None = None) -> dict:
    cfg = load_config(config_path)
    banner(f"PLANNING RUN -- {cfg.profile_name}")
    print(cfg.describe())

    # -- 1. data -------------------------------------------------------------
    tables = dataio.load_warehouse()
    history = dataio.planning_history(tables, cfg)
    acct_history = dataio.account_history(tables, cfg)
    # Master and balances are expressed at whatever grain the profile plans at.
    products = dataio.product_master_at_grain(tables, cfg, history)
    on_hand, open_orders = dataio.balances_at_grain(tables, cfg)
    print(
        f"\nLoaded {history[cfg.planning_level].nunique()} planning items x "
        f"{history['month'].nunique()} months of history "
        f"({history['month'].min()} .. {history['month'].max()})"
    )

    # -- 2. forecast accuracy, measured before anything is trusted -----------
    banner("1. DEMAND PLANNING")
    scored = forecast.backtest(history, cfg, holdout_months=3)
    accuracy = forecast.summarise_accuracy(scored)
    folds = forecast.rolling_backtest(history, cfg, origins=6, horizon=3)
    print(
        f"Backtest (last 3 months held out): "
        f"accuracy {accuracy['accuracy']:.1%} | WMAPE {accuracy['wmape']:.1%} | "
        f"bias {accuracy['bias_pct']:+.1%}"
    )
    print(
        f"Rolling origin ({len(folds)} folds): "
        f"accuracy {folds['accuracy'].mean():.1%} +/- {folds['accuracy'].std():.1%} | "
        f"bias {folds['bias_pct'].mean():+.1%} | worst fold {folds['accuracy'].min():.1%}"
    )
    print(
        f"  {accuracy['skus_over_30pct_error']} of {accuracy['skus_scored']} items "
        f"exceed 30% error -- these are the ones worth a planner's time"
    )

    fc, indices = forecast.build_forecast(history, cfg, seasonality_level="category")
    print(
        f"Forecast built: {len(fc):,} rows, {cfg.horizon_months} months "
        f"({fc['month'].min()} .. {fc['month'].max()})"
    )
    print(f"Total forecast demand: {fc['final_forecast'].sum():,.0f} units")

    peak = (
        fc.groupby("month", as_index=False)["final_forecast"].sum()
        .sort_values("final_forecast", ascending=False).head(1)
    )
    print(
        f"Peak demand month: {peak['month'].iloc[0]} "
        f"({peak['final_forecast'].iloc[0]:,.0f} units)"
    )

    # -- 3. policy -----------------------------------------------------------
    banner("2. REPLENISHMENT POLICY")
    stats = replenishment.demand_statistics(history, cfg, months=12)
    policy = replenishment.policy_parameters(stats, products, cfg)
    z = replenishment.z_for(cfg.service_level)
    print(
        f"Safety stock method: {cfg.replenishment['safety_stock_method']} "
        f"(service level {cfg.service_level:.0%}, z={z})"
    )
    print(f"Total safety stock investment: {money(policy['safety_stock_value_usd'].sum())}")
    print(
        "Demand classes: "
        + ", ".join(
            f"{k} {v}" for k, v in stats["demand_class"].value_counts().items()
        )
    )

    # -- 4. MRP --------------------------------------------------------------
    banner("3. TIME-PHASED REPLENISHMENT")
    plan = replenishment.run_mrp(fc, policy, on_hand, open_orders, cfg)
    orders = replenishment.order_book(plan, products, cfg)
    print(f"Orders to place: {len(orders)} across {orders['supplier_id'].nunique()} suppliers")
    print(f"Total planned purchases: {money(orders['order_value_usd'].sum())}")
    print(f"Projected stockout events: {int(plan['is_stockout'].sum())} item-months")

    frozen = replenishment.frozen_window(plan, policy, cfg)
    if len(frozen):
        locked = frozen[frozen["classification"].str.startswith("Locked")]
        print(
            f"  of which {len(locked)} are inside the frozen window "
            f"({locked['stockout_units'].sum():,.0f} units) -- no safety-stock "
            f"setting can reach them; they need expediting or demand shaping"
        )

    # -- 5. allocation -------------------------------------------------------
    banner("4. DISTRIBUTION & ALLOCATION")
    mix = alloc.account_mix(acct_history, cfg, months=6)
    allocation = alloc.allocate(plan, mix, tables["dim_location"], cfg)
    service = alloc.service_summary(allocation, cfg)
    node_req = alloc.node_requirements(plan, mix, tables["dim_location"], cfg)
    overall_fill = (
        allocation["allocated_units"].sum() / allocation["requirement_units"].sum()
    )
    print(f"Network fill rate: {overall_fill:.1%}")
    worst = service.nsmallest(3, "fill_rate")
    for r in worst.itertuples():
        print(f"  {r.account:<14} fill {r.fill_rate:6.1%}  short {r.short_units:>10,.0f} units")

    # -- 6. landed cost ------------------------------------------------------
    banner("5. LANDED-COST SCENARIO")
    rates = landed_cost.supplier_rates(open_orders, tables["dim_supplier"])
    ss = landed_cost.steady_state(rates)
    print(f"Scenario: {cfg.landed_cost['scenario']['name']} "
          f"(effective {cfg.landed_cost['scenario']['effective_from']})")
    print(
        f"Weighted origin surcharge: {ss['weighted_surcharge_rate']:.2%} "
        f"across {int(rates['applies'].sum())} of {len(rates)} suppliers"
    )
    print(
        f"Steady-state cost ratio: {ss['steady_state_cost_ratio']:.4f} "
        f"-> {ss['margin_points_gained']:.2%} of COGS, once fully diluted"
    )

    purch = landed_cost.purchase_savings(orders, rates, cfg)
    receipts = landed_cost.receipt_savings(plan, products, rates, cfg)
    dilution = landed_cost.cogs_dilution(plan, receipts, history, products, cfg)

    print(f"\n  Gate 1  purchase savings (cash)   {money(purch['purchase_saving_usd'].sum())}")
    print(f"  Gate 2  receipt savings (goods)   {money(receipts['receipt_saving_usd'].sum())}")
    print(f"  Gate 3  COGS savings (P&L)        {money(dilution['cogs_saving_usd'].sum())}")
    final = dilution.iloc[-1]
    print(
        f"\n  After {len(dilution)} months the cost ratio is {final['cost_ratio']:.4f} "
        f"and only {final['pnl_realisation_pct']:.0%} of the receipt-level saving "
        f"has reached the P&L."
    )
    print("  The rest is sitting in inventory, waiting to be sold.")

    # -- 7. KPIs -------------------------------------------------------------
    banner("6. KPIs")
    item_kpis = kpis.inventory_kpis(plan, products, policy, cfg)
    segments = kpis.abc_xyz(history, stats, cfg)
    monthly = kpis.monthly_summary(plan, cfg)
    summary = kpis.network_summary(plan, allocation, item_kpis, cfg)

    print(f"Planned purchases      {money(summary['planned_purchases_usd'])}")
    print(f"Average inventory      {money(summary['avg_inventory_usd'])}")
    print(f"Inventory turns (ann.) {summary['inventory_turns_annualised']:.1f}x")
    print(f"GMROI                  {summary['gmroi']:.2f}")
    print(f"Avg weeks of supply    {summary['avg_weeks_of_supply']:.1f}")
    print(f"Items at stockout risk {summary['items_with_stockout']}")
    print(f"Items overstocked      {summary['items_overstocked']}")
    print(
        "Segments: "
        + ", ".join(f"{k} {v}" for k, v in segments["segment"].value_counts().head(5).items())
    )

    # -- 8. outputs ----------------------------------------------------------
    banner("7. OUTPUTS")
    results = {
        "config": cfg,
        "accuracy": accuracy,
        "forecast_scores": scored,
        "rolling_backtest": folds,
        "seasonal_indices": indices,
        "forecast": fc,
        "policy": policy,
        "plan": plan,
        "orders": orders,
        "frozen_window": frozen,
        "allocation": allocation,
        "service": service,
        "node_requirements": node_req,
        "supplier_rates": rates,
        "purchase_savings": purch,
        "receipt_savings": receipts,
        "cogs_dilution": dilution,
        "steady_state": ss,
        "item_kpis": item_kpis,
        "segments": segments,
        "monthly": monthly,
        "summary": summary,
        "tables": tables,
    }

    out_dir = ROOT / "outputs" / slug(cfg.profile_name)
    xlsx = write_excel_workbook(results, out_dir / "planning_run.xlsx")
    print(f"  workbook   {xlsx.relative_to(ROOT)}")
    n = write_powerbi_star_schema(results, out_dir / "powerbi")
    print(f"  Power BI   {n} tables -> {(out_dir / 'powerbi').relative_to(ROOT)}/")
    dataio.to_sql(tables, out_dir / "planning.db")
    print(f"  SQLite     {(out_dir / 'planning.db').relative_to(ROOT)}")

    return results


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
