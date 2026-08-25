"""Demand planning.

Statistical baseline = deseasonalised moving average x damped seasonal index,
computed at the grain declared in ``config.yaml`` (``planning_level``).

Three design decisions worth defending in a review:

1. **Seasonal indices are estimated one level up** (category, not SKU). A single
   SKU has 2 observations per calendar month in a 24-month history -- far too
   few for a stable index. Its category has 20+. This is the classic
   bias/variance trade: accept a little bias to kill a lot of variance.

2. **Seasonality is damped** (``damping`` in the config). A raw index of 2.10 in
   December says "December is always 2.1x". Damping pulls the multiplier toward
   1.0 so one unusual holiday season cannot dictate next year's buy.

3. **The planner's override never destroys the statistical value.** Both are
   stored side by side, so forecast error can be attributed to the model or to
   the human, and the override can always be rolled back.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config


def _month_index(s: pd.Series) -> pd.Series:
    return s.apply(lambda p: p.month)


def seasonal_indices(
    history: pd.DataFrame,
    cfg: Config,
    level: str,
    value_col: str = "units",
) -> pd.DataFrame:
    """Multiplicative monthly indices estimated at ``level`` (e.g. category).

    Index = mean demand in calendar month m / mean demand across all months,
    normalised so the twelve indices average exactly 1.0, then damped.
    """
    sm = cfg.forecast.get("seasonality", {})
    if not sm.get("enabled", True):
        months = pd.DataFrame({"month_no": range(1, 13)})
        levels = history[level].drop_duplicates()
        return (
            months.merge(levels, how="cross")
            .assign(seasonal_index_raw=1.0, seasonal_index=1.0)
        )

    damping = float(sm.get("damping", 1.0))
    shrinkage_k = float(sm.get("shrinkage_k", 0.5))

    h = history.copy()
    h["month_no"] = _month_index(h["month"])
    h["year"] = h["month"].apply(lambda p: p.year)

    # Drop each item's pre-launch months. planning_history() zero-fills every
    # item across every month so that averages are honest, but a zero that
    # means "not launched yet" is not a zero that means "did not sell". Left in,
    # those rows bias the seasonal shape toward whichever months a new item
    # happened to miss.
    item_key = cfg.planning_keys[-1]
    if item_key in h.columns:
        first_sale = (
            h[h[value_col] > 0].groupby(item_key)["month"].min().rename("first_sale")
        )
        h = h.merge(first_sale, on=item_key, how="left")
        h = h[h["first_sale"].notna() & (h["month"] >= h["first_sale"])]

    grouped = h.groupby([level, "month_no"], as_index=False)[value_col].mean()
    overall = h.groupby(level, as_index=False)[value_col].mean().rename(
        columns={value_col: "overall_mean"}
    )
    idx = grouped.merge(overall, on=level)
    idx["seasonal_index"] = np.where(
        idx["overall_mean"] > 0, idx[value_col] / idx["overall_mean"], 1.0
    )

    # Normalise each group's twelve indices to average 1.0.
    norm = idx.groupby(level, as_index=False)["seasonal_index"].mean().rename(
        columns={"seasonal_index": "mean_index"}
    )
    idx = idx.merge(norm, on=level)
    idx["seasonal_index"] = np.where(
        idx["mean_index"] > 0, idx["seasonal_index"] / idx["mean_index"], 1.0
    )

    # ---- shrink toward 1.0 in proportion to the evidence --------------------
    # An earlier version of this used a hard gate: "fewer than 2 years of
    # history -> index = 1.0". That silently switched seasonality off for the
    # entire backtest (21 months / 12 = 1.75 years), and a flat forecast run
    # into a seasonal trough scored 18% accuracy. A cliff is the wrong shape for
    # this decision -- confidence in a seasonal estimate grows gradually with
    # the number of times that calendar month has actually been observed.
    #
    #     weight = n / (n + k)      n = years observed for that month
    #
    # so one observation is discounted hard, three are trusted most of the way,
    # and nothing is ever thrown away entirely.
    observed = (
        h.groupby([level, "month_no"])["year"].nunique().rename("years_observed").reset_index()
    )
    idx = idx.merge(observed, on=[level, "month_no"], how="left")
    idx["years_observed"] = idx["years_observed"].fillna(0)
    idx["confidence"] = idx["years_observed"] / (idx["years_observed"] + shrinkage_k)
    idx["seasonal_index"] = 1.0 + (idx["seasonal_index"] - 1.0) * idx["confidence"]

    # Two indices are returned on purpose:
    #   seasonal_index_raw -- the evidence-weighted estimate of the shape, used
    #                         to STRIP seasonality out of history;
    #   seasonal_index     -- the damped version, used to PUT seasonality back
    #                         when projecting forward.
    # Using the damped index for both would leave part of the seasonal swing
    # inside the "deseasonalised" level, so a base period taken in spring would
    # forecast summer too high.
    idx["seasonal_index_raw"] = idx["seasonal_index"]
    idx["seasonal_index"] = 1.0 + (idx["seasonal_index"] - 1.0) * damping
    return idx[[level, "month_no", "years_observed", "confidence",
                "seasonal_index_raw", "seasonal_index"]]


def _deseasonalise(
    history: pd.DataFrame, indices: pd.DataFrame, level: str, value_col: str
) -> pd.DataFrame:
    h = history.copy()
    h["month_no"] = _month_index(h["month"])
    h = h.merge(
        indices[[level, "month_no", "seasonal_index_raw"]], on=[level, "month_no"], how="left"
    )
    h["seasonal_index_raw"] = h["seasonal_index_raw"].fillna(1.0)
    h["deseasonalised"] = np.where(
        h["seasonal_index_raw"] > 0, h[value_col] / h["seasonal_index_raw"], h[value_col]
    )
    return h


def build_forecast(
    history: pd.DataFrame,
    cfg: Config,
    seasonality_level: str = "category",
    value_col: str = "units",
    horizon: int | None = None,
    as_of: pd.Period | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(forecast, indices)``.

    ``history`` must carry the planning keys, a ``month`` Period column, the
    seasonality level, and ``value_col``. One row per planning key per month.
    """
    keys = cfg.planning_keys
    horizon = horizon or cfg.horizon_months
    as_of = as_of or history["month"].max()
    window = int(cfg.forecast["moving_average_window"])

    # The seasonality level may itself be one of the planning keys (e.g. when
    # planning at SKU inside a category); do not carry it twice.
    group_keys = keys + ([seasonality_level] if seasonality_level not in keys else [])

    idx = seasonal_indices(history, cfg, seasonality_level, value_col)
    h = _deseasonalise(history, idx, seasonality_level, value_col)

    # ---- base level: mean of the last `window` deseasonalised months --------
    recent = h[h["month"] > as_of - window]
    level = (
        recent.groupby(group_keys, as_index=False)["deseasonalised"]
        .mean()
        .rename(columns={"deseasonalised": "base_level"})
    )

    # ---- trend: year-over-year change of deseasonalised demand -------------
    last_12 = h[h["month"] > as_of - 12].groupby(keys, as_index=False)["deseasonalised"].sum()
    prior_12 = (
        h[(h["month"] <= as_of - 12) & (h["month"] > as_of - 24)]
        .groupby(keys, as_index=False)["deseasonalised"].sum()
        .rename(columns={"deseasonalised": "prior_12"})
    )
    trend = last_12.merge(prior_12, on=keys, how="left")
    trend["yoy_growth"] = np.where(
        trend["prior_12"].fillna(0) > 0,
        trend["deseasonalised"] / trend["prior_12"] - 1.0,
        0.0,
    )
    # Cap the extrapolated trend: a SKU that tripled will not triple again.
    trend["yoy_growth"] = trend["yoy_growth"].clip(-0.30, 0.35)
    level = level.merge(trend[keys + ["yoy_growth"]], on=keys, how="left")
    level["yoy_growth"] = level["yoy_growth"].fillna(0.0)

    # ---- project ------------------------------------------------------------
    future = [as_of + i for i in range(1, horizon + 1)]
    grid = level.merge(pd.DataFrame({"month": future}), how="cross")
    grid["month_no"] = _month_index(grid["month"])
    grid["horizon_step"] = grid.groupby(keys).cumcount() + 1
    grid = grid.merge(
        idx[[seasonality_level, "month_no", "seasonal_index"]],
        on=[seasonality_level, "month_no"], how="left",
    )
    grid["seasonal_index"] = grid["seasonal_index"].fillna(1.0)
    # A moving average of a trending series does not describe "now" -- it
    # describes the midpoint of the window, (window-1)/2 months in the past. On
    # a growing book that lag shows up as a systematic under-forecast (it was
    # worth about -9% here across rolling origins). Carrying the trend forward
    # from the window's centre rather than from its right edge removes it.
    lag_months = (window - 1) / 2
    grid["trend_factor"] = (1 + grid["yoy_growth"]) ** (
        (grid["horizon_step"] + lag_months) / 12
    )
    grid["statistical_forecast"] = (
        grid["base_level"] * grid["seasonal_index"] * grid["trend_factor"]
    ).clip(lower=0).round(0)

    grid["manual_override"] = np.nan
    grid["final_forecast"] = grid["statistical_forecast"]
    grid["override_source"] = "statistical"

    cols = group_keys + [
        "month", "month_no", "horizon_step", "base_level", "seasonal_index",
        "yoy_growth", "statistical_forecast", "manual_override", "final_forecast",
        "override_source",
    ]
    return grid[cols].sort_values(keys + ["month"]).reset_index(drop=True), idx


def apply_overrides(forecast: pd.DataFrame, overrides: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Merge planner overrides in without losing the statistical baseline.

    ``overrides`` carries the planning keys, ``month`` and ``manual_override``.
    """
    if not cfg.forecast.get("allow_manual_override", True):
        return forecast
    keys = cfg.planning_keys + ["month"]
    out = forecast.drop(columns=["manual_override"]).merge(
        overrides[keys + ["manual_override"]], on=keys, how="left"
    )
    has = out["manual_override"].notna()
    out["final_forecast"] = np.where(
        has, out["manual_override"], out["statistical_forecast"]
    )
    out["override_source"] = np.where(has, "planner override", "statistical")
    return out


# -----------------------------------------------------------------------------
# Accuracy
# -----------------------------------------------------------------------------

def backtest(
    history: pd.DataFrame,
    cfg: Config,
    holdout_months: int = 3,
    seasonality_level: str = "category",
    value_col: str = "units",
) -> pd.DataFrame:
    """Re-forecast from ``holdout_months`` ago and score against what happened.

    Reported per planning key: WMAPE (weighted absolute % error) and bias.
    A forecast that is never scored is a forecast nobody should trust.
    """
    keys = cfg.planning_keys
    cutoff = history["month"].max() - holdout_months
    train = history[history["month"] <= cutoff]
    actual = history[history["month"] > cutoff]

    fc, _ = build_forecast(
        train, cfg, seasonality_level, value_col, horizon=holdout_months, as_of=cutoff
    )
    merged = actual.merge(
        fc[keys + ["month", "statistical_forecast"]], on=keys + ["month"], how="left"
    )
    merged["statistical_forecast"] = merged["statistical_forecast"].fillna(0)
    merged["abs_error"] = (merged[value_col] - merged["statistical_forecast"]).abs()
    merged["error"] = merged["statistical_forecast"] - merged[value_col]

    scored = merged.groupby(keys, as_index=False).agg(
        actual_units=(value_col, "sum"),
        forecast_units=("statistical_forecast", "sum"),
        abs_error=("abs_error", "sum"),
        error=("error", "sum"),
    )
    scored["wmape"] = np.where(
        scored["actual_units"] > 0, scored["abs_error"] / scored["actual_units"], np.nan
    )
    scored["bias_pct"] = np.where(
        scored["actual_units"] > 0, scored["error"] / scored["actual_units"], np.nan
    )
    scored["accuracy"] = 1 - scored["wmape"]
    return scored.sort_values("actual_units", ascending=False).reset_index(drop=True)


def summarise_accuracy(scored: pd.DataFrame) -> dict[str, float]:
    """Volume-weighted headline numbers -- the ones that belong on a slide."""
    total_actual = scored["actual_units"].sum()
    return {
        "wmape": scored["abs_error"].sum() / total_actual if total_actual else np.nan,
        "accuracy": 1 - scored["abs_error"].sum() / total_actual if total_actual else np.nan,
        "bias_pct": scored["error"].sum() / total_actual if total_actual else np.nan,
        "skus_scored": int(len(scored)),
        "skus_over_30pct_error": int((scored["wmape"] > 0.30).sum()),
    }


def rolling_backtest(
    history: pd.DataFrame,
    cfg: Config,
    origins: int = 5,
    horizon: int = 3,
    seasonality_level: str = "category",
    value_col: str = "units",
) -> pd.DataFrame:
    """Score the model from several successive cut-off dates, not just one.

    A single holdout window measures one draw from a noisy process. Tuning
    against it produces parameters that fit those three months and nothing else.
    Rolling the origin backwards and scoring each fold separately shows whether
    a parameter choice actually generalises -- and how stable accuracy is, which
    is itself worth reporting: a model that swings between 60% and 90% needs
    different governance than one that sits at 75% every month.
    """
    last = history["month"].max()
    rows: list[dict] = []
    for k in range(origins):
        cutoff = last - horizon - k
        train = history[history["month"] <= cutoff]
        actual = history[(history["month"] > cutoff) & (history["month"] <= cutoff + horizon)]
        if train["month"].nunique() < 13 or actual.empty:
            continue

        keys = cfg.planning_keys
        fc, _ = build_forecast(
            train, cfg, seasonality_level, value_col, horizon=horizon, as_of=cutoff
        )
        merged = actual.merge(
            fc[keys + ["month", "statistical_forecast"]], on=keys + ["month"], how="left"
        )
        merged["statistical_forecast"] = merged["statistical_forecast"].fillna(0)
        abs_err = (merged[value_col] - merged["statistical_forecast"]).abs().sum()
        err = (merged["statistical_forecast"] - merged[value_col]).sum()
        total = merged[value_col].sum()
        rows.append(
            {
                "origin": str(cutoff),
                "train_months": int(train["month"].nunique()),
                "actual_units": total,
                "wmape": abs_err / total if total else np.nan,
                "accuracy": 1 - abs_err / total if total else np.nan,
                "bias_pct": err / total if total else np.nan,
            }
        )
    return pd.DataFrame(rows)
