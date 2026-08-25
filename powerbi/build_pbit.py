"""Generate a Power BI template (.pbit) for the Replenishment Control Tower.

    python powerbi/build_pbit.py

Authors a PbixProj folder (TMSL model + report layout) and compiles it with
pbi-tools into a single .pbit. The model loads its data over HTTPS from the
public repository, so the template is self-contained: open it, let it refresh,
and the report is populated. No local paths, no gateway, no employer data.

Requires pbi-tools (https://pbi.tools). The path is auto-detected.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi" / "Replenishment Control Tower.pbit"

BASE_URL = ("https://raw.githubusercontent.com/olivasjafet-rgb/"
            "supply-chain-planning-platform/main/powerbi/dataset/")

PBI_TOOLS_CANDIDATES = [
    Path.home() / "pbi-tools-1.2.0" / "pbi-tools.exe",
    Path.home() / "pbi-tools-core-1.2.0" / "pbi-tools.core.exe",
    Path("pbi-tools.exe"),
]


# =============================================================================
# M expressions — one per table, loading straight from the repository
# =============================================================================
def m_load(file: str, extra: str = "") -> str:
    return (
        'let\n'
        f'    Source = Csv.Document(Web.Contents("{BASE_URL}{file}.csv"),'
        '[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n'
        '    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true])'
        f'{extra}\n'
        'in\n'
        f'    {"Result" if extra else "Headers"}'
    )


M_PRODUCTS = m_load("dim_product", """,
    Typed = Table.TransformColumnTypes(Headers, {
        {"sku", type text}, {"sku_name", type text}, {"division", type text},
        {"category", type text}, {"subcategory", type text}, {"supplier_id", type text},
        {"landed_unit_cost", type number}, {"retail_price", type number},
        {"case_pack", Int64.Type}, {"moq_units", Int64.Type},
        {"lead_time_days", Int64.Type}, {"sourcing_type", type text}}),
    Result = Table.RenameColumns(Typed, {
        {"sku","SKU"}, {"sku_name","Product"}, {"division","Division"},
        {"category","Category"}, {"subcategory","Subcategory"},
        {"supplier_id","Supplier ID"}, {"landed_unit_cost","Unit Cost"},
        {"retail_price","Retail Price"}, {"case_pack","Case Pack"},
        {"moq_units","MOQ"}, {"lead_time_days","Lead Time Days"},
        {"sourcing_type","Sourcing"}}, MissingField.Ignore)""")

M_SUPPLIERS = m_load("dim_supplier", """,
    Typed = Table.TransformColumnTypes(Headers, {
        {"supplier_id", type text}, {"origin_country", type text},
        {"lead_time_days", Int64.Type}, {"effective_rate", type number},
        {"sourcing_type", type text}, {"payment_terms", type text}}),
    Result = Table.RenameColumns(Typed, {
        {"supplier_id","Supplier ID"}, {"origin_country","Origin"},
        {"lead_time_days","Supplier Lead Time"},
        {"effective_rate","Origin Surcharge"}, {"sourcing_type","Supplier Sourcing"},
        {"payment_terms","Payment Terms"}}, MissingField.Ignore)""")

M_ACCOUNTS = m_load("dim_location", """,
    Typed = Table.TransformColumnTypes(Headers, {
        {"account", type text}, {"channel", type text},
        {"node", type text}, {"node_city", type text}}),
    Renamed = Table.RenameColumns(Typed, {
        {"account","Account"}, {"channel","Channel"},
        {"node","Fulfilment Node"}, {"node_city","Node City"}}, MissingField.Ignore),
    Result = Table.SelectColumns(Renamed,
        {"Account","Channel","Fulfilment Node","Node City"}, MissingField.Ignore)""")

M_CALENDAR = m_load("dim_calendar", """,
    AddDate = Table.AddColumn(Headers, "Date", each Date.FromText([month] & "-01"), type date),
    Typed = Table.TransformColumnTypes(AddDate, {
        {"month", type text}, {"year", Int64.Type}, {"month_no", Int64.Type},
        {"month_name", type text}, {"quarter", type text}, {"season", type text}}),
    Result = Table.RenameColumns(Typed, {
        {"month","Month Key"}, {"year","Year"}, {"month_no","Month No"},
        {"month_name","Month"}, {"quarter","Quarter"}, {"season","Season"}},
        MissingField.Ignore)""")

M_PLAN = m_load("fact_plan", """,
    Typed = Table.TransformColumnTypes(Headers, {
        {"sku", type text}, {"month", type text},
        {"forecast_units", type number}, {"scheduled_receipts", type number},
        {"planned_receipts", type number}, {"closing_units", type number},
        {"stockout_units", type number}, {"reorder_point", type number},
        {"safety_stock", type number}, {"order_up_to", type number},
        {"weeks_of_supply", type number}, {"unit_cost_usd", type number},
        {"supplier_id", type text}, {"lead_time_days", Int64.Type},
        {"order_placed_units", type number}, {"order_value_usd", type number},
        {"closing_value_usd", type number}}),
    AddDate = Table.AddColumn(Typed, "Date", each Date.FromText([month] & "-01"), type date),
    AddInbound = Table.AddColumn(AddDate, "Inbound Units",
        each [scheduled_receipts] + [planned_receipts], type number),
    AddAction = Table.AddColumn(AddInbound, "Action", each
        if [closing_units] <= 0 and [forecast_units] > 0 then "Stockout"
        else if [closing_units] > [order_up_to] then "Excess"
        else if [closing_units] >= [reorder_point] then "In range"
        else if [closing_units] + [Inbound Units] >= [reorder_point] then "In transit"
        else "Buy", type text),
    AddOrder = Table.AddColumn(AddAction, "Action Rank", each
        if [Action]="Stockout" then 1 else if [Action]="Buy" then 2
        else if [Action]="In transit" then 3 else if [Action]="In range" then 4
        else 5, Int64.Type),
    AddGap = Table.AddColumn(AddOrder, "Gap to Min Units",
        each List.Max({[reorder_point] - [closing_units], 0}), type number),
    AddGapV = Table.AddColumn(AddGap, "Gap to Min USD",
        each [Gap to Min Units] * [unit_cost_usd], type number),
    Result = Table.RenameColumns(AddGapV, {
        {"sku","SKU"}, {"month","Month Key"}, {"forecast_units","Demand Units"},
        {"scheduled_receipts","In Transit Units"},
        {"planned_receipts","Planned Receipts"}, {"closing_units","On Hand Units"},
        {"stockout_units","Stockout Units"}, {"reorder_point","Min"},
        {"order_up_to","Max"}, {"safety_stock","Safety Stock"},
        {"weeks_of_supply","Weeks of Supply"}, {"unit_cost_usd","Unit Cost"},
        {"supplier_id","Supplier ID"}, {"lead_time_days","Lead Time Days"},
        {"order_placed_units","Order Units"}, {"order_value_usd","Order USD"},
        {"closing_value_usd","On Hand USD"}}, MissingField.Ignore)""")

M_ALLOCATION = m_load("fact_allocation", """,
    Typed = Table.TransformColumnTypes(Headers, {
        {"sku", type text}, {"month", type text}, {"channel", type text},
        {"account", type text}, {"node", type text},
        {"requirement_units", type number}, {"allocated_units", type number},
        {"short_units", type number}, {"allocated_value_usd", type number}}),
    AddDate = Table.AddColumn(Typed, "Date", each Date.FromText([month] & "-01"), type date),
    Result = Table.RenameColumns(AddDate, {
        {"sku","SKU"}, {"month","Month Key"}, {"channel","Channel"},
        {"account","Account"}, {"node","Fulfilment Node"},
        {"requirement_units","Requirement Units"},
        {"allocated_units","Allocated Units"}, {"short_units","Short Units"},
        {"allocated_value_usd","Allocated USD"}}, MissingField.Ignore)""")


# =============================================================================
# Measures
# =============================================================================
MEASURES = [
    ("SKUs Below Min",
     'CALCULATE ( DISTINCTCOUNT ( Plan[SKU] ), Plan[Action] IN {"Buy","In transit","Stockout"} )',
     "#,0"),
    ("To Buy $",
     'CALCULATE ( SUM ( Plan[Gap to Min USD] ), Plan[Action] = "Buy" )',
     '\\$#,0;(\\$#,0);\\$#,0'),
    ("Covered by Transit $",
     'CALCULATE ( SUM ( Plan[Gap to Min USD] ), Plan[Action] = "In transit" )',
     '\\$#,0;(\\$#,0);\\$#,0'),
    ("Excess $",
     'CALCULATE ( SUMX ( Plan, ( Plan[On Hand Units] - Plan[Max] ) * Plan[Unit Cost] ), '
     'Plan[Action] = "Excess" )',
     '\\$#,0;(\\$#,0);\\$#,0'),
    ("On Hand $", "SUM ( Plan[On Hand USD] )", '\\$#,0;(\\$#,0);\\$#,0'),
    ("Demand Units", "SUM ( Plan[Demand Units] )", "#,0"),
    ("Planned Purchases $", "SUM ( Plan[Order USD] )", '\\$#,0;(\\$#,0);\\$#,0'),
    ("COGS $", "SUMX ( Plan, Plan[Demand Units] * Plan[Unit Cost] )",
     '\\$#,0;(\\$#,0);\\$#,0'),
    ("Average Inventory $",
     "AVERAGEX ( VALUES ( 'Calendar'[Month Key] ), [On Hand $] )",
     '\\$#,0;(\\$#,0);\\$#,0'),
    ("Inventory Turns",
     "DIVIDE ( [COGS $], [Average Inventory $] ) * DIVIDE ( 12, "
     "DISTINCTCOUNT ( 'Calendar'[Month Key] ) )",
     "0.0"),
    ("Weeks of Supply", "DIVIDE ( [On Hand $], DIVIDE ( [COGS $], 4.33 ) )", "0.0"),
    ("Requirement Units", "SUM ( Allocation[Requirement Units] )", "#,0"),
    ("Allocated Units", "SUM ( Allocation[Allocated Units] )", "#,0"),
    ("Short Units", "SUM ( Allocation[Short Units] )", "#,0"),
    ("Fill Rate", "DIVIDE ( [Allocated Units], [Requirement Units] )", "0.0%"),
    ("Stockout Item-Months",
     "CALCULATE ( COUNTROWS ( Plan ), Plan[Stockout Units] > 0 )", "#,0"),
    # The capped-coverage pattern: excess on one SKU must not offset a shortage
    # on another, so credit is capped per item before summing.
    ("Covered $ (capped per SKU)",
     "SUMX ( VALUES ( Plan[SKU] ), MIN ( [On Hand $], "
     "CALCULATE ( SUMX ( Plan, Plan[Min] * Plan[Unit Cost] ) ) ) )",
     '\\$#,0;(\\$#,0);\\$#,0'),
    ("Required $ (at Min)",
     "SUMX ( Plan, Plan[Min] * Plan[Unit Cost] )", '\\$#,0;(\\$#,0);\\$#,0'),
    ("% Covered", "DIVIDE ( [Covered $ (capped per SKU)], [Required $ (at Min)] )", "0.0%"),
]


def col(name, dtype, fmt=None, hidden=False, sort_by=None, summarize=None):
    c = {"name": name, "dataType": dtype,
         "sourceColumn": name, "summarizeBy": summarize or "none"}
    if fmt:
        c["formatString"] = fmt
    if hidden:
        c["isHidden"] = True
    if sort_by:
        c["sortByColumn"] = sort_by
    return c


def table(name, m_expr, columns, measures=None):
    t = {
        "name": name,
        "columns": columns,
        "partitions": [{
            "name": f"{name}-partition",
            "mode": "import",
            "source": {"type": "m", "expression": m_expr},
        }],
    }
    if measures:
        t["measures"] = measures
    return t


def build_model() -> dict:
    measures = [{"name": n, "expression": e, "formatString": f} for n, e, f in MEASURES]

    tables = [
        table("Products", M_PRODUCTS, [
            col("SKU", "string"), col("Product", "string"),
            col("Division", "string"), col("Category", "string"),
            col("Subcategory", "string"), col("Supplier ID", "string"),
            col("Unit Cost", "double", '\\$#,0.00'),
            col("Retail Price", "double", '\\$#,0.00'),
            col("Case Pack", "int64"), col("MOQ", "int64"),
            col("Lead Time Days", "int64"), col("Sourcing", "string"),
        ]),
        table("Suppliers", M_SUPPLIERS, [
            col("Supplier ID", "string"), col("Origin", "string"),
            col("Supplier Lead Time", "int64"),
            col("Origin Surcharge", "double", "0.00%"),
            col("Supplier Sourcing", "string"), col("Payment Terms", "string"),
        ]),
        table("Accounts", M_ACCOUNTS, [
            col("Account", "string"), col("Channel", "string"),
            col("Fulfilment Node", "string"), col("Node City", "string"),
        ]),
        table("Calendar", M_CALENDAR, [
            col("Month Key", "string"), col("Date", "dateTime", "General Date"),
            col("Year", "int64"), col("Month No", "int64"),
            col("Month", "string"), col("Quarter", "string"), col("Season", "string"),
        ]),
        table("Plan", M_PLAN, [
            col("SKU", "string"), col("Month Key", "string"),
            col("Date", "dateTime", "General Date"),
            col("Demand Units", "double", "#,0"),
            col("In Transit Units", "double", "#,0"),
            col("Planned Receipts", "double", "#,0"),
            col("Inbound Units", "double", "#,0"),
            col("On Hand Units", "double", "#,0"),
            col("Stockout Units", "double", "#,0"),
            col("Min", "double", "#,0"), col("Max", "double", "#,0"),
            col("Safety Stock", "double", "#,0"),
            col("Weeks of Supply", "double", "0.0"),
            col("Unit Cost", "double", '\\$#,0.00'),
            col("Supplier ID", "string"), col("Lead Time Days", "int64"),
            col("Order Units", "double", "#,0"),
            col("Order USD", "double", '\\$#,0'),
            col("On Hand USD", "double", '\\$#,0'),
            col("Gap to Min Units", "double", "#,0"),
            col("Gap to Min USD", "double", '\\$#,0'),
            col("Action", "string", sort_by="Action Rank"),
            col("Action Rank", "int64", hidden=True),
        ], measures),
        table("Allocation", M_ALLOCATION, [
            col("SKU", "string"), col("Month Key", "string"),
            col("Date", "dateTime", "General Date"),
            col("Channel", "string"), col("Account", "string"),
            col("Fulfilment Node", "string"),
            col("Requirement Units", "double", "#,0"),
            col("Allocated Units", "double", "#,0"),
            col("Short Units", "double", "#,0"),
            col("Allocated USD", "double", '\\$#,0'),
        ]),
    ]

    def rel(rid, ft, fc, tt, tc):
        return {"name": rid, "fromTable": ft, "fromColumn": fc,
                "toTable": tt, "toColumn": tc,
                "crossFilteringBehavior": "oneDirection"}

    relationships = [
        rel("plan_product", "Plan", "SKU", "Products", "SKU"),
        rel("plan_calendar", "Plan", "Month Key", "Calendar", "Month Key"),
        rel("alloc_product", "Allocation", "SKU", "Products", "SKU"),
        rel("alloc_calendar", "Allocation", "Month Key", "Calendar", "Month Key"),
        rel("alloc_account", "Allocation", "Account", "Accounts", "Account"),
        rel("product_supplier", "Products", "Supplier ID", "Suppliers", "Supplier ID"),
    ]

    return {
        "name": "ReplenishmentControlTower",
        "compatibilityLevel": 1567,
        "model": {
            "culture": "en-US",
            "dataAccessOptions": {"legacyRedirects": True,
                                  "returnErrorValuesAsNull": True},
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "sourceQueryCulture": "en-US",
            "tables": tables,
            "relationships": relationships,
            "annotations": [
                {"name": "PBI_QueryOrder",
                 "value": json.dumps(["Products", "Suppliers", "Accounts",
                                      "Calendar", "Plan", "Allocation"])},
                {"name": "__PBI_TimeIntelligenceEnabled", "value": "0"},
            ],
        },
    }


# =============================================================================
# Report layout
# =============================================================================
def card(name, x, y, w, h, z, measure):
    return {
        "name": name,
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": z,
                                           "width": w, "height": h, "tabOrder": z}}],
        "singleVisual": {
            "visualType": "card",
            "projections": {"Values": [{"queryRef": f"Plan.{measure}"}]},
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "p", "Entity": "Plan", "Type": 0}],
                "Select": [{"Measure": {"Expression": {"SourceRef": {"Source": "p"}},
                                        "Property": measure},
                            "Name": f"Plan.{measure}",
                            "NativeReferenceName": measure}],
            },
            "drillFilterOtherVisuals": True,
            "objects": {"labels": [{"properties": {
                "fontSize": {"expr": {"Literal": {"Value": "28D"}}}}}]},
        },
    }


def bar(name, x, y, w, h, z):
    return {
        "name": name,
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": z,
                                           "width": w, "height": h, "tabOrder": z}}],
        "singleVisual": {
            "visualType": "clusteredBarChart",
            "projections": {
                "Category": [{"queryRef": "Plan.Action"}],
                "Y": [{"queryRef": "Plan.SKUs Below Min"}],
            },
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "p", "Entity": "Plan", "Type": 0}],
                "Select": [
                    {"Column": {"Expression": {"SourceRef": {"Source": "p"}},
                                "Property": "Action"},
                     "Name": "Plan.Action", "NativeReferenceName": "Action"},
                    {"Measure": {"Expression": {"SourceRef": {"Source": "p"}},
                                 "Property": "SKUs Below Min"},
                     "Name": "Plan.SKUs Below Min",
                     "NativeReferenceName": "SKUs Below Min"},
                ],
            },
            "drillFilterOtherVisuals": True,
        },
    }


def tbl(name, x, y, w, h, z, cols):
    """cols: list of (entity_alias, entity, property, is_measure)"""
    from_map, selects, projections = {}, [], []
    for alias, entity, prop, is_measure in cols:
        from_map[alias] = entity
        ref = f"{entity}.{prop}"
        key = "Measure" if is_measure else "Column"
        selects.append({key: {"Expression": {"SourceRef": {"Source": alias}},
                              "Property": prop},
                        "Name": ref, "NativeReferenceName": prop})
        projections.append({"queryRef": ref})
    return {
        "name": name,
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": z,
                                           "width": w, "height": h, "tabOrder": z}}],
        "singleVisual": {
            "visualType": "tableEx",
            "projections": {"Values": projections},
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": a, "Entity": e, "Type": 0}
                         for a, e in from_map.items()],
                "Select": selects,
            },
            "drillFilterOtherVisuals": True,
        },
    }


def textbox(name, x, y, w, h, z, text, size=20, bold=True):
    runs = [{"value": text, "textStyle": {
        "fontSize": f"{size}pt", "fontWeight": "bold" if bold else "normal",
        "color": "#1F4E3D"}}]
    return {
        "name": name,
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": z,
                                           "width": w, "height": h, "tabOrder": z}}],
        "singleVisual": {
            "visualType": "textbox",
            "drillFilterOtherVisuals": True,
            "objects": {"general": [{"properties": {"paragraphs": [
                {"textRuns": runs}]}}]},
        },
    }


def build_report():
    """Two pages. Page 1 is the decision view, page 2 is service."""
    p1 = [
        textbox("t1", 20, 16, 900, 46,
                0, "Replenishment Control Tower — Back to the Roots"),
        textbox("t2", 20, 60, 1100, 32, 100,
                "Below the minimum is not one situation. It is three, "
                "and each one belongs to a different person.", 11, False),
        card("c1", 20, 105, 260, 130, 1000, "SKUs Below Min"),
        card("c2", 292, 105, 260, 130, 1010, "To Buy $"),
        card("c3", 564, 105, 260, 130, 1020, "Covered by Transit $"),
        card("c4", 836, 105, 260, 130, 1030, "Excess $"),
        bar("b1", 20, 250, 530, 300, 2000),
        tbl("tb1", 564, 250, 532, 300, 2010, [
            ("p", "Plan", "Action", False),
            ("p", "Plan", "SKUs Below Min", True),
            ("p", "Plan", "To Buy $", True),
        ]),
        tbl("tb2", 20, 566, 1076, 330, 3000, [
            ("pr", "Products", "Product", False),
            ("pr", "Products", "Category", False),
            ("p", "Plan", "Action", False),
            ("p", "Plan", "On Hand Units", False),
            ("p", "Plan", "Min", False),
            ("p", "Plan", "Max", False),
            ("p", "Plan", "In Transit Units", False),
            ("p", "Plan", "Gap to Min USD", False),
        ]),
    ]
    p2 = [
        textbox("t3", 20, 16, 900, 46, 0, "Service, Coverage and Cost"),
        card("c5", 20, 90, 260, 130, 1000, "Fill Rate"),
        card("c6", 292, 90, 260, 130, 1010, "Inventory Turns"),
        card("c7", 564, 90, 260, 130, 1020, "On Hand $"),
        card("c8", 836, 90, 260, 130, 1030, "Planned Purchases $"),
        tbl("tb3", 20, 240, 530, 340, 2000, [
            ("a", "Accounts", "Account", False),
            ("p", "Plan", "Requirement Units", True),
            ("p", "Plan", "Allocated Units", True),
            ("p", "Plan", "Short Units", True),
            ("p", "Plan", "Fill Rate", True),
        ]),
        tbl("tb4", 564, 240, 532, 340, 2010, [
            ("s", "Suppliers", "Origin", False),
            ("s", "Suppliers", "Supplier ID", False),
            ("s", "Suppliers", "Supplier Lead Time", False),
            ("s", "Suppliers", "Origin Surcharge", False),
        ]),
        tbl("tb5", 20, 596, 1076, 300, 3000, [
            ("c", "Calendar", "Month", False),
            ("p", "Plan", "Demand Units", True),
            ("p", "Plan", "On Hand $", True),
            ("p", "Plan", "Weeks of Supply", True),
            ("p", "Plan", "Stockout Item-Months", True),
        ]),
    ]
    return [("Control Tower", p1), ("Service & Cost", p2)]


def write_project():
    if PROJ.exists():
        shutil.rmtree(PROJ)
    (PROJ / "Model").mkdir(parents=True)
    (PROJ / "Report" / "sections").mkdir(parents=True)

    def dump(path: Path, obj, indent=2):
        path.write_text(json.dumps(obj, indent=indent, ensure_ascii=False),
                        encoding="utf-8")

    dump(PROJ / ".pbixproj.json", {
        "version": "1.0", "settings": {"model": {"serializationMode": "Raw"}}})
    (PROJ / "Version.txt").write_text("1.28", encoding="utf-8")
    dump(PROJ / "ReportMetadata.json", {"Version": 5, "AutoCreatedRelationships": [],
                                        "CreatedFrom": "Desktop"})
    dump(PROJ / "ReportSettings.json", {
        "Version": 4,
        "ReportSettings": {"UserConsentsToCompositeModels": True},
        "QueriesSettings": {"TypeDetectionEnabled": False,
                            "RelationshipImportEnabled": False}})
    dump(PROJ / "DiagramLayout.json", {"version": "1.1.0", "diagrams": []})
    dump(PROJ / "Model" / "database.json", build_model())
    dump(PROJ / "Report" / "report.json",
         {"id": 0, "layoutOptimization": 0, "resourcePackages": []})
    dump(PROJ / "Report" / "config.json", {"version": "5.55"})

    for i, (display, visuals) in enumerate(build_report()):
        sec = PROJ / "Report" / "sections" / f"{i:03d}_{display.replace(' ', '')}"
        (sec / "visualContainers").mkdir(parents=True)
        dump(sec / "section.json", {
            "displayName": display, "displayOption": 1,
            "name": f"page{i}section{i:04d}", "ordinal": i,
            "width": 1280, "height": 920})
        dump(sec / "config.json", {})
        dump(sec / "filters.json", [])
        for j, v in enumerate(visuals):
            vc = sec / "visualContainers" / f"{j:05d}_{v['singleVisual']['visualType']}"
            vc.mkdir(parents=True)
            pos = v["layouts"][0]["position"]
            dump(vc / "config.json", v)
            dump(vc / "visualContainer.json", {
                "x": pos["x"], "y": pos["y"], "z": pos["z"],
                "width": pos["width"], "height": pos["height"],
                "tabOrder": pos["tabOrder"]})
            dump(vc / "filters.json", [])
    return PROJ


def build_and_write() -> Path:
    """Write the .pbit directly (pbi-tools 1.2 cannot package for Desktop 2.157)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pbit_writer import build_layout, verify, write_pbit

    model = build_model()
    layout = build_layout(build_report())
    write_pbit(OUT, model, layout)
    rep = verify(OUT)
    s = rep["summary"]
    print(f"  tables        : {', '.join(s['tables'])}")
    print(f"  measures      : {s['measures']}")
    print(f"  relationships : {s['relationships']}")
    for name, n in s["pages"]:
        print(f"  page          : {name} ({n} visuals)")
    print(f"  parts         : {len(rep['parts'])}")
    return OUT


def find_pbi_tools() -> Path | None:
    for c in PBI_TOOLS_CANDIDATES:
        if c.exists():
            return c
    found = shutil.which("pbi-tools")
    return Path(found) if found else None


def main() -> int:
    print("Building the Power BI template ...")
    build_and_write()
    print("")
    print(f"  OK  {OUT.name}  ({OUT.stat().st_size/1024:.0f} KB)")
    print(f"      {OUT}")
    print("      Open it in Power BI Desktop and let it load; the data comes")
    print("      over HTTPS from the public repository.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
