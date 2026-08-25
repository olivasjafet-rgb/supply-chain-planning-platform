"""Control Tower tab -- mirrors the production Direct Sales inventory report.

Same sections as the original: composition donuts, shortfall priority with the
Transfer-from-DC bucket, a coverage matrix drillable to SKU, excess analysis
split by sales activity, and the before/after DC-transfer scenario. Field names
in English; data is a simulated 320-SKU snapshot (fixed seed) so the donuts
read like a real catalogue.

Classification (the production rules, translated): below the minimum, a DC
covering >= 60% of the gap routes to Transfer from DC; inbound covering it
routes to In transit; only the remainder is Buy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]

COLORS = {
    "Transfer from DC": "#4C9A6E",
    "Buy": "#B4553F",
    "In transit": "#C9A227",
    "In range": "#1F4E3D",
    "Excess": "#8A9299",
    "Made to order": "#B8BDB6",
}
SHORT = ["Transfer from DC", "Buy", "In transit"]
AVAIL = ["In range", "Excess", "Made to order"]


@st.cache_data(show_spinner=False)
def load_snapshot() -> pd.DataFrame:
    raw = json.loads(
        (ROOT / "powerbi" / "snapshot_data.json").read_text(encoding="utf-8")
    )
    return pd.DataFrame(raw["rows"], columns=raw["cols"])


def k(v: float) -> str:
    return f"${v/1e3:,.1f}k"


def _donut(parts, centre_v, centre_label, key):
    parts = [p for p in parts if p[1] > 0]
    fig = go.Figure(go.Pie(
        labels=[p[0] for p in parts],
        values=[p[1] for p in parts],
        hole=0.62, sort=False, direction="clockwise",
        marker=dict(colors=[COLORS[p[0]] for p in parts],
                    line=dict(color="rgba(255,255,255,0.9)", width=2)),
        textinfo="none",
        hovertemplate="%{label}: %{value} SKUs (%{percent})<extra></extra>",
    ))
    fig.update_layout(
        height=250, margin=dict(t=8, b=8, l=8, r=8),
        legend=dict(orientation="h", y=-0.12, x=0),
        annotations=[dict(
            text=f"<b style='font-size:22px'>{centre_v}</b><br>"
                 f"<span style='font-size:11px'>{centre_label}</span>",
            showarrow=False)],
    )
    st.plotly_chart(fig, key=key, width="stretch")


def _matrix_frame(rows: pd.DataFrame, by: str) -> pd.DataFrame:
    out = []
    for name, g in rows.groupby(by, sort=True):
        gap = g.loc[g.status.isin(SHORT), "gap"].sum()
        out.append({
            by.capitalize(): name,
            "SKUs": len(g),
            "Available $": k(g.onHand.sum()),
            "Required $": k(g.reqMin.sum()),
            "Variance": k(g.onHand.sum() - g.reqMin.sum()),
            "Shortfall $": k(gap) if gap else "—",
            "Excess $": k(g.excess.sum()) if g.excess.sum() else "—",
            "Buy": int((g.status == "Buy").sum()),
            "DC transfer": int((g.status == "Transfer from DC").sum()),
            "In transit": int((g.status == "In transit").sum()),
            "In range": int((g.status == "In range").sum()),
            "Cover (mo)": round(g.onHand.sum() / g.avg4m.sum(), 1)
                          if g.avg4m.sum() else None,
            "% Avail": f"{g.onHand.sum()/g.reqMin.sum():.1%}"
                       if g.reqMin.sum() else "—",
        })
    return pd.DataFrame(out)


def render() -> None:
    d0 = load_snapshot()
    st.caption(
        "Mirrors the production Direct Sales report — simulated 320-SKU snapshot, "
        "English field names. Below the minimum, a DC covering ≥ 60% of the gap "
        "routes to **Transfer from DC**; inbound covering it routes to "
        "**In transit**; only the remainder is **Buy**."
    )

    # ---- filters -----------------------------------------------------------
    f1, f2, f3, _ = st.columns([1, 1, 1, 2])
    div = f1.selectbox("Division", ["All"] + sorted(d0.division.unique()), key="ct_div")
    pool = d0 if div == "All" else d0[d0.division == div]
    cat = f2.selectbox("Category", ["All"] + sorted(pool.category.unique()), key="ct_cat")
    abc = f3.selectbox("ABC class", ["All", "A", "B", "C"], key="ct_abc")
    d = pool if cat == "All" else pool[pool.category == cat]
    if abc != "All":
        d = d[d.abc == abc]
    if d.empty:
        st.info("Nothing matches these filters.")
        return

    short = d[d.status.isin(SHORT)]
    avail = d[d.status.isin(AVAIL)]

    # ---- KPI row -----------------------------------------------------------
    c = st.columns(5)
    c[0].metric("SKUs below minimum", len(short), f"{len(short)/len(d):.0%} of catalogue")
    c[1].metric("Buy $", k(d.loc[d.status == "Buy", "gap"].sum()), "no cover anywhere",
                delta_color="off")
    c[2].metric("Transfer from DC $", k(d.loc[d.status == "Transfer from DC", "gap"].sum()),
                "DC covers ≥ 60%", delta_color="off")
    c[3].metric("In transit $", k(d.loc[d.status == "In transit", "gap"].sum()),
                "inbound covers it", delta_color="off")
    c[4].metric("Excess $", k(d.excess.sum()), "above maximum", delta_color="off")

    # ---- composition + priority -------------------------------------------
    a, b, p = st.columns([1, 1, 1.1])
    with a:
        st.subheader("Available — composition")
        _donut([(s, int((avail.status == s).sum())) for s in AVAIL],
               len(avail), "available", "ct_d1")
    with b:
        st.subheader("Below minimum — composition")
        _donut([(s, int((short.status == s).sum())) for s in SHORT],
               len(short), "below min", "ct_d2")
    with p:
        st.subheader("Shortfall priority")
        for s, note in [("Transfer from DC", "DC covers ≥ 60% of the gap"),
                        ("Buy", "No cover anywhere"),
                        ("In transit", "Inbound already covers it")]:
            rs = d[d.status == s]
            st.markdown(
                f"<div style='display:flex;align-items:center;gap:.6rem;"
                f"padding:.55rem 0;border-bottom:1px solid rgba(128,128,128,.25)'>"
                f"<span style='width:13px;height:13px;border-radius:50%;"
                f"background:{COLORS[s]};flex:0 0 auto'></span>"
                f"<span style='flex:1'><b>{s}</b><br>"
                f"<span style='font-size:.75rem;opacity:.7'>{note} · {len(rs)} SKUs</span></span>"
                f"<span style='font-family:monospace;font-size:1.25rem;font-weight:600;"
                f"color:{COLORS[s]}'>{k(rs.gap.sum())}</span></div>",
                unsafe_allow_html=True)

    st.divider()

    # ---- coverage matrix, drillable ---------------------------------------
    st.subheader("Coverage matrix")
    st.dataframe(_matrix_frame(d, "division"), hide_index=True, width="stretch")
    m1, m2 = st.columns(2)
    drill_div = m1.selectbox("Open a division", ["—"] + sorted(d.division.unique()),
                             key="ct_drill_div")
    if drill_div != "—":
        dd = d[d.division == drill_div]
        st.dataframe(_matrix_frame(dd, "category"), hide_index=True, width="stretch")
        drill_cat = m2.selectbox("Open a category",
                                 ["—"] + sorted(dd.category.unique()), key="ct_drill_cat")
        if drill_cat != "—":
            sk = dd[dd.category == drill_cat].sort_values("gap", ascending=False)
            st.dataframe(pd.DataFrame({
                "SKU": sk.sku, "Product": sk.product, "Status": sk.status,
                "On hand $": sk.onHand.map(k), "Min $": sk.reqMin.map(k),
                "Max $": sk.reqMax.map(k),
                "Gap $": sk.gap.map(lambda v: k(v) if v else "—"),
                "DC $": sk.dc.map(lambda v: k(v) if v else "—"),
                "Transit $": sk.transit.map(lambda v: k(v) if v else "—"),
                "Excess $": sk.excess.map(lambda v: k(v) if v else "—"),
                "ABC": sk.abc,
            }), hide_index=True, width="stretch", height=330)

    st.divider()

    # ---- excess + DC scenario ---------------------------------------------
    e, s2 = st.columns(2)
    with e:
        st.subheader("Excess analysis")
        ex = d[d.status == "Excess"]
        no = ex.loc[ex.active == 0, "excess"].sum()
        yes = ex.loc[ex.active == 1, "excess"].sum()
        tot = no + yes
        if tot:
            fig = go.Figure(go.Pie(
                labels=["No active sales", "With active sales"], values=[no, yes],
                hole=0.62, sort=False,
                marker=dict(colors=["#B4553F", "#4C9A6E"],
                            line=dict(color="rgba(255,255,255,.9)", width=2)),
                textinfo="none",
                hovertemplate="%{label}: %{percent}<extra></extra>"))
            fig.update_layout(height=210, margin=dict(t=6, b=6, l=6, r=6),
                              legend=dict(orientation="h", y=-0.15),
                              annotations=[dict(text=f"<b>{k(tot)}</b><br>"
                                                "<span style='font-size:10px'>excess</span>",
                                                showarrow=False)])
            st.plotly_chart(fig, key="ct_d3", width="stretch")
            st.caption(
                f"**{no/tot:.0%}** of the excess has no rotation — it will not sell "
                f"itself down. {k(yes)} has movement and can be reduced.")
        else:
            st.info("No excess under these filters.")
    with s2:
        st.subheader("DC scenario — before → after")
        rec = short[(short.dc >= short.gap) & (short.gap > 0)]
        dc_app = min(short.dc.sum(), short.gap.sum())
        cov = dc_app / short.gap.sum() if short.gap.sum() else 0
        b1, b2 = st.columns(2)
        b1.metric("Shortfall (before)", f"{len(short)} SKUs", k(short.gap.sum()),
                  delta_color="off")
        b2.metric("Shortfall (after transfers)", f"{len(short)-len(rec)} SKUs",
                  f"-{k(rec.gap.sum())}", delta_color="normal")
        st.progress(min(cov, 1.0),
                    text=f"DC applicable: {k(dc_app)} — {cov:.0%} of the shortfall "
                         "is coverable from the DC")
        st.caption(
            f"**{len(rec)} SKUs recovered with a transfer** — cover the company "
            "already owns. Buying it instead would pay twice for the same units.")

    st.divider()

    # ---- availability analysis --------------------------------------------
    st.subheader("Availability analysis")
    rows = []
    def block(label, rs, bold=False):
        gap = rs.gap.sum()
        dc_a = min(rs.dc.sum(), gap) if gap else 0
        tr_a = min(rs.transit.sum(), gap) if gap else 0
        rows.append({
            "Supply status": label if not bold else f"**{label}**",
            "SKUs": len(rs), "% share": f"{len(rs)/len(d):.0%}",
            "Gap $": k(gap) if gap else "—",
            "DC applicable $": k(dc_a) if gap else "—",
            "Transit applicable $": k(tr_a) if gap else "—",
            "% DC covers": f"{dc_a/gap:.1%}" if gap else "—",
            "% transit covers": f"{tr_a/gap:.1%}" if gap else "—",
            "Recovered w/ transfer": str(int(((rs.dc >= rs.gap) & (rs.gap > 0)).sum()) or "—"),
        })
    block("Shortfall", short, bold=True)
    for s in ["Buy", "Transfer from DC", "In transit"]:
        block(f"· {s}", d[d.status == s])
    block("Available", avail, bold=True)
    for s in ["Made to order", "Excess", "In range"]:
        block(f"· {s}", d[d.status == s])
    block("Total", d, bold=True)
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
