# Engine Internals

A complete technical walkthrough of the planning platform: what every module does,
what it depends on, what the maths actually computes, and where it breaks.

Written to be read next to the code. Every number in the worked example (§7) is a
real value produced by `python run_simulation.py`, not an illustration.

---

## Contents

1. [How to read this](#1-how-to-read-this)
2. [Mental model](#2-mental-model)
3. [Dependency graph and execution order](#3-dependency-graph-and-execution-order)
4. [Data contracts](#4-data-contracts)
5. [`config.yaml` reference](#5-configyaml-reference)
6. [Module by module](#6-module-by-module)
7. [Worked example: one SKU, end to end](#7-worked-example-one-sku-end-to-end)
8. [Known characteristics and failure modes](#8-known-characteristics-and-failure-modes)
9. [Recipes: how to change things](#9-recipes-how-to-change-things)
10. [Testing](#10-testing)
11. [Glossary](#11-glossary)

---

## 1. How to read this

The codebase is small enough to hold in your head: 8 modules in `src/`, ~2,000 lines.
The hard part is not the Python — it is knowing *why* each calculation is shaped the
way it is. This guide leads with the why.

Three conventions used throughout the code:

- **Everything is a DataFrame.** No classes carrying state, no ORM, no hidden globals.
  Each function takes DataFrames in and returns DataFrames out. That means you can run
  any stage on its own in a REPL and look at what came out.
- **`cfg` is passed everywhere.** Any function that needs to know the planning grain,
  the service level, or the hierarchy takes the `Config` object rather than importing
  a settings module. This is what makes the second profile possible.
- **Column names are derived, never literal.** The code writes
  `cfg.planning_keys[-1]` instead of `"sku"`. When you see a literal `"sku"` in the
  engine, it is either a bug or a deliberate reference to the *source data's* grain
  (which really is SKU) — the comments say which.

```
python -i -c "
import sys; sys.path.insert(0,'.')
from src.config import load_config
from src import dataio, forecast, replenishment
cfg = load_config(); t = dataio.load_warehouse()
h = dataio.planning_history(t, cfg)
"
```

That drops you into a shell with the real data loaded. Everything below can be
poked at from there.

---

## 2. Mental model

### The question the engine answers

> Given what we sold, what we have, what is already on the water, and what each
> supplier's terms are — what should we order, when, and who gets it?

Five transformations, in strict order. Each depends on the one before it:

```
history ──▶ FORECAST ──▶ POLICY ──▶ MRP ──▶ ALLOCATION ──▶ outputs
             what we      how much   when we    who gets
             will sell    buffer     order      the units
```

A sixth branch, **landed cost**, hangs off the MRP output and answers a different
question — what a sourcing change is worth and when it shows up.

### The two grains

This trips people up, so it is worth being explicit.

| Grain | What lives there | Example |
|---|---|---|
| **Source grain** | The data as it arrives. Always SKU × account × month. | `fact_sales_history` |
| **Planning grain** | Where forecasts and orders happen. Set by `planning_level`. | SKU, or category |

In the default profile they are the same (both SKU), so the distinction is invisible.
In `profiles/category_planning.yaml` the planning grain sits two levels up, and
`dataio.product_master_at_grain()` / `balances_at_grain()` roll the source data up to
match. See [§6.2](#62-srcdataiopy).

### Units vs money

The engine plans in **units** and reports in **dollars**. Every conversion goes
through `landed_unit_cost` (FOB + origin surcharge), never through `fob_unit_cost`.
If you see inventory valued at FOB somewhere, that is a bug.

---

## 3. Dependency graph and execution order

```
                       ┌─────────────┐
                       │ config.yaml │
                       └──────┬──────┘
                              │
                       ┌──────▼──────┐
                       │  config.py  │   Hierarchy, Config, validation
                       └──────┬──────┘
                              │  (every module below imports Config)
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   ┌────▼─────┐        ┌──────▼──────┐       ┌──────▼──────┐
   │ dataio.py│───────▶│ forecast.py │──────▶│replenishment│
   └────┬─────┘        └─────────────┘       └──┬───────┬──┘
        │                                       │       │
        │              ┌─────────────┐          │       │
        └─────────────▶│allocation.py│◀─────────┘       │
                       └──────┬──────┘                  │
                              │            ┌────────────▼────┐
                              │            │ landed_cost.py  │
                              │            └────────┬────────┘
                       ┌──────▼──────┐              │
                       │   kpis.py   │◀─────────────┘
                       └──────┬──────┘
                              │
                       ┌──────▼──────┐
                       │ exports.py  │   Excel + Power BI + DAX
                       └─────────────┘
```

**Nobody imports anybody upward.** `forecast.py` does not know `replenishment.py`
exists. This is why you can replace the forecasting module wholesale without touching
anything downstream — the contract between them is just a DataFrame with a
`final_forecast` column.

### Actual call order in `run_simulation.py`

| # | Call | Produces |
|---|---|---|
| 1 | `load_config()` | `cfg` |
| 2 | `dataio.load_warehouse()` | 7 raw tables |
| 3 | `dataio.planning_history()` | history at planning grain, zero-filled |
| 4 | `dataio.account_history()` | history at item × account |
| 5 | `dataio.product_master_at_grain()` | master, possibly rolled up |
| 6 | `dataio.balances_at_grain()` | on-hand + open POs, possibly rolled up |
| 7 | `forecast.backtest()` / `rolling_backtest()` | accuracy scorecards |
| 8 | `forecast.build_forecast()` | `fc`, `indices` |
| 9 | `replenishment.demand_statistics()` | mean, std, CV per item |
| 10 | `replenishment.policy_parameters()` | safety stock, min, max |
| 11 | `replenishment.run_mrp()` | the time-phased plan |
| 12 | `replenishment.order_book()` | actionable orders |
| 13 | `replenishment.frozen_window()` | stockout triage |
| 14 | `allocation.account_mix()` → `allocate()` | who gets what |
| 15 | `landed_cost.supplier_rates()` → 3 gates | savings model |
| 16 | `kpis.*` | KPI sheets |
| 17 | `exports.*` | workbook, star schema, DAX |

Steps 7–8 are independent of 9–10; both feed 11.

### External dependencies

| Package | Used for | Where |
|---|---|---|
| `pandas` | everything | all modules |
| `numpy` | vectorised maths, `np.where`, `np.select` | most modules |
| `pyyaml` | reading the profile | `config.py` only |
| `openpyxl` | writing the formatted workbook | `exports.py` only |
| `streamlit` | the workbench UI | `app/app.py` only |
| `plotly` | charts in the workbench | `app/app.py` only |

The engine itself needs only pandas + numpy + pyyaml. Streamlit and plotly are UI
dependencies; openpyxl is an output dependency. You can run the whole planning cycle
without any of the last three by not calling `exports`.

---

## 4. Data contracts

The star schema lives in `data/warehouse/` as CSV. Seven tables.

### `dim_product` — the item master

| Column | Type | Meaning | Consumed by |
|---|---|---|---|
| `sku` | str | primary key at source grain | everything |
| `sku_name` | str | display name | `order_book`, `kpis` |
| `division` / `category` / `subcategory` | str | hierarchy levels | grouping, seasonality |
| `supplier_id` | str | FK to `dim_supplier` | `policy_parameters`, landed cost |
| `fob_unit_cost` | float | ex-works cost | landed-cost reconstruction |
| `origin_expense_rate` | float | supplier surcharge as a fraction | `landed_unit_cost` |
| `landed_unit_cost` | float | `fob × (1 + rate)` — **the costing basis** | all valuation |
| `retail_price` | float | list price | revenue and margin in `kpis` |
| `case_pack` | int | order rounding multiple | `_round_order` |
| `moq_units` | int | minimum order quantity | `_round_order` |
| `lead_time_days` | float | mean lead time | safety stock, MRP timing |
| `lead_time_std_days` | float | lead-time variability | safety stock (second term) |
| `sourcing_type` | str | `Import` / `Domestic` | scenario shocks, landed cost |
| `base_monthly_units` | int | generator seed value only | not used by the engine |

### `fact_sales_history`

`month` (Period[M]) · `sku` · `account` · `units` · `revenue_usd` · `cogs_usd`

One row per SKU × account × month. **Missing months are genuinely missing** — they
are not zero-filled here. `planning_history()` does the zero-filling, deliberately,
so that the raw table stays honest about what was observed.

### `fact_inventory`

`as_of_month` · `sku` · `node` · `on_hand_units` · `on_hand_value_usd`

A single snapshot at the planning cut-off, split by DC.

### `fact_open_orders`

`po_number` · `sku` · `supplier_id` · `node` · `eta_month` · `units` ·
`fob_value_usd` · `origin_expense_usd` · `status`

Orders already placed and not yet received. In the MRP these become **scheduled
receipts** — supply the engine must plan around but cannot change.

### `dim_supplier`

`supplier_id` · `origin_country` · `lead_time_days` · `lead_time_std_days` ·
`origin_expense_rate` · `payment_terms` · `sourcing_type`

### `dim_location`

`account` · `channel` · `node` · `demand_share` · `orders_in_case_packs` · `node_city`

`demand_share` here is the *generator's* network default. The engine does not use it —
`allocation.account_mix()` recomputes the real mix from sales history. Two different
things with a similar name; do not confuse them.

### `dim_calendar`

`month` · `year` · `month_no` · `month_name` · `quarter` · `is_history` · `season`

Used only by the Power BI export (as the date table). The engine derives dates from
the facts.

### Period columns

`dataio._read()` converts these to `pd.PeriodIndex(freq="M")` on load:
`month`, `eta_month`, `as_of_month`, `order_arrival_month`.

Periods are used rather than Timestamps because monthly arithmetic becomes trivial:
`as_of - 12` is twelve months back, `month + lt_months` is the arrival month. No
timezone, no day-of-month, no `relativedelta`. When writing out (CSV, Excel, SQL) they
are cast back to strings.

---

## 5. `config.yaml` reference

Every key, what it does, and what happens if you get it wrong.

### Top level

| Key | Effect if changed |
|---|---|
| `profile_name` | Names the run and the output folder (`outputs/<slug>/`). Two profiles with the same name overwrite each other. |
| `currency`, `units` | Labels only. No conversion logic. |
| `fiscal_year_start_month` | Reserved; not yet consumed by the engine. |

### `product_hierarchy`

```yaml
product_hierarchy:
  levels:
    - {key: division, label: "Division"}
    - {key: category, label: "Category"}
    - {key: subcategory, label: "Subcategory"}
    - {key: sku, label: "SKU"}
  planning_level: sku
  reporting_rollups: [division, category]
```

- **`levels`** — ordered broadest → narrowest. `Hierarchy.above(key)` returns the
  parents; `through(key)` returns parents plus the key itself. `cfg.planning_keys` is
  `through(planning_level)`, and that list is the group-by key for almost every
  operation in the engine.
- **`planning_level`** — must be one of the level keys, or `load_config` raises. This
  is *the* switch that changes the model's grain.
- **`reporting_rollups`** — must all be known level keys. Used by dashboards.

**Failure mode:** naming a level that does not exist as a column in `dim_product`
gives a `KeyError` deep inside a `groupby`, not a clean config error. The validator
checks internal consistency (levels vs `planning_level` vs `reporting_rollups`) but
cannot check the data until it is loaded.

### `location_hierarchy`

Same shape. `allocation_level` is where the fair-share decision is made (account);
`stocking_level` is where inventory physically sits (node). Both are validated
against the declared levels.

### `forecast`

| Key | Default | What it does |
|---|---|---|
| `history_months` | 24 | Informational; the engine uses whatever history exists. |
| `horizon_months` | 12 | Number of future months generated. Drives MRP length. |
| `moving_average_window` | 9 | Months averaged for the base level **and** the lag correction `(window−1)/2`. |
| `seasonality.enabled` | true | `false` forces every index to 1.0. |
| `seasonality.shrinkage_k` | 0.5 | Evidence weighting `n/(n+k)`. Higher = more shrinkage toward flat. |
| `seasonality.damping` | 1.0 | Extra flattening applied **only** when projecting. 1.0 = off. |
| `allow_manual_override` | true | If false, `apply_overrides()` is a no-op. |

**Why `damping` is 1.0.** Shrinkage already discounts weak estimates. Applying damping
on top discounts them twice, and cost ~2 accuracy points at every window length tested.
The lever is kept because a business with genuinely unstable seasonality might want it.

**Why `moving_average_window` is 9.** Chosen by rolling-origin backtest, not by fitting
one holdout. See [§8](#8-known-characteristics-and-failure-modes).

### `replenishment`

| Key | What it does |
|---|---|
| `safety_stock_method` | `service_level` (statistical) or `coverage_months` (simple rule). Anything else raises `ValueError`. |
| `target_service_level` | Snapped to the nearest entry in `Z_TABLE`. 0.95 → z = 1.645. |
| `coverage_months_default` | Used only under `coverage_months`. |
| `review_period_days` | Added to the reorder point to form the order-up-to level. |
| `order_policy` | Declared but not yet branched on — the implementation is min/max. |
| `respect_moq`, `respect_case_pack` | Toggle the two rounding rules in `_round_order`. |
| `max_monthly_purchase_usd` | Cash cap. `null` = uncapped. **Applied inside the MRP loop.** |

### `landed_cost`

| Key | What it does |
|---|---|
| `enabled` | Declared; the runner always computes it. |
| `basis` | `empirical_supplier_rate` (measured) vs `flat_rate`. |
| `flat_rate` | Fallback rate if you switch basis. |
| `costing_method` | Documents the assumption. The implementation is weighted-average. |
| `scenario.effective_from` | `"YYYY-MM"`. Receipts and POs at or after this month get the discount. |

### `kpi_targets`

Colouring thresholds for dashboards. Note that `weeks_of_supply_min/max` are **not**
what drives the item health flag — that uses a per-item comparison instead
(see [§6.7](#67-srckpispy)).

---

## 6. Module by module

### 6.1 `src/config.py`

Two frozen dataclasses and a loader.

**`Hierarchy`** — an ordered list of level keys plus display labels.

```python
h.above("sku")     # ['division', 'category', 'subcategory']
h.through("sku")   # ['division', 'category', 'subcategory', 'sku']
h.leaf             # 'sku'
```

`above()` raises `KeyError` on an unknown level rather than returning empty — a silent
empty list would produce a group-by on nothing and a wrong answer.

**`Config`** — the whole profile, frozen. Key properties:

```python
cfg.planning_keys    # the group-by key for the whole engine
cfg.horizon_months
cfg.service_level
```

**`with_overrides(**changes)`** — returns a modified copy, merging nested dicts:

```python
cfg2 = cfg.with_overrides(replenishment={"target_service_level": 0.99})
```

Only the named sub-keys change; the rest of the `replenishment` block is preserved.
This is what makes `run_scenarios.py` honest — a scenario is the same code path with
one value different, so it cannot silently diverge in some other way.

**`load_config(path)`** validates three cross-references before returning:
`planning_level` ∈ levels, every `reporting_rollup` ∈ levels, and both
`allocation_level` / `stocking_level` ∈ location levels.

---

### 6.2 `src/dataio.py`

**`load_warehouse(path)`** — reads all seven CSVs, converting period columns.

**`planning_history(tables, cfg)`** — the most important function here.

1. Attaches the planning level to sales if it is not already a column (needed when
   planning coarser than SKU).
2. Aggregates to planning grain × month.
3. Builds a **complete grid** (every item × every month in range) and left-joins,
   filling gaps with 0.

Step 3 matters more than it looks. Without it, a 4-month moving average over an item
that sold in only 2 of those months averages 2 values, not 4 — inflating the level by
2×. The zero-fill makes averages honest.

The cost of the zero-fill is that pre-launch months look like zero demand.
`forecast.seasonal_indices()` strips those back out (see below).

**`account_history(tables, cfg)`** — sales at item × account, rebuilt at planning grain
if needed. An account's share of a *category* is not the average of its shares of that
category's SKUs, so this has to aggregate before dividing.

**`product_master_at_grain(tables, cfg, history)`** — returns `dim_product` unchanged
when planning at SKU. Otherwise rolls up, with attribute-appropriate aggregation:

| Attribute | Aggregation | Why |
|---|---|---|
| `lead_time_days` | demand-weighted mean | The group waits for the mix it actually buys |
| `landed_unit_cost`, `retail_price` | demand-weighted mean | Same |
| `case_pack` | **max** | Respect the coarsest packaging constraint |
| `moq_units` | **sum** | A group order must clear every member's floor |
| `supplier_id`, `sourcing_type` | mode by volume | Dominant supplier represents the group |

A plain `.mean()` on lead time is the classic error: it under-buffers every long-lead
item in the group.

**`balances_at_grain(tables, cfg)`** — same idea for on-hand and open POs.

**`to_sql(tables, path)`** — materialises the star schema into SQLite.

---

### 6.3 `src/forecast.py`

Four public functions and the maths that carries the model.

#### `seasonal_indices(history, cfg, level, value_col)`

Estimates a multiplicative index per (level, calendar month).

```
raw_index(g, m) = mean(demand | group g, calendar month m)
                  ────────────────────────────────────────
                        mean(demand | group g)

normalised so that mean over the 12 months = 1.0
```

Then two adjustments:

**Pre-launch stripping.** For each item, find the first month with non-zero sales and
drop everything before it. Without this, an item launched in month 12 contributes
eleven artificial zeros to whichever calendar months it missed, dragging those indices
down.

**Evidence shrinkage.**

```
n          = distinct years that calendar month has been observed
confidence = n / (n + shrinkage_k)
index      = 1 + (raw_index − 1) × confidence
```

With `k = 0.5`: one year of data → 0.67 weight, two years → 0.80, three → 0.86.
Nothing is discarded; thin evidence is pulled toward neutral in proportion to how thin
it is.

Returns **two** index columns:

| Column | Used for | Why |
|---|---|---|
| `seasonal_index_raw` | stripping seasonality out of history | Best available estimate |
| `seasonal_index` | putting seasonality back when projecting | Damped version |

Using the damped index for both would leave part of the seasonal swing inside the
supposedly deseasonalised level, so a base period taken in spring would forecast
summer far too high. With `damping = 1.0` the two are currently identical, but the
split is structural — set damping below 1 and it matters immediately.

#### `build_forecast(history, cfg, seasonality_level, ...)`

```
deseasonalised(t)  = units(t) / seasonal_index_raw(category, month_of(t))

base_level         = mean of the last `window` deseasonalised months

yoy_growth         = Σ last 12m deseasonalised / Σ prior 12m − 1,  clipped to [−0.30, +0.35]

lag_months         = (window − 1) / 2

trend_factor(k)    = (1 + yoy_growth) ^ ((k + lag_months) / 12)

forecast(k)        = base_level × seasonal_index(month) × trend_factor(k)
```

where `k` is the horizon step (1 = first future month).

Two details worth understanding:

**The trend clip.** A SKU that tripled will not triple again. Without the clip, one
explosive item drives the whole purchase plan.

**The lag correction.** A mean of the last *N* months does not describe "now" — it
describes the midpoint of the window, `(N−1)/2` months in the past. On a growing book
that lag reads as systematic under-forecast; it measured **−9%** before this term was
added and **−0.7%** after.

The `group_keys` line handles the case where `seasonality_level` is itself one of the
planning keys (planning at SKU inside a category), which would otherwise produce a
duplicate column and a `ValueError` from `sort_values`.

#### `apply_overrides(forecast, overrides, cfg)`

Left-joins planner overrides on planning keys + month. Sets `final_forecast` to the
override where present, and stamps `override_source`. **`statistical_forecast` is
never modified** — so error can be attributed to the model or the human, and any
override is reversible.

#### `backtest()` and `rolling_backtest()`

`backtest(holdout_months=3)` — train on everything up to a cut-off, forecast forward,
score against actuals. Per item:

```
WMAPE    = Σ|actual − forecast| / Σ actual
bias     = Σ(forecast − actual) / Σ actual
accuracy = 1 − WMAPE
```

WMAPE rather than MAPE because MAPE divides by each actual individually and explodes
on small values — one SKU that sold 3 units in a trough month can dominate the metric.

`rolling_backtest(origins=6, horizon=3)` repeats that from six successive cut-offs.
**This is the one to trust.** On this dataset a single fold reports +12.9% bias and six
folds report −0.7%; the sign flips. Tuning against one window selects the wrong
parameters with high confidence.

---

### 6.4 `src/replenishment.py`

#### `demand_statistics(history, cfg, months=12)`

Mean, standard deviation and coefficient of variation of monthly demand over the
recent window. CV buckets into `Stable` (≤0.25), `Variable` (≤0.60), `Erratic`.

#### `policy_parameters(stats, products, cfg)`

The buffer maths.

```
daily_demand      = avg_monthly_demand / 30
daily_demand_std  = demand_std / √30

safety_stock      = z × √( LT × σ²_daily_demand  +  daily_demand² × σ²_LT )
                          └── demand variance ──┘  └── lead-time variance ──┘

reorder_point     = daily_demand × LT + safety_stock          ← the "min"
order_up_to       = reorder_point + daily_demand × review_days ← the "max"
```

**Why both variance terms.** Demand variance alone is the most common way to
under-stock a long-lead import. For BTR-1001 the two terms are 244.3M and 58.3M — so
lead-time variability contributes 19% of the total variance. On items with steadier
demand and a wobblier lane, it dominates outright. Dropping it is a silent, systematic
under-buffer.

`z` comes from an explicit `Z_TABLE` rather than `scipy.stats.norm.ppf` — one fewer
dependency, and the snapping to tabulated values is visible rather than hidden.

Under `coverage_months`, safety stock is simply `avg_monthly_demand × months` and both
variance terms are ignored.

#### `_round_order(qty, case_pack, moq, cfg)`

MOQ first, then round **up** to the case pack. Never rounds down to zero — an order of
1 unit against a case pack of 6 becomes 6, not 0.

#### `run_mrp(forecast, policy, on_hand, open_orders, cfg)`

The core loop. **Month outer, item inner** — and that ordering is load-bearing.

```python
for m in range(horizon):
    candidates = []
    for item in items:
        opening  = closing[item]
        receipts = scheduled[item][m] + planned[item][m]
        closing[item] = max(0, opening + receipts − demand[item][m])
        stockout      = max(0, −(opening + receipts − demand[item][m]))

        arrival = m + lead_time_months[item]
        if arrival >= horizon:            # would land outside the plan
            continue

        position = closing[item]
                 + receipts scheduled/planned in months m+1 … arrival
                 − demand in months m+1 … arrival

        if position < reorder_point[item]:
            candidates.append(order_up_to[item] − position, urgency=position/ROP)

    candidates.sort(by urgency)           # closest to its own ROP first
    spend = 0
    for c in candidates:
        if cap and spend + c.value > cap:
            mark deferred                 # and it genuinely does NOT arrive
        else:
            spend += c.value
            planned[c.item][c.arrival] += c.qty
```

Three things to notice:

1. **The order is sized against the position at the *arrival* month**, not today.
   Ordering against today's position on a 4-month lane would ignore everything already
   in transit and double-buy.

2. **`arrival >= horizon` skips the order entirely.** Orders that would land beyond
   the plan are not placed — they belong to the next cycle. This is why the last few
   months of the horizon show no new orders, and why the horizon should be at least
   twice the longest lead time for the tail to be meaningful.

3. **The cash cap is applied here, not afterwards.** A cap is a constraint shared
   across items competing for one budget, so it can only be resolved once all of that
   month's candidates are known. Applying it as a filter over a finished plan produces
   a plan that reports the saving without the consequence — purchases fall, but the
   projection still contains the receipts that were cancelled. That was a real defect;
   `test_cash_cap_binds_and_costs_service` guards it.

**Urgency ranking** is `position / reorder_point`, not absolute weeks of cover. Ranking
by absolute cover starves fast movers, whose cover is always low by construction.

#### `frozen_window(plan, policy, cfg)`

Classifies each projected shortage:

```
locked      if horizon_month_index < lead_time_months
addressable otherwise
```

A shortage in month 2 of a 4-month lane cannot be reached by any order placed inside
the plan. It was determined by the opening inventory and what was already on the water.
The two classes get opposite recommendations, so reporting them together invites
fixing the wrong problem.

#### `order_book(plan, products, cfg)`

Filters to rows with an order, joins names and constraints, renames for humans.
This is the actionable deliverable.

---

### 6.5 `src/allocation.py`

#### `account_mix(history, cfg, months=6)`

Each account's share of each item's recent demand, renormalised to 1.0 per item.

Six months rather than twenty-four **on purpose**: account mix drifts (a new listing at
Costco, a lost door at Kroger), and stale shares mis-route inventory into the wrong DC.

#### `allocate(plan, mix, locations, cfg, min_presentation_weeks=1.0)`

```
available   = opening + scheduled receipts + planned receipts
requirement = forecast × account share
floor       = requirement / 4.33 × min_presentation_weeks   (retail channel only)
```

Then, per item × month:

```
if available ≥ Σ requirement:
    allocate the requirement in full
elif available ≤ Σ floor:
    pro-rate the floors                      # cannot even protect minimums
else:
    allocate floor to everyone
    fair-share the remainder in proportion to (requirement − floor)
```

**Why floors first.** Straight pro-rata quietly drops retail doors below the level
where the shelf is lost, and losing the shelf costs far more than the units. E-commerce
accounts get no floor — there is no shelf to lose.

`np.floor` on the result means allocation never over-promises; the rounding residue
stays unallocated rather than being invented.

---

### 6.6 `src/landed_cost.py`

#### `supplier_rates(open_orders, suppliers)`

```
empirical_rate = Σ origin_expense_usd / Σ fob_value_usd     per supplier
```

Measured from transactions, not read off a rate card. Suppliers with no purchase
history fall back to the published rate and are flagged in `rate_source`.
`applies = effective_rate > 0` — domestic suppliers carry nothing and must never be
credited with a saving.

#### The three gates

| Gate | Function | Recognised when | Lag |
|---|---|---|---|
| 1. Purchase | `purchase_savings()` | the PO is placed | none |
| 2. Receipt | `receipt_savings()` | goods land | the lead time |
| 3. COGS | `cogs_dilution()` | units are sold | inventory turnover |

#### `cogs_dilution()` — the chained ratio

```
ratio(m) = [ Inv(m−1) × ratio(m−1) + Receipts_new(m) ]
           ─────────────────────────────────────────────
           [ Inv(m−1)              + Receipts_old(m) ]

ratio(0) = 1.000        (opening inventory is entirely at old cost)

COGS_new(m)      = COGS_current(m) × ratio(m)
Inventory_new(m) = Inventory_current(m) × ratio(m)
```

Cheaper receipts **dilute** the pool they join rather than replacing it. Consequences:

- A four-month inventory turn does **not** mean the benefit is booked by month four.
  The ratio approaches its floor asymptotically.
- **The ratio is not monotonic.** A receipt carrying no surcharge enters at ratio 1.0,
  which is *above* an already-discounted pool, so a month weighted toward domestic
  supply pushes the blended ratio back up. Reading one month as a regression is the
  mistake. `test_cost_ratio_stays_between_its_floor_and_one` asserts the bounds and
  deliberately does **not** assert monotonicity.

#### `steady_state(rates)`

```
w           = purchase-value-weighted surcharge rate
floor_ratio = 1 / (1 + w)
```

Answers "what is this worth once fully diluted?" without waiting for convergence.

---

### 6.7 `src/kpis.py`

**`abc_xyz()`** — ABC by cumulative revenue share (80% / 95% cut-offs), XYZ by CV
(0.25 / 0.60). The cross determines review cadence: AX/AY weekly, B monthly, C
quarterly.

**`inventory_kpis()`** — per-item turns, GMROI, weeks of supply, and a health flag.

The health flag is judged **relative to the item's own supply chain**:

```
expected_weeks = lead_time_days / 7 + safety_stock_weeks
cover_ratio    = avg_weeks_of_supply / expected_weeks

Stockout risk  if any stockout month
Overstocked    if cover_ratio > 1.50
Thin cover     if cover_ratio < 0.75
Healthy        otherwise
```

A 95-day import holding 18 weeks is running correctly; a 14-day domestic SKU holding
18 weeks is not. One company-wide weeks-of-supply band is wrong about half the
catalogue — this was changed after a global band flagged 16 of 26 items as overstocked.

**`network_summary()`** — the dashboard headline numbers. Turns are annualised as
`(COGS / avg inventory) × (12 / horizon_months)`.

---

### 6.8 `src/exports.py`

**`_write_sheet()`** — writes a frame with a styled frozen header, auto-filter, column
widths sampled from the data, and number formats inferred from column names
(`_fmt_for`). Period columns are cast to strings first — openpyxl cannot serialise a
Period.

**`_cover_sheet()`** — the Summary sheet: scope, method, headline results and the
assumptions, so the workbook is self-documenting.

**`write_powerbi_star_schema()`** — writes 14 CSVs plus `measures.dax` and a README
naming every relationship. (`run_scenarios.py` adds two more, so the folder ends up with
16 tables after a full pass.) `dim_calendar` gains a `month_start` timestamp because
Power BI needs a real date column to mark a date table.

---

## 7. Worked example: one SKU, end to end

**BTR-1001 — Organic Mushroom Grow Kit (Oyster)**, supplier SUP-01 (China),
lead time 95 ± 12 days, case pack 6, MOQ 3,000, landed cost $6.7712.

Reproduce any of this from the REPL snippet in §1.

### Step 1 — history

24 months, 423,567 units. The last twelve:

```
2025-09  16,863    2026-03  15,245
2025-10  25,406    2026-04  13,029
2025-11  32,855    2026-05  14,273
2025-12  39,579    2026-06  13,653
2026-01  13,878    2026-07  16,068
2026-02  12,753    2026-08  15,364
```

A clear holiday-gifting shape: December is ~3× February.

### Step 2 — seasonal index (Mushroom Kits, 2 years observed, confidence 0.80)

```
Jan 0.817   Apr 0.857   Jul 0.771   Oct 1.232
Feb 0.792   May 0.813   Aug 0.821   Nov 1.596
Mar 0.850   Jun 0.769   Sep 0.891   Dec 1.791
```

### Step 3 — deseasonalise, then level

Window = 9 months (Dec-2025 … Aug-2026):

| month | units | index | deseasonalised |
|---|---:|---:|---:|
| 2025-12 | 39,579 | 1.791 | 22,093 |
| 2026-01 | 13,878 | 0.817 | 16,984 |
| 2026-02 | 12,753 | 0.792 | 16,111 |
| 2026-03 | 15,245 | 0.850 | 17,928 |
| 2026-04 | 13,029 | 0.857 | 15,200 |
| 2026-05 | 14,273 | 0.813 | 17,558 |
| 2026-06 | 13,653 | 0.769 | 17,753 |
| 2026-07 | 16,068 | 0.771 | 20,846 |
| 2026-08 | 15,364 | 0.821 | 18,707 |

The point of the middle column: December's 39,579 and February's 12,753 — a 3× spread —
become 22,093 and 16,111 once seasonality is removed. That is the underlying level.

```
base_level = mean = 18,131.12
last 12m deseasonalised = 223,325 ; prior 12m = 191,854
yoy_growth = 223,325 / 191,854 − 1 = +16.40%   (inside the ±30/35% clip)
lag_months = (9 − 1) / 2 = 4.0
```

### Step 4 — project

For **Nov-2026** (horizon step k = 3):

```
trend_factor = 1.1640 ^ ((3 + 4) / 12) = 1.0926
forecast     = 18,131.12 × 1.5956 × 1.0926 = 31,610 units
```

Full horizon: 17,206 · 24,099 · **31,610** · 35,942 · 16,603 · 16,288 · 17,721 ·
18,091 · 17,376 · 16,647 · 16,898 · 18,234.

### Step 5 — policy

```
avg_monthly_demand = 19,080.50      demand_std = 8,784.01      CV = 0.460 → Variable

daily_demand     = 19,080.50 / 30    = 636.02
daily_demand_std = 8,784.01 / √30    = 1,603.73

demand variance term    = 95 × 1,603.73²  = 244,336,236
lead-time variance term = 636.02² × 12²   =  58,250,477
                                    √sum  =      17,395

safety_stock  = 1.645 × 17,395            = 28,615 units
demand_during_lt = 636.02 × 95            = 60,422
reorder_point = 60,422 + 28,615           = 89,037   ← min
order_up_to   = 89,037 + 636.02 × 30      = 108,118  ← max
lead_time_months = ceil(95/30)            = 4
```

Note the lead-time term is 19% of total variance. Drop it and safety stock falls to
25,713 — a 10.1% under-buffer on an item that is already going to stock out.

### Step 6 — opening position

```
on hand  DC-West 10,541 · DC-Central 10,261 · DC-East 7,246   = 28,048 units
open POs PO-4101 Oct 28,870 · PO-4102 Nov 14,628 · PO-4103 Dec 32,047
```

### Step 7 — the MRP loop, month by month

| month | opening | forecast | sched. receipts | planned receipts | closing | stockout | order placed | arrives |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 2026-09 | 28,048 | 17,206 | 0 | 0 | 10,842 | 0 | **129,990** | 2027-01 |
| 2026-10 | 10,842 | 24,099 | 28,870 | 0 | 15,613 | 0 | 0 | |
| 2026-11 | 15,613 | 31,610 | 14,628 | 0 | 0 | **1,369** | 32,640 | 2027-03 |
| 2026-12 | 0 | 35,942 | 32,047 | 0 | 0 | **3,895** | 0 | |
| 2027-01 | 0 | 16,603 | 0 | 129,990 | 113,387 | 0 | 31,572 | 2027-05 |
| 2027-02 | 113,387 | 16,288 | 0 | 0 | 97,099 | 0 | 0 | |
| 2027-03 | 97,099 | 17,721 | 0 | 32,640 | 112,018 | 0 | 33,540 | 2027-07 |
| 2027-04 | 112,018 | 18,091 | 0 | 0 | 93,927 | 0 | 0 | |
| 2027-05 | 93,927 | 17,376 | 0 | 31,572 | 108,123 | 0 | 0 | |
| 2027-06 | 108,123 | 16,647 | 0 | 0 | 91,476 | 0 | 0 | |
| 2027-07 | 91,476 | 16,898 | 0 | 33,540 | 108,118 | 0 | 0 | |
| 2027-08 | 108,118 | 18,234 | 0 | 0 | 89,884 | 0 | 0 | |

**Where the 129,990 comes from.** In September (m = 0), arrival is m + 4 = January:

```
closing now                     =  10,842
+ receipts Oct…Jan (28,870 + 14,628 + 32,047 + 0)  =  75,545
− demand   Oct…Jan (24,099 + 31,610 + 35,942 + 16,603) = 108,254
                                   ─────────
position at arrival             = −21,867      ← below ROP of 89,037, so order

order = 108,118 − (−21,867) = 129,985
      → case pack 6 → ceil(129,985/6) × 6 = 129,990 units = $880,188
```

**Why it still stocks out in Nov and Dec.** Nothing ordered in September can arrive
before January. The 1,369 and 3,895 unit shortfalls were already determined by the
opening inventory and the three POs on the water. That is the frozen window, and no
safety-stock setting reaches it.

### Step 8 — allocation, Sep-2026

| account | share | requirement | protected min | allocated | fill |
|---|---:|---:|---:|---:|---:|
| Amazon | 19.0% | 3,269 | 0 | 3,269 | 100% |
| Home Depot | 17.2% | 2,959 | 683 | 2,959 | 100% |
| Walmart | 13.9% | 2,391 | 552 | 2,391 | 100% |
| DTC Shopify | 11.7% | 2,021 | 0 | 2,021 | 100% |
| Lowe's | 11.4% | 1,954 | 451 | 1,954 | 100% |
| Target | 11.0% | 1,890 | 436 | 1,890 | 100% |
| Costco | 10.4% | 1,782 | 412 | 1,782 | 100% |
| Whole Foods | 3.7% | 631 | 146 | 631 | 100% |
| Kroger | 1.8% | 308 | 71 | 308 | 100% |

Supply covers requirement this month, so floors never bind. Note Amazon and DTC carry
no protected minimum — no shelf to lose. Note also the mix: mushroom kits skew
e-commerce (30.7% Amazon + DTC) far above the network default.

### Step 9 — triage

```
BTR-1001  2026-11  horizon month 2 < lead-time months 4  → Locked
BTR-1001  2026-12  horizon month 3 < lead-time months 4  → Locked
Recommended action: expedite / air freight / shape demand
```

---

## 8. Known characteristics and failure modes

### Characteristics that are correct but look wrong

**The January inventory spike.** BTR-1001 goes from 0 to 113,387 units in one month.
That is a single order-up-to replenishment sized against a deficit position, landing at
once. It is correct min/max behaviour on a long lane, but it produces a lumpy
inventory profile. A real deployment would split it into multiple containers across
months — that is a scheduling refinement the current model does not do.

**Service level changes that change nothing.** With every shortage inside the frozen
window, moving 95% → 99% adds inventory and fixes no shortages. Not a bug — see §6.4.

**A non-monotonic cost ratio.** See §6.6.

**No orders in the last months of the horizon.** Orders whose arrival falls beyond the
horizon are skipped. Keep the horizon ≥ 2 × the longest lead time or the tail is
meaningless.

### Defects that were real, and their guards

| Defect | Symptom | Fix | Test |
|---|---|---|---|
| Hard "2 years of history" seasonality gate | 21 months ÷ 12 = 1.75 → every index forced to 1.0 → **18% accuracy** | Evidence-weighted shrinkage | `test_seasonality_survives_a_shorter_history` |
| Parameters tuned on one holdout | Bias read +13% on one fold, −9% across six | Rolling-origin selection + MA centring | `test_rolling_backtest_returns_several_folds` |
| Cash cap applied after the MRP | $4.3M "saved", zero service impact | Cap moved inside the loop | `test_cash_cap_binds_and_costs_service` |

### Things that will bite you

- **Period vs string.** Reading a CSV without `dataio._read()` leaves `month` as a
  string, and every `month > cutoff` comparison silently does lexicographic
  comparison. Always load through `dataio`.
- **pandas 3 strictness.** Assigning formatted strings into a float column now raises
  rather than upcasting. Build display frames fresh (see `run_scenarios.fmt`).
- **Excel sheet names.** `/ \ * ? : [ ]` are rejected and names cap at 31 chars —
  `run_scenarios._sheet_name()` sanitises.
- **`groupby` on a duplicated column.** If `seasonality_level` is also a planning key
  and both get carried, `sort_values` raises "label is not unique".

---

## 9. Recipes: how to change things

### Point it at a different catalogue

1. Replace the CSVs in `data/warehouse/` keeping the column contracts in §4.
2. Edit `product_hierarchy.levels` in `config.yaml` to match your columns.
3. Set `planning_level` to the grain you plan at.
4. Run `python tests/test_engine.py` — the contract tests will tell you what is missing.

### Plan at a different grain

Change `planning_level`. That is the whole change. `dataio` handles the rollup. See
`profiles/category_planning.yaml`.

### Swap the data source (CSV → SQL / NetSuite)

Only `dataio.load_warehouse()` reads data. Replace its body to return the same seven
DataFrames with the same columns and dtypes, and nothing downstream changes.

```python
def load_warehouse(conn=None):
    return {
        "dim_product": pd.read_sql("SELECT ... FROM item_master", conn),
        ...
    }
```

Convert period columns yourself, or reuse `PERIOD_COLUMNS`.

### Replace the forecasting method

`build_forecast()` must return a frame with the planning keys, `month`,
`statistical_forecast`, `final_forecast` and `override_source`. Anything satisfying
that contract works — Holt-Winters, Prophet, an ML model, or a CSV of planner numbers.
Score it with `rolling_backtest()` before switching.

### Add a constraint (e.g. container capacity)

Add it to the candidate loop in `run_mrp()`, next to the cash cap — that is where
shared constraints belong. Record what was deferred in a column so it is visible.

### Change what "healthy" means

`kpis.inventory_kpis()`, the `cover_vs_expected` thresholds.

### Add a scenario

Append a dict to `SCENARIOS` in `run_scenarios.py` with the config overrides and/or a
product-mutation function.

---

## 10. Testing

```bash
python tests/test_engine.py        # plain runner, no pytest needed
python -m pytest tests/ -q         # if you have pytest
```

21 tests in five groups:

| Group | Checks |
|---|---|
| Inventory identity | `closing = opening + receipts − demand + stockout`, and opening chains to prior closing |
| Constraints | case pack, MOQ, cash cap binds *and* costs service |
| Policy direction | higher service → more buffer; zeroing σ_LT → less buffer |
| Forecast | indices normalise to 1.0, seasonality survives short history, overrides preserve the baseline |
| Allocation | never exceeds requirement, minimums protected, shares sum to 1 |
| Landed cost | ratio bounded by its floor and 1.0, P&L lags the warehouse, domestic suppliers get no credit |
| Portability | second profile runs at a different grain; rollups are demand-weighted; bad config rejected |

Three are named regression tests for real defects (§8). When you change the engine,
those are the ones to watch.

---

## 11. Glossary

| Term | Meaning |
|---|---|
| **Planning grain** | The level at which forecasts and orders are produced (`planning_level`) |
| **Frozen window** | The first *N* months of the horizon that no new order can reach, where *N* = lead time in months |
| **Reorder point (min)** | Inventory level that triggers an order: demand over the lead time + safety stock |
| **Order-up-to (max)** | Target level an order brings the position back to |
| **Inventory position** | On hand + on order − demand between now and the arrival date |
| **Scheduled receipt** | A PO already placed; supply the plan must work around |
| **Planned receipt** | An order the engine decided to place inside this run |
| **WMAPE** | Σ\|error\| / Σ actual — volume-weighted, unlike MAPE |
| **Bias** | Σ(forecast − actual) / Σ actual; positive = over-forecast |
| **Rolling origin** | Backtesting from several successive cut-off dates rather than one |
| **Shrinkage** | Pulling an estimate toward a neutral value in proportion to how little data supports it |
| **Landed cost** | FOB + origin-side expenses; the basis for all valuation here |
| **Cost ratio** | New weighted-average cost ÷ old, after a sourcing change |
| **Fair-share** | Allocating scarce supply proportionally to requirement |
| **Protected minimum** | Presentation stock a retail door must keep to hold the shelf |
| **GMROI** | Gross margin ÷ average inventory |
| **Weeks of supply** | Inventory ÷ weekly demand |
| **ABC / XYZ** | Segmentation by value contribution / demand variability |
