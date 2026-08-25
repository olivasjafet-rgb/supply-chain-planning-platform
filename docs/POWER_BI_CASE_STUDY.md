# Case Study — Replenishment Control Tower (Power BI)

A production Power BI report I built and maintain for a national retail and
distribution business. It runs every morning and answers one question per SKU
per location:

> We are below the minimum. **Where does the cover come from — transfer it from
> the DC, wait for what is already on the water, or raise a purchase order?**

This is the counterpart to the planning engine in this repository. That one is a
simulation of what I can build; **this one is what I actually run.** Figures,
names, hosts and file paths are removed — what is documented here is the model
architecture and the technique.

---

## The problem it solves

The business sells the same catalogue through several direct-sales locations,
supplied from one central distribution centre and from imports. Each SKU carries
a **minimum and maximum** per location. Every morning someone had to answer, for
thousands of SKU-location combinations:

- Is this item below its minimum?
- If so, is there stock at the DC that could cover it?
- Or is a container already in transit that will cover it?
- Or does nobody have it, and it has to be bought?
- And separately — where is there **excess** that nobody has noticed?

Before the report, that was a manual reconciliation across an ERP extract, a
transit report, a stock query and a min/max sheet. It took most of a morning, it
was done weekly rather than daily, and the four answers above were never
separated cleanly — everything below minimum looked like a purchase.

---

## The decision logic

The report classifies every SKU-location into one of seven states, and the state
determines who acts:

| State | Meaning | Who acts |
|---|---|---|
| **In range** | Between min and max | nobody |
| **Transfer from DC** | Below minimum, and the DC holds enough to cover it | warehouse — move it |
| **In transit** | Below minimum, but inbound stock already covers it | nobody — it is handled |
| **Buy** | Below minimum, no DC stock, nothing inbound | purchasing — raise a PO |
| **On order** | Below minimum, a PO exists but has not shipped | purchasing — expedite |
| **Missing / no stock** | Zero on hand against live demand | escalate |
| **Excess** | Above maximum | merchandising — stop buying, redistribute |

Separating *transfer* from *buy* is the point of the whole thing. Both look
identical in a raw "below minimum" report, and treating them the same means
buying inventory the company already owns — the most expensive mistake in
replenishment, because it pays twice for the same cover and leaves the original
units to age at the DC.

---

## Model architecture

A star schema centred on the item-requirement table, fed from five source
systems:

```
                    ┌───────────────────────────┐
                    │  Item requirement detail  │   ← the dimension every
                    │  (SKU, min, max, cost,    │     fact joins to
                    │   status, action bucket)  │
                    └─────────────┬─────────────┘
                                  │  1
        ┌──────────┬──────────┬───┴────┬──────────┬──────────┐
        │ many     │ many     │ many   │ many     │ many     │
   ┌────▼────┐ ┌───▼────┐ ┌───▼────┐ ┌─▼──────┐ ┌─▼────────┐
   │ SALES   │ │ STOCK  │ │TRANSIT │ │ITEM    │ │ DC       │
   │ (ERP)   │ │ (ERP)  │ │ (ERP)  │ │STATUS  │ │TRANSFERS │
   └─────────┘ └────────┘ └────────┘ └────────┘ └──────────┘

   plus a DISCONNECTED selector table driving scope (no relationship —
   applied through TREATAS, see below)
```

| Metric | Count |
|---|---|
| Functional tables | 12 |
| Relationships | 20 (all single-direction, many-to-one) |
| DAX measures | 36 |
| Source systems | 5 — SQL Server warehouse, two dataflows, Excel, CSV |
| M partitions | 8 |
| Report pages | 3 |
| Visuals | 86 |
| Bidirectional or many-to-many relationships | **0** |

Zero bidirectional relationships is deliberate. Bidirectional filtering is the
usual cause of ambiguous filter paths and wrong totals in a model with several
fact tables pointing at one dimension; where a cross-filter was genuinely needed
it was done explicitly in DAX rather than structurally in the model.

---

## The four techniques that carry the report

### 1. Per-item capped availability — the measure that stops the report lying

The naive coverage measure is `total available ÷ total required`. It is wrong,
and wrong in the dangerous direction: **surplus on one SKU silently offsets a
shortage on another.** A location holding triple cover on one item and nothing on
a second reads as "100% covered" and nobody investigates.

The fix is to cap the credit at the item level before summing:

```dax
Considered Inventory $ =
SUMX (
    VALUES ( 'Item Requirement'[SKU] ),      -- iterate SKU by SKU
    VAR RequiredSKU  = [Required Inventory $]
    VAR AvailableSKU =
        DIVIDE (
            CALCULATE (
                SUM ( Stock[Available_USD] ),
                TREATAS ( VALUES ( 'Location Selector'[Location] ), Stock[LocationId] )
            ),
            1000
        )
    RETURN
        MIN ( AvailableSKU, RequiredSKU )    -- excess never covers a shortage
)
```

`MIN(available, required)` per SKU, summed by `SUMX` over `VALUES`. Excess above
the requirement earns no credit. The reported coverage then means what a planner
thinks it means.

The same shape is reused three times with different supply included — on-hand
only, on-hand plus inbound transit, on-hand plus DC stock — which is what lets
the report say *"you are at 71% today, 88% once the transit lands, and 96% if we
also pull from the DC."* Three numbers, one pattern, and each one answers a
different person's question.

### 2. A disconnected selector table applied with `TREATAS`

The report has to switch scope — all locations, one channel, one project group —
without duplicating measures per scope. The selector is a **disconnected table**
with no relationship to anything. It is applied as a virtual relationship:

```dax
CALCULATE (
    SUM ( Stock[Available_USD] ),
    TREATAS ( VALUES ( 'Location Selector'[Location] ), Stock[LocationId] )
)
```

`TREATAS` pushes the selected values onto the fact table's column as if a
relationship existed. A physical relationship could not do this job, because the
same selection has to filter several fact tables that do not share a common
location dimension at the same grain.

### 3. Scope-aware requirement via `SWITCH` on `SELECTEDVALUE`

Different scopes read their requirement from different min/max columns — the
project channel is planned against a different maximum than the retail floor:

```dax
Requirement QTY =
VAR Scope = SELECTEDVALUE ( 'Location Selector'[Label], "Global" )
RETURN
    SWITCH (
        Scope,
        "Projects", SUM ( 'Item Requirement'[Max_Projects] ),
        "Retail",   SUM ( 'Item Requirement'[Max_Retail] ),
        SUM ( 'Item Requirement'[Max_Global] )
    )
```

The same pattern drives a **dynamic subtitle measure**, so the header always
states which locations are in scope. Small detail, disproportionate effect: the
single most common way a filtered dashboard misleads is a reader who does not
notice what is filtered.

### 4. Coverage anchored to the last closed month

```dax
Avg Cost of Sales 4M $ =
VAR WindowEnd = EOMONTH ( TODAY (), -1 )       -- last day of the previous month
VAR TotalCost =
    CALCULATE (
        SUM ( Sales[Cost_of_Sales] ),
        DATESINPERIOD ( Sales[Date], WindowEnd, -4, MONTH )
    )
RETURN
    DIVIDE ( TotalCost, 4 )

Coverage = DIVIDE ( [Available $], [Avg Cost of Sales 4M $] )
```

Anchoring to `EOMONTH(TODAY(), -1)` rather than to `TODAY()` matters: a
part-month in the denominator makes coverage jump on the 1st of every month and
drift down through it. Analysts stop trusting a number that moves for calendar
reasons rather than business ones.

---

## Model health audit

I ran a metadata extraction over the `.pbix` and audited the model against
itself. Reporting the findings rather than hiding them, because a model nobody
has audited is a model nobody should trust:

| Finding | Detail | Impact |
|---|---|---|
| **Auto date/time is on** | 11 hidden date tables generated automatically, one per date column, each with 6 calculated columns | Model bloat; blocks consistent time intelligence. The fix is one shared date table marked as such, and the feature switched off. |
| **87 calculated columns** | Mostly inside those auto date tables; 3 are genuine business columns | Calculated columns are stored, not compressed like imported ones. Business logic belongs upstream in Power Query or the source query. |
| **Two local-file sources** | An Excel workbook and a CSV read from a local path | Fragile. Breaks for any other user and cannot refresh in the Service without a gateway. These should move to the warehouse or a dataflow. |
| **One orphan measure** | A measure with an empty expression | Dead weight; removed. |
| **Unused-measure risk list** | 96 measure-usage rows analysed for direct use *and* indirect dependency | Deletion candidates identified, but not deleted on evidence of absence alone. |

That last row is the one worth defending. A measure missing from the visual-usage
export is **not** proof it is unused — it may be referenced by another measure, a
tooltip, conditional formatting, a drill-through page, a field parameter or a
bookmark. The analysis therefore scores both direct use and dependency, and
flags risk, rather than producing a delete list. Deleting on absence alone is how
a dashboard breaks two weeks later in front of someone who matters.

### What I would change next

1. Replace the 11 auto date tables with one marked date table.
2. Move the two local-file sources into the warehouse.
3. Push the three business calculated columns upstream into the source query.
4. Split the 53-visual page — it is the slowest page and most of the visuals
   answer the same question at different granularities.

---

## How this maps to the role

| Requirement in the job description | Evidence here |
|---|---|
| Establish and manage **minimum and maximum** inventory values for replenishment | The entire report is a min/max evaluation engine |
| Manage **product pipelining** so inventory is available where needed | Transfer / in-transit / buy separation is exactly pipelining |
| Manage warehouse inventory and **coordinate with distribution centres** | DC-transfer state and DC stock coverage measure |
| Review **store demand** and determine allocation and replenishment levels | Per-location requirement with scope-aware min/max |
| Build **technical reports and dashboards** for supply chain decisions | 3 pages, 86 visuals, in daily production use |
| **Power BI** or similar BI tooling | Star schema, 36 measures, TREATAS virtual relationships, disconnected parameter tables, model-health audit |
| **Connect different systems** to streamline fulfilment | Five source systems unified into one semantic model |
| Advanced **Excel**, **SQL** | Native SQL query folding into the warehouse; Excel master data integrated through Power Query |

---

## A note on what is and is not shown

This document describes architecture and technique. It contains no sales figures,
no cost data, no SKU or supplier identities, no server names, no database names,
no workspace identifiers and no file paths. DAX is reproduced with neutral table
and column names; the logic is unchanged because the logic is the part worth
showing, and it is my own work rather than my employer's data.
