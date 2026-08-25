# Supply Chain Planning Platform

A configurable demand-planning, replenishment, allocation and landed-cost engine
for a seasonal consumer-goods business — built in Python, with a Streamlit
workbench on the front and a Power BI star schema on the back.

The data here is **simulated** for a company shaped like Back to the Roots:
26 SKUs across grow kits, seeds, soils and garden kits, sold through 7 retail
accounts and 2 e-commerce channels, shipped from 3 DCs, sourced from a mix of
95–110 day imports and 10–21 day domestic suppliers. The *logic* is the logic I
run in production on a 400-class import book.

---

> **Also in this repository:** [`docs/POWER_BI_CASE_STUDY.md`](docs/POWER_BI_CASE_STUDY.md)
> documents a *production* Power BI replenishment report — the same problem this engine
> simulates, already running daily against five source systems.
>
> **Learning the code?** [`docs/ENGINE_INTERNALS.md`](docs/ENGINE_INTERNALS.md) is a full
> technical walkthrough — every module, the maths behind each formula, the data
> contracts, the config reference, and one SKU traced end to end with real numbers.

## What it does, in one pass

```bash
python data/generate_dataset.py   # build the simulated dataset
python run_simulation.py          # one full planning cycle
python run_scenarios.py           # seven what-if scenarios, compared
python -m streamlit run app/app.py   # the interactive workbench
python tests/test_engine.py       # 21 tests
```

`run_simulation.py` produces this, end to end:

| Stage | Output |
|---|---|
| **Demand plan** | 12-month SKU forecast, seasonal indices, accuracy scorecard |
| **Policy** | safety stock, reorder point (min), order-up-to (max) per SKU |
| **Replenishment** | time-phased projection + an order book: what to buy, from whom, when |
| **Allocation** | units split across 9 accounts and 3 DCs, with fill rate and shorts |
| **Landed cost** | the three gates a sourcing saving passes through, and its lag |
| **KPIs** | turns, GMROI, weeks of supply, ABC/XYZ segmentation |

Written to `outputs/<profile>/`: one formatted Excel workbook (17 sheets), a
Power BI-ready star schema with a `measures.dax` file, and a SQLite database.

---

## Results on the simulated book

| | |
|---|---|
| Forecast accuracy | **78.0%** (±3.0%), bias **−0.7%**, worst fold 74.4% |
| Planned purchases | $13.08M over 12 months |
| Average inventory | $3.59M |
| Inventory turns | 4.0× annualised |
| GMROI | 4.61 |
| Network fill rate | 99.7% |
| Projected stockouts | 6 item-months — **all** inside the frozen window |
| Sourcing scenario | 4.68% weighted surcharge across 6 of 9 suppliers |

Accuracy is measured by **rolling the forecast origin back six times** and
scoring each fold, not by holding out one window. That distinction is not
academic: on a single fold this model reads +12.9% bias; across six folds the
same settings read −0.7%. The sign flips. Tuning against one holdout would have
selected the wrong parameters with high confidence.

---

## The three findings worth talking about

### 1. Every projected stockout is unfixable by policy

All six shortages are long-lead imports (95–110 days) falling in the first four
months of the horizon. An order placed in month 1 lands in month 4 — so nothing
inside the plan can reach them. They were determined before the cycle started,
by the opening inventory and what was already on the water.

This is why raising the target service level from 95% to 99% leaves the shortage
list **completely unchanged** while adding $375K of inventory. The engine reports
the split explicitly (`Stockout Triage`), because the two kinds carry opposite
recommendations:

- **Locked** → expedite, air-freight, substitute, or shape demand
- **Addressable** → change a replenishment parameter

Reporting both under one "stockout" heading invites a planner to fix the wrong
problem.

### 2. A sourcing saving reaches the P&L far later than people assume

Negotiating an origin surcharge away does not improve next month's margin. The
saving passes three gates with different lags — purchase (cash), receipt
(warehouse), COGS (P&L) — and under weighted-average costing the third is
governed by a chained ratio:

```
ratio(m) = [ Inv(m−1)·ratio(m−1) + Receipts_new(m) ] / [ Inv(m−1) + Receipts_old(m) ]
```

Cheaper receipts *dilute* the pool they join; they do not replace it. After 12
months this book has captured $485K at the warehouse but only **78%** of it has
reached the P&L. A four-month inventory turn does **not** mean the benefit is
booked by month four — the ratio approaches its floor asymptotically.

It is also **not monotonic**: a month weighted toward domestic supply admits
receipts at ratio 1.0, above an already-discounted pool, and the reported saving
moves backwards. That is correct behaviour, and worth knowing before someone
reads one month as a regression.

### 3. A cash cap has to be modelled inside the loop, not after it

Capping the monthly buy at $1.0M cuts purchases by $3.15M and inventory by
$1.40M — and costs **7.2 points of fill**, taking stockouts from 6 to 43
item-months. Getting that second half right required restructuring the MRP to
run month-outer/item-inner, so competing items are ranked by urgency against a
shared budget. (See *How this was built* below.)

---

## Portable by configuration

Nothing in `src/` hard-codes a product level, a location name, or a planning
grain. The hierarchy is declared in `config.yaml` and the engine derives its
grouping keys from it:

```yaml
product_hierarchy:
  levels:
    - {key: division,    label: "Division"}
    - {key: category,    label: "Category"}
    - {key: subcategory, label: "Subcategory"}
    - {key: sku,         label: "SKU"}
  planning_level: sku          # the grain forecasts and orders are cut at
  reporting_rollups: [division, category]
```

`profiles/category_planning.yaml` proves it: a **three**-level hierarchy planning
at **category** instead of SKU, a coverage-months buffer instead of a statistical
one, a 9-month horizon, weekly review, and a binding cash cap. Same code:

```bash
python run_simulation.py profiles/category_planning.yaml
```

When the planning level sits above the data's grain, the master and balances are
rolled up with attribute-appropriate aggregation — lead time and cost are
**demand-weighted** (a plain mean would under-buffer every long-lead item in the
group), case pack takes the max, MOQ sums.

Re-pointing this at a different catalogue — different divisions, categories,
SKUs — is a config exercise, not a rewrite.

---

## Architecture

```
config.yaml                  the only business-specific file
  │
  ├─ src/config.py           loads + validates the hierarchy definition
  ├─ src/dataio.py           star schema in (CSV today, SQL/NetSuite tomorrow)
  ├─ src/forecast.py         seasonal index → deseasonalise → level → project
  ├─ src/replenishment.py    safety stock → min/max → time-phased MRP
  ├─ src/allocation.py       account mix → protect minimums → fair-share
  ├─ src/landed_cost.py      supplier rates → 3 savings gates → cost dilution
  ├─ src/kpis.py             turns, GMROI, weeks of supply, ABC/XYZ
  ├─ src/exports.py          Excel workbook + Power BI star schema + DAX
  │
  ├─ run_simulation.py       one full planning cycle
  ├─ run_scenarios.py        seven scenarios, one input changed each
  ├─ app/app.py              the Streamlit workbench
  └─ tests/test_engine.py    21 tests
```

### Streamlit, not Power BI — and why both are here

This gets asked every time it is demoed, so it is worth stating plainly.

| | Power BI / Tableau | This workbench |
|---|---|---|
| Reads, aggregates, slices | yes | yes |
| Computes a forecast | no | **yes** |
| Sizes safety stock from variability | no | **yes** |
| Solves a time-phased order plan | no | **yes** |
| Responds to changed assumptions | filters an existing table | **recomputes the plan** |
| Audience | the whole company | the planning team |

A BI tool filters numbers that already exist. Nothing in the order book exists
until this engine solves for it — there is no table anywhere containing *"order
14,400 units of BTR-1001 in November"*. That row is the output of a projection.

So both belong in the stack, and this project ships both: `run_simulation.py`
writes a clean star schema and `measures.dax` into `outputs/<profile>/powerbi/`,
because publishing to a wide audience is genuinely Power BI's job. **Python does
the solving; Power BI does the distribution.**

In the workbench, every sidebar control re-runs the full engine — forecast,
safety stock, MRP, allocation, landed cost — and redraws from the new result.
Ticking *Cap monthly purchases* recomputes 26 SKUs × 12 months and moves fill
rate from 99.7% to 92.5%. No BI filter can do that, because the answer is not in
any table until it is computed.

---

## Method notes

**Seasonality is estimated one level up.** A single SKU has 2 observations per
calendar month in a 24-month history — far too few for a stable index. Its
category has 20+. Accept a little bias to kill a lot of variance.

**Seasonal indices are shrunk, not gated.** `weight = n / (n + k)` where `n` is
the number of years that calendar month has been observed. Nothing is discarded;
weak estimates are pulled toward 1.0 in proportion to how thin the evidence is.

**Safety stock carries both variances:**
`z · √(LT·σ²demand + demand²·σ²LT)`. Dropping the lead-time term is the most
common way to under-stock a long-lead import — on a 95-day lane swinging ±15
days, that term dominates.

**Moving averages are re-centred.** A mean of the last *N* months describes the
midpoint of the window, not today. On a growing book that lag reads as a
systematic under-forecast (−9% here); carrying the trend from the window's
centre removes it.

**Minimum presentation stock is protected before fair-share.** Straight pro-rata
quietly drops retail doors below the level where the shelf is lost, and losing
the shelf costs far more than the units.

**Planner overrides never destroy the statistical value.** Both are stored, so
error can be attributed to the model or the human, and any override can be
rolled back.

---

## How this was built

Python, pandas, and Claude as a working partner — used not to generate code
unattended, but to move faster through the loop that matters: **write the logic,
run it against data, read the result, and challenge it.**

Three defects in this model were caught precisely because every step was scored
rather than eyeballed. All three are now regression tests:

1. **A seasonality rule that silently disabled itself.** A hard "needs 2 full
   years" gate saw 21 months ÷ 12 = 1.75 during the backtest and flattened every
   index to 1.0 — forecasting flat into a gardening business's summer trough, at
   **18% accuracy**. Evidence-weighted shrinkage took it to **78%**.

2. **Parameters that looked optimal on one holdout.** The bias flipped sign
   between a single fold (+13%) and six rolling origins (−9%). Only the rolling
   evaluation exposed the moving-average centring error underneath it.

3. **A cash cap applied after the MRP instead of inside it.** It reported $4.3M
   of deferred purchases with *zero* service impact, because the inventory
   projection still contained the receipts that had been cancelled. Fixing it
   meant restructuring the loop to month-outer/item-inner.

None of those were visible from reading the code. They surfaced from running it,
measuring it, and not accepting the first plausible answer.

---

## Notes on the data

Simulated with a fixed random seed (`data/generate_dataset.py`), so every run is
reproducible. Supplier names are anonymised (`SUP-01` … `SUP-09`); demand shapes,
lead times, case packs, MOQs and origin-expense rates are set to realistic ranges
for the category. A handful of SKUs are deliberately awkward — two launched
mid-history, two have erratic demand — so the planning logic has to cope with
them rather than with a clean book.

No proprietary data from any employer appears in this repository.
