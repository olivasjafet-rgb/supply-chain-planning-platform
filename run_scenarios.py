"""Scenario simulation -- the trade-off curve behind a planning decision.

    python run_scenarios.py

A single plan answers "what should we buy?". The question a supply chain lead
actually gets asked is different: *what does it cost us to promise 99% instead
of 95%? what breaks if the ocean lane slips three weeks? what service can we
still hold if finance caps the monthly buy?*

Each scenario below re-runs the identical engine with one input changed, so any
difference in the result is attributable to that input and nothing else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import allocation as alloc  # noqa: E402
from src import dataio, forecast, kpis, landed_cost, replenishment  # noqa: E402
from src.config import Config, load_config  # noqa: E402
from src.exports import _write_sheet  # noqa: E402

INVALID_SHEET_CHARS = set(chr(47) + chr(92) + "*?:[]")


def _sheet_name(name: str) -> str:
    """Excel rejects several punctuation characters in sheet names and caps
    them at 31 characters."""
    return "".join("-" if ch in INVALID_SHEET_CHARS else ch for ch in name)[:31]


# -----------------------------------------------------------------------------
# Scenario definitions
#   Each is (label, question it answers, config overrides, product mutation)
# -----------------------------------------------------------------------------
def lead_time_shock(products: pd.DataFrame, extra_days: int = 21) -> pd.DataFrame:
    """Ocean lanes slip. Domestic does not."""
    p = products.copy()
    is_import = p["sourcing_type"] == "Import"
    p.loc[is_import, "lead_time_days"] += extra_days
    p.loc[is_import, "lead_time_std_days"] = (p.loc[is_import, "lead_time_std_days"] * 1.5).round()
    return p


SCENARIOS = [
    {
        "label": "Baseline",
        "question": "The plan of record: 95% service, no cash constraint.",
        "config": {},
        "products": None,
        "demand_multiplier": 1.0,
    },
    {
        "label": "Service 99%",
        "question": "What does promising 99% instead of 95% cost in inventory?",
        "config": {"replenishment": {"target_service_level": 0.99}},
        "products": None,
        "demand_multiplier": 1.0,
    },
    {
        "label": "Service 90%",
        "question": "How much working capital is freed by accepting 90%?",
        "config": {"replenishment": {"target_service_level": 0.90}},
        "products": None,
        "demand_multiplier": 1.0,
    },
    {
        "label": "Cash cap $1.0M/mo",
        "question": "If finance caps the monthly buy, what service survives?",
        "config": {"replenishment": {"max_monthly_purchase_usd": 1_000_000}},
        "products": None,
        "demand_multiplier": 1.0,
    },
    {
        "label": "Ocean +21 days",
        "question": "A three-week slip on every import lane -- where does it hurt?",
        "config": {},
        "products": lead_time_shock,
        "demand_multiplier": 1.0,
    },
    {
        "label": "Demand +15%",
        "question": "A big retail win lands. Can the network absorb it?",
        "config": {},
        "products": None,
        "demand_multiplier": 1.15,
    },
    {
        "label": "Coverage rule (2mo)",
        "question": "What if we buy on months-of-cover instead of statistics?",
        "config": {"replenishment": {"safety_stock_method": "coverage_months"}},
        "products": None,
        "demand_multiplier": 1.0,
    },
]


def run_one(
    cfg: Config,
    tables: dict[str, pd.DataFrame],
    history: pd.DataFrame,
    acct_history: pd.DataFrame,
    fc_base: pd.DataFrame,
    products: pd.DataFrame,
    demand_multiplier: float,
) -> dict:
    """One full pass of the engine. The forecast is shared across scenarios
    unless demand itself is the variable being tested -- so a difference in the
    result cannot come from forecast noise."""
    fc = fc_base.copy()
    if demand_multiplier != 1.0:
        fc["final_forecast"] = (fc["final_forecast"] * demand_multiplier).round(0)

    stats = replenishment.demand_statistics(history, cfg, months=12)
    if demand_multiplier != 1.0:
        stats["avg_monthly_demand"] *= demand_multiplier
        stats["demand_std"] *= demand_multiplier

    policy = replenishment.policy_parameters(stats, products, cfg)
    plan = replenishment.run_mrp(
        fc, policy, tables["fact_inventory"], tables["fact_open_orders"], cfg
    )
    orders = replenishment.order_book(plan, products, cfg)

    mix = alloc.account_mix(acct_history, cfg, months=6)
    allocation = alloc.allocate(plan, mix, tables["dim_location"], cfg)

    item_kpis = kpis.inventory_kpis(plan, products, policy, cfg)
    summary = kpis.network_summary(plan, allocation, item_kpis, cfg)

    rates = landed_cost.supplier_rates(tables["fact_open_orders"], tables["dim_supplier"])
    receipts = landed_cost.receipt_savings(plan, products, rates, cfg)
    dilution = landed_cost.cogs_dilution(plan, receipts, history, products, cfg)

    deferred = float(plan["order_deferred_units"].sum())
    return {
        "plan": plan,
        "orders": orders,
        "policy": policy,
        "allocation": allocation,
        "summary": summary,
        "dilution": dilution,
        "metrics": {
            "Planned purchases": summary["planned_purchases_usd"],
            "Average inventory": summary["avg_inventory_usd"],
            "Ending inventory": summary["ending_inventory_usd"],
            "Safety stock $": float(policy["safety_stock_value_usd"].sum()),
            "Inventory turns": summary["inventory_turns_annualised"],
            "GMROI": summary["gmroi"],
            "Weeks of supply": summary["avg_weeks_of_supply"],
            "Fill rate": summary["fill_rate"],
            "Stockout item-months": float(plan["is_stockout"].sum()),
            "Short units": float(allocation["short_units"].sum()),
            "Items at risk": float(summary["items_with_stockout"]),
            "Orders to place": float(len(orders)),
            "Units deferred by cash cap": deferred,
            "COGS saving (12mo)": float(dilution["cogs_saving_usd"].sum()),
        },
    }


def main() -> None:
    base = load_config()
    tables = dataio.load_warehouse()
    history = dataio.planning_history(tables, base)
    acct_history = dataio.account_history(tables, base)
    base_products = tables["dim_product"]

    print("=" * 78)
    print(f"SCENARIO SIMULATION -- {base.profile_name}")
    print("=" * 78)
    print(f"{len(SCENARIOS)} scenarios, identical engine, one input changed each.\n")

    # One forecast, shared -- so scenario differences are policy, not noise.
    fc_base, _ = forecast.build_forecast(history, base, seasonality_level="category")

    results: dict[str, dict] = {}
    for sc in SCENARIOS:
        cfg = base.with_overrides(**sc["config"]) if sc["config"] else base
        products = sc["products"](base_products) if sc["products"] else base_products
        print(f"  running: {sc['label']:<22} {sc['question']}")
        results[sc["label"]] = run_one(
            cfg, tables, history, acct_history, fc_base, products, sc["demand_multiplier"]
        )

    # ---- comparison table ---------------------------------------------------
    metrics = pd.DataFrame(
        {label: r["metrics"] for label, r in results.items()}
    )
    baseline = metrics["Baseline"]
    delta = metrics.sub(baseline, axis=0)

    print("\n" + "=" * 78)
    print("COMPARISON  (absolute)")
    print("=" * 78)
    def fmt(row_name: str, v: float) -> str:
        if "rate" in row_name.lower():
            return f"{v:.1%}"
        if any(k in row_name for k in ("$", "purchases", "inventory", "saving")):
            return f"${v:,.0f}"
        return f"{v:,.1f}"

    # Built as a fresh string frame: pandas will not accept formatted text
    # written back into float columns.
    show = pd.DataFrame(
        {col: [fmt(idx, metrics.at[idx, col]) for idx in metrics.index]
         for col in metrics.columns},
        index=metrics.index,
    )
    print(show.to_string())

    print("\n" + "=" * 78)
    print("READING THE TRADE-OFF")
    print("=" * 78)
    for label in metrics.columns:
        if label == "Baseline":
            continue
        d = delta[label]
        print(
            f"\n{label}"
            f"\n  inventory {d['Average inventory']:+,.0f}  "
            f"purchases {d['Planned purchases']:+,.0f}  "
            f"fill {d['Fill rate']:+.2%}  "
            f"stockout months {d['Stockout item-months']:+.0f}"
        )

    # A named number for the service question, because it is the one that gets
    # asked in the room.
    inv_99 = delta["Service 99%"]["Average inventory"]
    fill_99 = delta["Service 99%"]["Fill rate"]
    inv_90 = delta["Service 90%"]["Average inventory"]
    fill_90 = delta["Service 90%"]["Fill rate"]
    print(
        f"\nMoving 95% -> 99% costs ${inv_99:,.0f} of average inventory "
        f"and buys {fill_99:+.2%} of fill."
        f"\nMoving 95% -> 90% frees ${-inv_90:,.0f} and gives up {-fill_90:.2%}."
    )

    # ---- workbook -----------------------------------------------------------
    out = ROOT / "outputs" / "back-to-the-roots" / "scenario_comparison.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)

    defs = pd.DataFrame(
        [
            {
                "Scenario": s["label"],
                "Question it answers": s["question"],
                "What changed": (
                    ", ".join(
                        f"{k}.{kk} = {vv}"
                        for k, v in s["config"].items()
                        for kk, vv in v.items()
                    )
                    or ("import lead times +21 days" if s["products"] else "")
                    or (f"demand x {s['demand_multiplier']}" if s["demand_multiplier"] != 1 else "nothing (plan of record)")
                ),
            }
            for s in SCENARIOS
        ]
    )

    comparison = metrics.reset_index().rename(columns={"index": "Metric"})
    deltas = delta.reset_index().rename(columns={"index": "Metric"})

    monthly = []
    for label, r in results.items():
        m = kpis.monthly_summary(r["plan"], base).assign(scenario=label)
        monthly.append(m)
    monthly_all = pd.concat(monthly, ignore_index=True)

    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        _write_sheet(writer, defs, "Scenarios",
                     "Seven runs of the identical engine. Each changes exactly one input.")
        _write_sheet(writer, comparison, "Comparison",
                     "Absolute results side by side.")
        _write_sheet(writer, deltas, "Delta vs Baseline",
                     "Every scenario minus the plan of record. This is the trade-off.")
        _write_sheet(writer, monthly_all, "Monthly by Scenario",
                     "Month-by-month network position for each scenario -- the chart source.")
        for label, r in results.items():
            _write_sheet(
                writer, r["orders"].head(500), _sheet_name(f"Orders {label}"),
                f"Order book under: {label}",
            )

    print(f"\nWorkbook: {out.relative_to(ROOT)}")

    csv_dir = ROOT / "outputs" / "back-to-the-roots" / "powerbi"
    csv_dir.mkdir(parents=True, exist_ok=True)
    monthly_all.assign(month=monthly_all["month"].astype(str)).to_csv(
        csv_dir / "fact_scenario_monthly.csv", index=False
    )
    comparison.to_csv(csv_dir / "fact_scenario_comparison.csv", index=False)
    print("Power BI: fact_scenario_monthly.csv, fact_scenario_comparison.csv")


if __name__ == "__main__":
    main()
