// =============================================================================
//  Replenishment Control Tower — Power Query source
// =============================================================================
//  Paste each block below into Power BI Desktop as a separate blank query:
//      Home > Transform data > New Source > Blank Query > Advanced Editor
//  Paste, rename the query to the name in the header, Done.
//
//  Every table loads over HTTPS from a public repository. No local paths, no
//  gateway, nothing to install. The data is simulated from a fixed seed.
// =============================================================================


// ── 1 of 8 ── QUERY NAME: BaseUrl ────────────────────────────────────────────
// Create this one FIRST. Everything else references it, so the source can be
// repointed in one place.
"https://raw.githubusercontent.com/olivasjafet-rgb/supply-chain-planning-platform/main/powerbi/dataset/"


// ── 2 of 8 ── QUERY NAME: fnLoad ─────────────────────────────────────────────
// A tiny function so each table below is a one-liner instead of ten.
let
    fnLoad = (fileName as text) as table =>
        let
            Source  = Csv.Document(
                          Web.Contents(BaseUrl & fileName & ".csv"),
                          [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
                      ),
            Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true])
        in
            Headers
in
    fnLoad


// ── 3 of 8 ── QUERY NAME: Products ───────────────────────────────────────────
let
    Source = fnLoad("dim_product"),
    Typed  = Table.TransformColumnTypes(Source, {
        {"sku", type text}, {"sku_name", type text},
        {"division", type text}, {"category", type text}, {"subcategory", type text},
        {"supplier_id", type text},
        {"fob_unit_cost", type number}, {"landed_unit_cost", type number},
        {"retail_price", type number},
        {"case_pack", Int64.Type}, {"moq_units", Int64.Type},
        {"lead_time_days", Int64.Type}, {"lead_time_std_days", Int64.Type},
        {"origin_expense_rate", type number}, {"sourcing_type", type text}
    }),
    Renamed = Table.RenameColumns(Typed, {
        {"sku", "SKU"}, {"sku_name", "Product"},
        {"division", "Division"}, {"category", "Category"}, {"subcategory", "Subcategory"},
        {"supplier_id", "Supplier ID"},
        {"landed_unit_cost", "Landed Unit Cost"}, {"retail_price", "Retail Price"},
        {"case_pack", "Case Pack"}, {"moq_units", "MOQ"},
        {"lead_time_days", "Lead Time (days)"}, {"sourcing_type", "Sourcing"}
    })
in
    Renamed


// ── 4 of 8 ── QUERY NAME: Suppliers ──────────────────────────────────────────
let
    Source = fnLoad("dim_supplier"),
    Typed  = Table.TransformColumnTypes(Source, {
        {"supplier_id", type text}, {"origin_country", type text},
        {"lead_time_days", Int64.Type}, {"lead_time_std_days", Int64.Type},
        {"effective_rate", type number}, {"sourcing_type", type text},
        {"payment_terms", type text}
    }),
    Renamed = Table.RenameColumns(Typed, {
        {"supplier_id", "Supplier ID"}, {"origin_country", "Origin"},
        {"lead_time_days", "Lead Time (days)"},
        {"lead_time_std_days", "Lead Time Std (days)"},
        {"effective_rate", "Origin Surcharge %"},
        {"sourcing_type", "Sourcing"}, {"payment_terms", "Payment Terms"}
    }, MissingField.Ignore)
in
    Renamed


// ── 5 of 8 ── QUERY NAME: Accounts ───────────────────────────────────────────
let
    Source = fnLoad("dim_location"),
    Typed  = Table.TransformColumnTypes(Source, {
        {"account", type text}, {"channel", type text},
        {"node", type text}, {"node_city", type text}
    }),
    Renamed = Table.RenameColumns(Typed, {
        {"account", "Account"}, {"channel", "Channel"},
        {"node", "Fulfilment Node"}, {"node_city", "Node City"}
    }, MissingField.Ignore),
    Kept = Table.SelectColumns(Renamed,
        {"Account", "Channel", "Fulfilment Node", "Node City"}, MissingField.Ignore)
in
    Kept


// ── 6 of 8 ── QUERY NAME: Calendar ───────────────────────────────────────────
// One real date table. Mark it as the date table in Power BI:
//   Table tools > Mark as date table > Date
// This replaces the auto date/time feature, which should be turned OFF in
//   File > Options > Current file > Data load > uncheck "Auto date/time"
let
    Source   = fnLoad("dim_calendar"),
    AddDate  = Table.AddColumn(Source, "Date",
                   each Date.FromText([month] & "-01"), type date),
    Typed    = Table.TransformColumnTypes(AddDate, {
                   {"month", type text}, {"year", Int64.Type},
                   {"month_no", Int64.Type}, {"month_name", type text},
                   {"quarter", type text}, {"season", type text}
               }),
    Renamed  = Table.RenameColumns(Typed, {
                   {"month", "Month Key"}, {"year", "Year"},
                   {"month_no", "Month No"}, {"month_name", "Month"},
                   {"quarter", "Quarter"}, {"season", "Season"}
               }, MissingField.Ignore),
    Sorted   = Table.Sort(Renamed, {{"Date", Order.Ascending}})
in
    Sorted


// ── 7 of 8 ── QUERY NAME: Plan ───────────────────────────────────────────────
// THE CORE FACT. One row per SKU per month, carrying on-hand, the minimum
// (reorder point), the maximum (order-up-to), inbound receipts and the order.
// The Action column is the whole point of the report.
let
    Source = fnLoad("fact_plan"),
    Typed  = Table.TransformColumnTypes(Source, {
        {"sku", type text}, {"month", type text},
        {"opening_units", type number}, {"forecast_units", type number},
        {"scheduled_receipts", type number}, {"planned_receipts", type number},
        {"closing_units", type number}, {"stockout_units", type number},
        {"reorder_point", type number}, {"safety_stock", type number},
        {"order_up_to", type number}, {"weeks_of_supply", type number},
        {"unit_cost_usd", type number}, {"supplier_id", type text},
        {"lead_time_days", Int64.Type},
        {"order_placed_units", type number}, {"order_value_usd", type number},
        {"closing_value_usd", type number}
    }),
    AddDate = Table.AddColumn(Typed, "Date",
                  each Date.FromText([month] & "-01"), type date),

    // Inbound supply that is already committed and will land.
    AddInbound = Table.AddColumn(AddDate, "Inbound Units",
                  each [scheduled_receipts] + [planned_receipts], type number),

    // ---- the action bucket -------------------------------------------------
    // Mirrors the production report: below the minimum is not one situation,
    // it is three, and each one belongs to a different person.
    AddAction = Table.AddColumn(AddInbound, "Action", each
        if [closing_units] <= 0 and [forecast_units] > 0 then "Stockout"
        else if [closing_units] > [order_up_to]           then "Excess"
        else if [closing_units] >= [reorder_point]        then "In range"
        else if [closing_units] + [Inbound Units] >= [reorder_point] then "In transit"
        else "Buy", type text),

    // Sort order so the legend and axis read worst-to-best, not alphabetically.
    AddOrder = Table.AddColumn(AddAction, "Action Order", each
        if      [Action] = "Stockout"   then 1
        else if [Action] = "Buy"        then 2
        else if [Action] = "In transit" then 3
        else if [Action] = "In range"   then 4
        else 5, Int64.Type),

    // How far below the minimum, in units and dollars. Zero when at or above.
    AddGap = Table.AddColumn(AddOrder, "Gap to Min Units",
                  each List.Max({[reorder_point] - [closing_units], 0}), type number),
    AddGapV = Table.AddColumn(AddGap, "Gap to Min $",
                  each [Gap to Min Units] * [unit_cost_usd], type number),

    Renamed = Table.RenameColumns(AddGapV, {
        {"sku", "SKU"}, {"month", "Month Key"},
        {"opening_units", "Opening Units"}, {"forecast_units", "Demand Units"},
        {"scheduled_receipts", "In Transit Units"},
        {"planned_receipts", "Planned Receipt Units"},
        {"closing_units", "On Hand Units"}, {"stockout_units", "Stockout Units"},
        {"reorder_point", "Min"}, {"order_up_to", "Max"},
        {"safety_stock", "Safety Stock"},
        {"weeks_of_supply", "Weeks of Supply"},
        {"unit_cost_usd", "Unit Cost"}, {"supplier_id", "Supplier ID"},
        {"order_placed_units", "Order Units"}, {"order_value_usd", "Order $"},
        {"closing_value_usd", "On Hand $"}
    }, MissingField.Ignore)
in
    Renamed


// ── 8 of 8 ── QUERY NAME: Allocation ─────────────────────────────────────────
// SKU x month x account: what each account needed, what it got, what was short.
let
    Source = fnLoad("fact_allocation"),
    Typed  = Table.TransformColumnTypes(Source, {
        {"sku", type text}, {"month", type text},
        {"channel", type text}, {"account", type text}, {"node", type text},
        {"requirement_units", type number}, {"protected_minimum", type number},
        {"allocated_units", type number}, {"short_units", type number},
        {"fill_rate_pct", type number}, {"allocated_value_usd", type number}
    }),
    AddDate = Table.AddColumn(Typed, "Date",
                  each Date.FromText([month] & "-01"), type date),
    Renamed = Table.RenameColumns(AddDate, {
        {"sku", "SKU"}, {"month", "Month Key"},
        {"channel", "Channel"}, {"account", "Account"}, {"node", "Fulfilment Node"},
        {"requirement_units", "Requirement Units"},
        {"protected_minimum", "Protected Minimum"},
        {"allocated_units", "Allocated Units"},
        {"short_units", "Short Units"},
        {"allocated_value_usd", "Allocated $"}
    }, MissingField.Ignore),
    Dropped = Table.RemoveColumns(Renamed, {"fill_rate_pct"}, MissingField.Ignore)
in
    Dropped


// ── OPTIONAL ── QUERY NAME: OrderBook ────────────────────────────────────────
// What to buy, from whom, when to place it and when it lands.
let
    Source = fnLoad("fact_order_book"),
    Typed  = Table.TransformColumnTypes(Source, {
        {"sku", type text}, {"sku_name", type text},
        {"supplier_id", type text}, {"lead_time_days", Int64.Type},
        {"place_in_month", type text}, {"order_arrival_month", type text},
        {"order_units", type number}, {"case_pack", Int64.Type},
        {"moq_units", Int64.Type}, {"unit_cost_usd", type number},
        {"order_value_usd", type number}
    }),
    Renamed = Table.RenameColumns(Typed, {
        {"sku", "SKU"}, {"sku_name", "Product"},
        {"supplier_id", "Supplier ID"}, {"lead_time_days", "Lead Time (days)"},
        {"place_in_month", "Place In"}, {"order_arrival_month", "Lands In"},
        {"order_units", "Order Units"}, {"case_pack", "Case Pack"},
        {"moq_units", "MOQ"}, {"order_value_usd", "Order $"}
    }, MissingField.Ignore)
in
    Renamed
