"""Planning workbench -- Streamlit front end.

    python -m streamlit run app/app.py

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
This is not a BI dashboard. A BI tool reads a table that something else already
computed and draws it. Every number on these pages is computed *here*, on the
fly, by the Python engine in ``src/`` -- and the Scenario Lab page re-runs that
whole engine against whatever the sliders say before it draws anything.

That is the line between the two tools:

    Power BI answers   "what happened, and how does it slice?"
    This answers       "what should we order, and what breaks if we change X?"

Both belong in the stack. This project ships the BI half too -- ``run_simulation.py``
writes a star schema and a DAX measure file to ``outputs/powerbi/`` -- because
reporting to a wider audience is genuinely Power BI's job. What Power BI cannot
do is solve a time-phased replenishment plan, so that part lives in Python.
"""

from __future__ import annotations

import sys
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import allocation as alloc  # noqa: E402
from src import dataio, forecast, kpis, landed_cost, replenishment  # noqa: E402
from src.config import load_config  # noqa: E402

st.set_page_config(page_title="Supply Chain Planning Workbench", layout="wide", page_icon=":seedling:")

# -- palette -----------------------------------------------------------------
GREEN = "#1F4E3D"
LEAF = "#4C9A6E"
SAND = "#C9A227"
CLAY = "#B4553F"
GREY = "#8A9299"

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; max-width: 1500px;}
      div[data-testid="stMetricValue"] {font-size: 1.55rem;}
      .caption {color:#6b7280; font-size:0.85rem; line-height:1.45;}
      .pill {display:inline-block; padding:2px 10px; border-radius:999px;
             background:#1F4E3D14; color:#1F4E3D; font-size:0.75rem; font-weight:600;}
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Data + engine, cached so the sliders feel instant
# -----------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_base():
    cfg = load_config()
    tables = dataio.load_warehouse()
    history = dataio.planning_history(tables, cfg)
    acct_history = dataio.account_history(tables, cfg)
    fc, indices = forecast.build_forecast(history, cfg, seasonality_level="category")
    folds = forecast.rolling_backtest(history, cfg, origins=6, horizon=3)
    scored = forecast.backtest(history, cfg, holdout_months=3)
    return cfg, tables, history, acct_history, fc, indices, folds, scored


@st.cache_data(show_spinner=False)
def run_engine(
    _cfg, _tables, _history, _acct_history, _fc,
    service_level: float, cash_cap, lead_time_delta: int, demand_pct: float,
    safety_method: str, coverage_months: float,
):
    """Full engine pass. The leading-underscore args are cache-excluded frames;
    the plain args are the actual cache key -- i.e. the scenario definition."""
    cfg = _cfg.with_overrides(
        replenishment={
            "target_service_level": service_level,
            "max_monthly_purchase_usd": cash_cap,
            "safety_stock_method": safety_method,
            "coverage_months_default": coverage_months,
        }
    )
    products = _tables["dim_product"].copy()
    if lead_time_delta:
        is_import = products["sourcing_type"] == "Import"
        products.loc[is_import, "lead_time_days"] += lead_time_delta

    fc = _fc.copy()
    if demand_pct != 0:
        fc["final_forecast"] = (fc["final_forecast"] * (1 + demand_pct / 100)).round(0)

    stats = replenishment.demand_statistics(_history, cfg, months=12)
    if demand_pct != 0:
        stats["avg_monthly_demand"] *= 1 + demand_pct / 100
        stats["demand_std"] *= 1 + demand_pct / 100

    policy = replenishment.policy_parameters(stats, products, cfg)
    plan = replenishment.run_mrp(
        fc, policy, _tables["fact_inventory"], _tables["fact_open_orders"], cfg
    )
    orders = replenishment.order_book(plan, products, cfg)
    frozen = replenishment.frozen_window(plan, policy, cfg)

    mix = alloc.account_mix(_acct_history, cfg, months=6)
    allocation = alloc.allocate(plan, mix, _tables["dim_location"], cfg)
    service = alloc.service_summary(allocation, cfg)

    item_kpis = kpis.inventory_kpis(plan, products, policy, cfg)
    segments = kpis.abc_xyz(_history, stats, cfg)
    monthly = kpis.monthly_summary(plan, cfg)
    summary = kpis.network_summary(plan, allocation, item_kpis, cfg)

    rates = landed_cost.supplier_rates(_tables["fact_open_orders"], _tables["dim_supplier"])
    receipts = landed_cost.receipt_savings(plan, products, rates, cfg)
    dilution = landed_cost.cogs_dilution(plan, receipts, _history, products, cfg)
    steady = landed_cost.steady_state(rates)

    return dict(
        cfg=cfg, policy=policy, plan=plan, orders=orders, frozen=frozen,
        allocation=allocation, service=service, item_kpis=item_kpis,
        segments=segments, monthly=monthly, summary=summary, rates=rates,
        dilution=dilution, steady=steady, stats=stats, fc=fc,
    )


cfg0, tables, history, acct_history, fc_base, indices, folds, scored = load_base()

# -----------------------------------------------------------------------------
# Sidebar -- the scenario controls
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown(f"### {cfg0.profile_name}")
    st.markdown(
        '<span class="pill">Python engine, not a BI extract</span>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Every control below re-runs the full planning engine — forecast, "
        "safety stock, time-phased MRP, allocation and landed cost — and "
        "redraws from the new result."
    )
    st.divider()

    st.markdown("**Service policy**")
    safety_method = st.radio(
        "Safety stock basis", ["service_level", "coverage_months"],
        format_func=lambda v: "Statistical (service level)" if v == "service_level"
        else "Months of coverage",
        help="Statistical sizes the buffer from demand and lead-time variability. "
             "Coverage is the simpler months-of-supply rule.",
    )
    service_level = st.select_slider(
        "Target service level", options=[0.90, 0.925, 0.95, 0.975, 0.99],
        value=0.95, format_func=lambda v: f"{v:.1%}",
        disabled=safety_method != "service_level",
    )
    coverage_months = st.slider(
        "Coverage (months)", 0.5, 5.0, 2.0, 0.5,
        disabled=safety_method != "coverage_months",
    )

    st.markdown("**Shocks**")
    lead_time_delta = st.slider("Import lead time (days)", -21, 45, 0, 7,
                                help="Applied to imported items only.")
    demand_pct = st.slider("Demand vs plan (%)", -25, 40, 0, 5)

    st.markdown("**Constraints**")
    use_cap = st.checkbox("Cap monthly purchases", value=False)
    cash_cap = st.slider("Monthly cap ($M)", 0.5, 3.0, 1.0, 0.1) * 1_000_000 if use_cap else None

    st.divider()
    st.caption(
        f"Planning at **{cfg0.planning_level}** across "
        f"{' > '.join(cfg0.products.keys)}. Change `config.yaml` to plan a "
        "different business on the same code."
    )

R = run_engine(
    cfg0, tables, history, acct_history, fc_base,
    service_level, cash_cap, lead_time_delta, demand_pct,
    safety_method, coverage_months,
)
BASE = run_engine(
    cfg0, tables, history, acct_history, fc_base,
    0.95, None, 0, 0.0, "service_level", 2.0,
)
is_scenario = R["summary"] != BASE["summary"]

st.title("Supply Chain Planning Workbench")
st.markdown(
    '<p class="caption">Demand planning, replenishment, allocation and landed-cost '
    "simulation for a seasonal consumer-goods business. Simulated data; production logic.</p>",
    unsafe_allow_html=True,
)

tabs = st.tabs([
    "Executive", "Demand Plan", "Replenishment", "Service & Allocation",
    "Landed Cost", "How it's built",
])


def delta_for(key, fmt="${:,.0f}", invert=False):
    if not is_scenario:
        return None
    d = R["summary"][key] - BASE["summary"][key]
    if abs(d) < 1e-9:
        return None
    return fmt.format(d)


# =============================================================================
# 1. EXECUTIVE
# =============================================================================
with tabs[0]:
    s, b = R["summary"], BASE["summary"]
    c = st.columns(6)
    c[0].metric("Planned purchases", f"${s['planned_purchases_usd']/1e6:,.2f}M",
                delta_for("planned_purchases_usd", "${:+,.0f}"), delta_color="inverse")
    c[1].metric("Average inventory", f"${s['avg_inventory_usd']/1e6:,.2f}M",
                delta_for("avg_inventory_usd", "${:+,.0f}"), delta_color="inverse")
    c[2].metric("Inventory turns", f"{s['inventory_turns_annualised']:.1f}x",
                delta_for("inventory_turns_annualised", "{:+.1f}"))
    c[3].metric("GMROI", f"{s['gmroi']:.2f}", delta_for("gmroi", "{:+.2f}"))
    c[4].metric("Fill rate", f"{s['fill_rate']:.1%}", delta_for("fill_rate", "{:+.2%}"))
    c[5].metric("Items at risk", f"{s['items_with_stockout']:.0f}",
                delta_for("items_with_stockout", "{:+.0f}"), delta_color="inverse")

    if is_scenario:
        st.info(
            f"Comparing a scenario against the plan of record. "
            f"Inventory {s['avg_inventory_usd']-b['avg_inventory_usd']:+,.0f}, "
            f"purchases {s['planned_purchases_usd']-b['planned_purchases_usd']:+,.0f}, "
            f"fill {s['fill_rate']-b['fill_rate']:+.2%}."
        )

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Demand, receipts and inventory")
        m = R["monthly"]
        x = m["month"].astype(str)
        fig = go.Figure()
        fig.add_bar(x=x, y=m["forecast_units"], name="Forecast demand", marker_color=LEAF)
        fig.add_bar(x=x, y=m["receipts_units"] + m["planned_receipts_units"],
                    name="Receipts", marker_color=SAND, opacity=0.85)
        fig.add_scatter(x=x, y=m["closing_units"], name="Closing inventory",
                        mode="lines+markers", line=dict(color=GREEN, width=3), yaxis="y2")
        fig.update_layout(
            barmode="group", height=420, margin=dict(t=10, b=10, l=0, r=0),
            yaxis=dict(title="Units / month"),
            yaxis2=dict(title="Closing units", overlaying="y", side="right", showgrid=False),
            legend=dict(orientation="h", y=1.12, x=0),
        )
        st.plotly_chart(fig, width="stretch")

    with right:
        st.subheader("Where the inventory sits")
        by_cat = (
            R["item_kpis"].groupby("category", as_index=False)
            .agg(inventory=("avg_inventory_usd", "sum"), margin=("gross_margin_usd", "sum"))
            .sort_values("inventory", ascending=True)
        )
        fig = go.Figure(go.Bar(
            x=by_cat["inventory"], y=by_cat["category"], orientation="h",
            marker_color=GREEN,
            text=[f"${v/1000:,.0f}K" for v in by_cat["inventory"]], textposition="outside",
        ))
        fig.update_layout(height=420, margin=dict(t=10, b=10, l=0, r=40),
                          xaxis_title="Average inventory ($)")
        st.plotly_chart(fig, width="stretch")

    st.subheader("Item health")
    health = (
        R["item_kpis"].groupby("health", as_index=False)
        .agg(items=("sku", "count"), inventory=("avg_inventory_usd", "sum"))
    )
    hc = st.columns(len(health))
    colours = {"Healthy": LEAF, "Overstocked": SAND, "Thin cover": GREY, "Stockout risk": CLAY}
    for col, row in zip(hc, health.itertuples()):
        col.metric(row.health, f"{row.items} items", f"${row.inventory/1000:,.0f}K inventory")

    st.dataframe(
        R["item_kpis"][[
            "sku", "sku_name", "category", "health", "avg_weeks_of_supply",
            "expected_weeks_of_supply", "inventory_turns", "gmroi",
            "avg_inventory_usd", "purchase_value_usd",
        ]].round(2),
        width="stretch", hide_index=True, height=340,
    )
    st.caption(
        "Health is judged against each item's own lead time plus its own safety "
        "buffer — not one company-wide weeks-of-supply band. A 95-day import "
        "holding 18 weeks is correct; a 14-day domestic SKU holding 18 weeks is not."
    )


# =============================================================================
# 2. DEMAND PLAN
# =============================================================================
with tabs[1]:
    c = st.columns(4)
    c[0].metric("Rolling accuracy", f"{folds['accuracy'].mean():.1%}",
                f"+/- {folds['accuracy'].std():.1%}")
    c[1].metric("Rolling bias", f"{folds['bias_pct'].mean():+.1%}")
    c[2].metric("Worst fold", f"{folds['accuracy'].min():.1%}")
    c[3].metric("Items > 30% error", f"{int((scored['wmape'] > 0.30).sum())} of {len(scored)}")

    st.caption(
        "Accuracy is measured by rolling the forecast origin back six times and "
        "scoring each fold, not by holding out one window. On a single fold this "
        "model reads +13% bias; across six it reads −0.7%. Tuning on one fold "
        "would have selected the wrong parameters."
    )

    level = st.selectbox("View", ["Total"] + sorted(history["category"].unique()))
    h = history if level == "Total" else history[history["category"] == level]
    f = R["fc"] if level == "Total" else R["fc"][R["fc"]["category"] == level]
    hs = h.groupby("month", as_index=False)["units"].sum()
    fs = f.groupby("month", as_index=False)["final_forecast"].sum()

    fig = go.Figure()
    fig.add_scatter(x=hs["month"].astype(str), y=hs["units"], name="Actual",
                    mode="lines+markers", line=dict(color=GREEN, width=2.5))
    fig.add_scatter(x=fs["month"].astype(str), y=fs["final_forecast"], name="Forecast",
                    mode="lines+markers", line=dict(color=LEAF, width=2.5, dash="dash"))
    fig.update_layout(height=380, margin=dict(t=10, b=10, l=0, r=0),
                      legend=dict(orientation="h", y=1.12, x=0), yaxis_title="Units")
    st.plotly_chart(fig, width="stretch")

    left, right = st.columns(2)
    with left:
        st.subheader("Seasonal shape by category")
        fig = go.Figure()
        for cat in sorted(indices["category"].unique()):
            g = indices[indices["category"] == cat].sort_values("month_no")
            fig.add_scatter(x=g["month_no"], y=g["seasonal_index"], name=cat, mode="lines")
        fig.add_hline(y=1.0, line_dash="dot", line_color=GREY)
        fig.update_layout(height=340, margin=dict(t=10, b=10, l=0, r=0),
                          xaxis=dict(title="Calendar month", tickmode="linear"),
                          yaxis_title="Index")
        st.plotly_chart(fig, width="stretch")
        st.caption(
            "Estimated at category level, then shrunk toward 1.0 in proportion to "
            "how many years that calendar month has actually been observed. An "
            "earlier hard cut-off ('under 2 years → no seasonality') silently "
            "flattened the entire backtest and scored 18% accuracy."
        )
    with right:
        st.subheader("Accuracy by fold")
        fig = go.Figure(go.Bar(
            x=folds["origin"], y=folds["accuracy"], marker_color=GREEN,
            text=[f"{v:.0%}" for v in folds["accuracy"]], textposition="outside",
        ))
        fig.update_layout(height=340, margin=dict(t=10, b=10, l=0, r=0),
                          yaxis=dict(title="Accuracy", tickformat=".0%", range=[0, 1]),
                          xaxis_title="Forecast origin")
        st.plotly_chart(fig, width="stretch")

    st.subheader("Worst-forecast items — where a planner should spend time")
    st.dataframe(
        scored.nlargest(10, "abs_error")[
            ["sku", "actual_units", "forecast_units", "wmape", "bias_pct"]
        ].round(3),
        width="stretch", hide_index=True,
    )


# =============================================================================
# 3. REPLENISHMENT
# =============================================================================
with tabs[2]:
    o = R["orders"]
    c = st.columns(4)
    c[0].metric("Orders to place", f"{len(o)}")
    c[1].metric("Order value", f"${o['order_value_usd'].sum()/1e6:,.2f}M")
    c[2].metric("Safety stock", f"${R['policy']['safety_stock_value_usd'].sum()/1e6:,.2f}M")
    deferred = R["plan"]["deferred_value_usd"].sum()
    c[3].metric("Deferred by cash cap", f"${deferred/1e6:,.2f}M" if deferred else "—")

    st.subheader("Purchase plan by month")
    m = R["monthly"]
    fig = go.Figure()
    fig.add_bar(x=m["month"].astype(str), y=m["purchases_usd"], name="Purchases",
                marker_color=GREEN)
    if cash_cap:
        fig.add_hline(y=cash_cap, line_dash="dash", line_color=CLAY,
                      annotation_text="cash cap", annotation_position="top left")
    fig.update_layout(height=320, margin=dict(t=10, b=10, l=0, r=0),
                      yaxis_title="Purchases ($)")
    st.plotly_chart(fig, width="stretch")

    st.subheader("Stockout triage")
    fw = R["frozen"]
    if len(fw):
        locked = fw[fw["classification"].str.startswith("Locked")]
        addressable = fw[~fw["classification"].str.startswith("Locked")]
        cc = st.columns(2)
        cc[0].metric("Locked in the frozen window", f"{len(locked)} item-months",
                     f"{locked['stockout_units'].sum():,.0f} units")
        cc[1].metric("Addressable by policy", f"{len(addressable)} item-months",
                     f"{addressable['stockout_units'].sum():,.0f} units")
        st.dataframe(fw, width="stretch", hide_index=True)
        st.caption(
            "A shortage inside the frozen window cannot be fixed by any "
            "safety-stock setting — an order placed today with a 95-day lead time "
            "lands in month four. Those need expediting or demand shaping; only "
            "the addressable ones justify a parameter change. This is why moving "
            "the service level from 95% to 99% can leave the shortage list "
            "completely unchanged."
        )
    else:
        st.success("No projected stockouts in this scenario.")

    st.subheader("Order book")
    st.dataframe(
        o[["sku", "sku_name", "supplier_id", "lead_time_days", "place_in_month",
           "order_arrival_month", "order_units", "case_pack", "moq_units",
           "order_value_usd", "reorder_point", "safety_stock", "order_up_to"]],
        width="stretch", hide_index=True, height=380,
    )

    st.subheader("Policy parameters")
    st.dataframe(
        R["policy"][["sku", "avg_monthly_demand", "demand_std", "cv", "demand_class",
                     "lead_time_days", "lead_time_std_days", "safety_stock",
                     "safety_stock_weeks", "reorder_point", "order_up_to",
                     "safety_stock_value_usd"]].round(2),
        width="stretch", hide_index=True, height=320,
    )
    st.caption(
        "Safety stock = z · √(LT·σ²demand + demand²·σ²LT). The second term is the "
        "one most models drop; on a 95-day lane that swings ±15 days it is the "
        "term that dominates."
    )


# =============================================================================
# 4. SERVICE & ALLOCATION
# =============================================================================
with tabs[3]:
    svc = R["service"]
    c = st.columns(3)
    c[0].metric("Network fill rate", f"{R['summary']['fill_rate']:.1%}")
    c[1].metric("Short units", f"{R['allocation']['short_units'].sum():,.0f}")
    c[2].metric("Accounts below 99%", f"{int((svc['fill_rate'] < 0.99).sum())}")

    fig = go.Figure(go.Bar(
        x=svc["account"], y=svc["fill_rate"],
        marker_color=[CLAY if v < 0.97 else (SAND if v < 0.99 else GREEN) for v in svc["fill_rate"]],
        text=[f"{v:.1%}" for v in svc["fill_rate"]], textposition="outside",
    ))
    fig.update_layout(height=340, margin=dict(t=10, b=10, l=0, r=0),
                      yaxis=dict(title="Fill rate", tickformat=".0%", range=[0.8, 1.02]))
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "When supply is short the engine protects each retail account's minimum "
        "presentation stock first, then fair-shares the remainder. Straight "
        "pro-rata would quietly drop doors below the level where the shelf is lost — "
        "and losing the shelf costs far more than the units."
    )

    st.dataframe(svc.round(4), width="stretch", hide_index=True)

    st.subheader("Allocation detail")
    acc = st.selectbox("Account", sorted(R["allocation"]["account"].unique()))
    view = R["allocation"][R["allocation"]["account"] == acc]
    st.dataframe(
        view[["sku", "month", "requirement_units", "protected_minimum",
              "allocated_units", "short_units", "fill_rate_pct"]].round(3),
        width="stretch", hide_index=True, height=340,
    )


# =============================================================================
# 5. LANDED COST
# =============================================================================
with tabs[4]:
    d, ss = R["dilution"], R["steady"]
    c = st.columns(4)
    c[0].metric("Weighted surcharge", f"{ss['weighted_surcharge_rate']:.2%}")
    c[1].metric("Receipt saving", f"${d['receipt_saving_usd'].sum()/1000:,.0f}K")
    c[2].metric("COGS saving", f"${d['cogs_saving_usd'].sum()/1000:,.0f}K")
    c[3].metric("Reached the P&L", f"{d['pnl_realisation_pct'].iloc[-1]:.0%}")

    st.subheader("The three gates a sourcing saving passes through")
    x = d["month"].astype(str)
    fig = go.Figure()
    fig.add_bar(x=x, y=d["receipt_saving_usd"], name="Received (warehouse)", marker_color=LEAF)
    fig.add_bar(x=x, y=d["cogs_saving_usd"], name="Recognised in COGS (P&L)", marker_color=GREEN)
    fig.add_scatter(x=x, y=d["pnl_realisation_pct"], name="% reached the P&L",
                    mode="lines+markers", line=dict(color=CLAY, width=3), yaxis="y2")
    fig.update_layout(
        barmode="group", height=400, margin=dict(t=10, b=10, l=0, r=0),
        yaxis=dict(title="$ saved"),
        yaxis2=dict(title="Realisation", overlaying="y", side="right",
                    tickformat=".0%", range=[0, 1], showgrid=False),
        legend=dict(orientation="h", y=1.12, x=0),
    )
    st.plotly_chart(fig, width="stretch")

    st.markdown(
        f"""
**Why the P&L lags the warehouse.** Under weighted-average costing a cheaper
receipt *dilutes* the cost of the pool it joins, it does not replace it:

`ratio(m) = [Inv(m−1)·ratio(m−1) + Receipts_new(m)] / [Inv(m−1) + Receipts_old(m)]`

After {len(d)} months the ratio is **{d['cost_ratio'].iloc[-1]:.4f}**, against a
steady-state floor of **{ss['steady_state_cost_ratio']:.4f}**. So
**{d['pnl_realisation_pct'].iloc[-1]:.0%}** of the saving has reached the P&L and the
rest is still sitting in inventory, waiting to be sold. This is the single most
misread number in a sourcing business case — a four-month inventory turn does
*not* mean the benefit is fully booked by month four, because the ratio
approaches its floor asymptotically and never quite lands on it.
        """
    )

    st.subheader("Supplier rates — measured, not assumed")
    st.dataframe(
        R["rates"][["supplier_id", "origin_country", "sourcing_type", "lead_time_days",
                    "purchase_value_usd", "origin_expense_usd", "empirical_rate",
                    "effective_rate", "rate_source", "applies"]].round(4),
        width="stretch", hide_index=True,
    )
    st.caption(
        "Each supplier's surcharge is its actual origin expenses divided by its "
        "actual purchase value. A single blended assumption misprices every "
        "supplier in the book — here the real spread runs from 0% to 6.7%."
    )


# =============================================================================
# 6. HOW IT'S BUILT
# =============================================================================
with tabs[5]:
    st.subheader("This is a Python application, not a BI report")
    st.markdown(
        """
The distinction matters, and it is the first question this tool usually gets.

| | Power BI / Tableau | This workbench |
|---|---|---|
| Reads data | yes | yes |
| Aggregates and slices | yes | yes |
| **Computes a forecast** | no (visual only) | yes |
| **Sizes safety stock from variability** | no | yes |
| **Solves a time-phased order plan** | no | yes |
| **Re-runs on changed assumptions** | no — filters an existing table | yes — recomputes the plan |
| Audience | everyone | the planning team |

A BI tool filters numbers that already exist. Nothing in the order book exists
until this engine solves for it: there is no table anywhere that contains
"order 14,400 units of BTR-1001 in November". That row is the output of a
projection that has to be computed.

**Both belong in the stack, and this project ships both.** `run_simulation.py`
writes a clean star schema and a `measures.dax` file into `outputs/powerbi/`,
because publishing to a wide audience is genuinely Power BI's job. Python does
the solving; Power BI does the distribution.
        """
    )

    st.subheader("Architecture")
    st.code(
        """config.yaml            <- the only business-specific file
   |
   +-- src/config.py         loads + validates the hierarchy definition
   +-- src/dataio.py         star schema in  (CSV today, SQL/NetSuite tomorrow)
   +-- src/forecast.py       seasonal index -> deseasonalise -> level -> project
   +-- src/replenishment.py  safety stock -> min/max -> time-phased MRP
   +-- src/allocation.py     account mix -> protect minimums -> fair-share
   +-- src/landed_cost.py    supplier rates -> 3 savings gates -> cost dilution
   +-- src/kpis.py           turns, GMROI, weeks of supply, ABC/XYZ
   +-- src/exports.py        Excel workbook + Power BI star schema + DAX
   |
   +-- run_simulation.py     one full planning cycle
   +-- run_scenarios.py      seven scenarios, one input changed each
   +-- app/app.py            this workbench""",
        language="text",
    )

    st.subheader("Portable by configuration")
    st.markdown(
        f"""
Nothing in `src/` hard-codes a product level or a location name. The hierarchy
is declared in `config.yaml` and the engine derives its grouping keys from it:

- **This profile** plans at `{cfg0.planning_level}` across
  `{' > '.join(cfg0.products.keys)}`, stocking at `{cfg0.stocking_level}`.
- Swap the file for a different business — different levels, different depth,
  a different planning grain — and the same code runs. `profiles/` contains a
  second, deliberately dissimilar profile as proof.

That is why re-pointing this at a new catalogue is a configuration exercise
rather than a rewrite.
        """
    )

    st.subheader("How it was built")
    st.markdown(
        """
Built in Python with Claude as a working partner — not to generate code
unattended, but to move faster through the loop that actually matters:
**write the logic, run it against data, read the result, and challenge it.**

Three defects in this model were caught precisely because every step was scored
rather than eyeballed:

1. A seasonality rule that silently disabled itself in the backtest and scored
   **18% accuracy**. Replacing the hard cut-off with evidence-weighted shrinkage
   took it to **78%**.
2. Parameters that looked optimal against a single holdout window and were
   wrong across six rolling origins — the bias flipped sign, from +13% to −9%.
3. A cash cap applied *after* the MRP instead of inside it, which reported
   $4.3M of savings with no service impact because the projection still
   contained the receipts that had been cancelled.

None of those would have been visible from reading the code. They surfaced from
running it, measuring it, and not accepting the first plausible answer — which
is the part of the workflow the AI accelerates most.
        """
    )
