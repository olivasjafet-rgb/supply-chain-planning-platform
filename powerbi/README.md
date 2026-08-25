# Power BI dataset

The star schema produced by `python run_simulation.py`, committed so a Power BI
file can load it straight from this repository over HTTPS — no local paths, no
gateway, nothing to install.

Base URL for the raw files:

```
https://raw.githubusercontent.com/olivasjafet-rgb/supply-chain-planning-platform/main/powerbi/dataset/
```

| Table | Grain | Use |
|---|---|---|
| `dim_product` | SKU | item master: division, category, cost, lead time, case pack, MOQ |
| `dim_supplier` | supplier | origin, lead time and variability, measured surcharge rate |
| `dim_location` | account | channel, fulfilment node |
| `dim_calendar` | month | date table — mark this as the date table |
| `fact_plan` | SKU x month | **the core fact**: on hand, min, max, receipts, orders, cover |
| `fact_allocation` | SKU x month x account | requirement, allocated, short, fill rate |
| `fact_sales_history` | SKU x month x account | 24 months of actuals |
| `fact_order_book` | order | what to buy, from whom, when placed, when landed |
| `fact_landed_cost` | month | the three savings gates and the cost-dilution ratio |
| `fact_forecast` | SKU x month | statistical forecast, seasonal index, override column |
| `fact_stockout_triage` | shortage | locked vs addressable classification |

`measures.dax` carries the measures written against this schema.

All data is simulated from a fixed seed in `data/generate_dataset.py`.
