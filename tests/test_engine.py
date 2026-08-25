"""Tests for the planning engine.

These check the properties that would be expensive to get wrong: conservation of
inventory across the projection, constraints actually binding, the direction of
each policy lever, and the arithmetic of the cost-dilution chain.

Three of them are regression tests for defects this model actually had. They are
marked as such, because a test that documents a real failure is worth more than
one that documents an intention.

Run:  python -m pytest tests/ -q      (or: python tests/test_engine.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import allocation as alloc
from src import dataio, forecast, landed_cost, replenishment
from src.config import load_config


def _fixture():
    cfg = load_config()
    tables = dataio.load_warehouse()
    history = dataio.planning_history(tables, cfg)
    fc, _ = forecast.build_forecast(history, cfg, seasonality_level="category")
    stats = replenishment.demand_statistics(history, cfg, months=12)
    policy = replenishment.policy_parameters(stats, tables["dim_product"], cfg)
    plan = replenishment.run_mrp(
        fc, policy, tables["fact_inventory"], tables["fact_open_orders"], cfg
    )
    return cfg, tables, history, fc, stats, policy, plan


CFG, TABLES, HISTORY, FC, STATS, POLICY, PLAN = _fixture()


# -- inventory identity -------------------------------------------------------

def test_inventory_balance_holds_every_month():
    """closing = opening + receipts - demand + stockout, for every row.

    The stockout term matters: closing is floored at zero, so without adding the
    shortfall back the identity fails on exactly the rows that matter most.
    """
    lhs = PLAN["closing_units"]
    rhs = (
        PLAN["opening_units"]
        + PLAN["scheduled_receipts"]
        + PLAN["planned_receipts"]
        - PLAN["forecast_units"]
        + PLAN["stockout_units"]
    )
    assert np.allclose(lhs, rhs, atol=1.0), "inventory identity broken"


def test_opening_chains_to_previous_closing():
    item = PLAN[CFG.planning_level].iloc[0]
    g = PLAN[PLAN[CFG.planning_level] == item].sort_values("month")
    assert np.allclose(
        g["opening_units"].to_numpy()[1:], g["closing_units"].to_numpy()[:-1], atol=1.0
    )


# -- constraints --------------------------------------------------------------

def test_orders_respect_case_pack_and_moq():
    orders = replenishment.order_book(PLAN, TABLES["dim_product"], CFG)
    bad_pack = orders[orders["order_units"] % orders["case_pack"] != 0]
    assert bad_pack.empty, f"{len(bad_pack)} orders break case pack"
    bad_moq = orders[orders["order_units"] < orders["moq_units"]]
    assert bad_moq.empty, f"{len(bad_moq)} orders fall below MOQ"


def test_cash_cap_binds_and_costs_service():
    """REGRESSION. The cap was originally applied after the MRP had already
    projected the receipts it cancelled, so it reported the saving with no
    impact on service at all. A binding cap must show both."""
    cap = 1_000_000
    capped_cfg = CFG.with_overrides(replenishment={"max_monthly_purchase_usd": cap})
    capped = replenishment.run_mrp(
        FC, POLICY, TABLES["fact_inventory"], TABLES["fact_open_orders"], capped_cfg
    )
    monthly = capped.groupby("month")["order_value_usd"].sum()
    assert (monthly <= cap + 1).all(), "cap breached"
    assert capped["order_value_usd"].sum() < PLAN["order_value_usd"].sum()
    assert capped["stockout_units"].sum() > PLAN["stockout_units"].sum(), (
        "a binding cash cap that costs no service means the deferred receipts "
        "are still sitting in the projection"
    )


# -- policy levers point the right way ---------------------------------------

def test_higher_service_level_raises_safety_stock():
    low = replenishment.policy_parameters(
        STATS, TABLES["dim_product"],
        CFG.with_overrides(replenishment={"target_service_level": 0.90}),
    )
    high = replenishment.policy_parameters(
        STATS, TABLES["dim_product"],
        CFG.with_overrides(replenishment={"target_service_level": 0.99}),
    )
    assert high["safety_stock"].sum() > low["safety_stock"].sum()


def test_lead_time_variability_increases_the_buffer():
    """Zeroing the lead-time term must lower safety stock -- proof the second
    term of the formula is actually carrying weight."""
    products = TABLES["dim_product"].copy()
    products["lead_time_std_days"] = 0
    no_lt_var = replenishment.policy_parameters(STATS, products, CFG)
    assert no_lt_var["safety_stock"].sum() < POLICY["safety_stock"].sum()


def test_frozen_window_classifies_early_shortages_as_locked():
    fw = replenishment.frozen_window(PLAN, POLICY, CFG)
    if len(fw):
        early = fw[fw["horizon_month"] < fw["lead_time_months"]]
        assert early["classification"].str.startswith("Locked").all()


# -- forecasting --------------------------------------------------------------

def test_seasonal_indices_average_to_one():
    idx = forecast.seasonal_indices(HISTORY, CFG, "category")
    means = idx.groupby("category")["seasonal_index_raw"].mean()
    assert np.allclose(means.to_numpy(), 1.0, atol=0.05), "indices not normalised"


def test_seasonality_survives_a_shorter_history():
    """REGRESSION. A hard 'needs 2 full years' gate silently flattened every
    index during the backtest -- 21 months / 12 = 1.75 -- and dropped measured
    accuracy to 18%. Evidence-weighted shrinkage replaced the cliff."""
    short = HISTORY[HISTORY["month"] > HISTORY["month"].max() - 21]
    idx = forecast.seasonal_indices(short, CFG, "category")
    assert idx["seasonal_index"].std() > 0.05, "seasonality collapsed to flat"


def test_overrides_never_destroy_the_statistical_value():
    keys = CFG.planning_keys + ["month"]
    ov = FC[keys].head(5).copy()
    ov["manual_override"] = 12345.0
    out = forecast.apply_overrides(FC, ov, CFG)
    touched = out[out["override_source"] == "planner override"]
    assert len(touched) == 5
    assert (touched["final_forecast"] == 12345.0).all()
    assert touched["statistical_forecast"].notna().all()


def test_rolling_backtest_returns_several_folds():
    """REGRESSION. Parameters chosen against a single holdout read +13% bias;
    across six origins the same settings read -9%. One fold is not evidence."""
    folds = forecast.rolling_backtest(HISTORY, CFG, origins=4, horizon=3)
    assert len(folds) >= 3
    assert folds["accuracy"].between(0, 1).all()


def test_forecast_is_non_negative_and_covers_the_horizon():
    assert (FC["final_forecast"] >= 0).all()
    assert FC["month"].nunique() == CFG.horizon_months


# -- allocation ---------------------------------------------------------------

def test_allocation_never_exceeds_requirement():
    mix = alloc.account_mix(dataio.account_history(TABLES, CFG), CFG, months=6)
    a = alloc.allocate(PLAN, mix, TABLES["dim_location"], CFG)
    assert (a["allocated_units"] <= a["requirement_units"] + 1).all()
    assert (a["fill_rate_pct"] <= 1.0 + 1e-9).all()


def test_minimum_presentation_stock_is_protected_when_short():
    """Retail floors are honoured before the remainder is fair-shared."""
    mix = alloc.account_mix(dataio.account_history(TABLES, CFG), CFG, months=6)
    a = alloc.allocate(PLAN, mix, TABLES["dim_location"], CFG)
    short = a[(a["short_units"] > 0) & (a["protected_minimum"] > 0)]
    if len(short):
        assert (short["allocated_units"] >= short["protected_minimum"] - 1).all()


def test_account_mix_sums_to_one_per_item():
    mix = alloc.account_mix(dataio.account_history(TABLES, CFG), CFG, months=6)
    totals = mix.groupby(CFG.planning_level)["demand_share"].sum()
    assert np.allclose(totals.to_numpy(), 1.0, atol=1e-6)


# -- landed cost --------------------------------------------------------------

def _dilution():
    rates = landed_cost.supplier_rates(TABLES["fact_open_orders"], TABLES["dim_supplier"])
    receipts = landed_cost.receipt_savings(PLAN, TABLES["dim_product"], rates, CFG)
    return rates, landed_cost.cogs_dilution(
        PLAN, receipts, HISTORY, TABLES["dim_product"], CFG
    )


def test_cost_ratio_stays_between_its_floor_and_one():
    """The ratio is bounded, and it declines overall -- but it is deliberately
    NOT asserted to be monotonic. A month weighted toward suppliers that carry
    no surcharge admits receipts at a ratio of 1.0, which is above an already
    discounted pool, so the blended ratio moves back up. That is correct
    weighted-average behaviour, not drift."""
    rates, d = _dilution()
    floor = landed_cost.steady_state(rates)["steady_state_cost_ratio"]
    assert d["cost_ratio"].iloc[0] <= 1.0
    assert (d["cost_ratio"] >= floor - 1e-9).all(), "ratio fell through its asymptote"
    assert (d["cost_ratio"] <= 1.0 + 1e-9).all(), "ratio rose above par"
    assert d["cost_ratio"].iloc[-1] < d["cost_ratio"].iloc[0], "no dilution at all"


def test_pnl_lags_the_warehouse():
    """The whole point of the model: COGS savings trail receipt savings."""
    _rates, d = _dilution()
    assert d["cumulative_cogs_saving"].iloc[-1] < d["cumulative_receipt_saving"].iloc[-1]
    assert 0 < d["pnl_realisation_pct"].iloc[-1] < 1


def test_domestic_suppliers_generate_no_surcharge_saving():
    """A supplier with no origin expense must never be credited with a saving."""
    rates = landed_cost.supplier_rates(TABLES["fact_open_orders"], TABLES["dim_supplier"])
    domestic = rates[rates["sourcing_type"] == "Domestic"]
    assert (domestic["effective_rate"] == 0).all()
    assert not domestic["applies"].any()


# -- configuration portability ------------------------------------------------

def test_second_profile_runs_at_a_different_grain():
    cfg2 = load_config(ROOT / "profiles" / "category_planning.yaml")
    assert cfg2.planning_level != CFG.planning_level
    h2 = dataio.planning_history(TABLES, cfg2)
    p2 = dataio.product_master_at_grain(TABLES, cfg2, h2)
    inv2, po2 = dataio.balances_at_grain(TABLES, cfg2)
    fc2, _ = forecast.build_forecast(h2, cfg2, seasonality_level="category")
    s2 = replenishment.demand_statistics(h2, cfg2, months=12)
    pol2 = replenishment.policy_parameters(s2, p2, cfg2)
    plan2 = replenishment.run_mrp(fc2, pol2, inv2, po2, cfg2)
    assert len(plan2) > 0
    assert plan2[cfg2.planning_level].nunique() < PLAN[CFG.planning_level].nunique()


def test_grain_rollup_weights_lead_time_by_demand():
    """A plain mean of lead times would under-buffer every long-lead item in
    the group; the rollup must be demand-weighted."""
    cfg2 = load_config(ROOT / "profiles" / "category_planning.yaml")
    h2 = dataio.planning_history(TABLES, cfg2)
    p2 = dataio.product_master_at_grain(TABLES, cfg2, h2)
    src = TABLES["dim_product"]
    for cat in p2["category"]:
        members = src[src["category"] == cat]["lead_time_days"]
        rolled = float(p2.loc[p2["category"] == cat, "lead_time_days"].iloc[0])
        assert members.min() - 1 <= rolled <= members.max() + 1


def test_config_rejects_an_undefined_planning_level():
    import os
    import tempfile

    import yaml

    bad = dict(CFG.raw)
    bad["product_hierarchy"] = {**bad["product_hierarchy"], "planning_level": "nonexistent"}
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(bad, fh)
    try:
        load_config(path)
    except ValueError:
        return
    finally:
        os.unlink(path)
    raise AssertionError("an undefined planning level was accepted")


if __name__ == "__main__":
    import traceback

    tests = [
        (n, f) for n, f in sorted(globals().items())
        if n.startswith("test_") and callable(f)
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {name}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
