"""Configuration loader.

The whole platform is driven by ``config.yaml``. Nothing in ``src/`` hard-codes a
product level, a location name or a planning grain -- those are read from the
config object built here. Re-pointing the engine at a different business is a
config change, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.yaml"


@dataclass(frozen=True)
class Hierarchy:
    """An ordered set of grouping levels, broadest first."""

    keys: list[str]
    labels: dict[str, str]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("a hierarchy needs at least one level")

    @property
    def leaf(self) -> str:
        return self.keys[-1]

    def label(self, key: str) -> str:
        return self.labels.get(key, key.replace("_", " ").title())

    def above(self, key: str) -> list[str]:
        """Levels that sit above ``key`` (its parents), broadest first."""
        if key not in self.keys:
            raise KeyError(f"{key!r} is not a level of this hierarchy: {self.keys}")
        return self.keys[: self.keys.index(key)]

    def through(self, key: str) -> list[str]:
        """Every level from the top down to and including ``key``."""
        return self.above(key) + [key]


@dataclass(frozen=True)
class Config:
    profile_name: str
    currency: str
    units: str
    fiscal_year_start_month: int
    products: Hierarchy
    locations: Hierarchy
    planning_level: str
    reporting_rollups: list[str]
    allocation_level: str
    stocking_level: str
    forecast: dict[str, Any]
    replenishment: dict[str, Any]
    landed_cost: dict[str, Any]
    kpi_targets: dict[str, Any]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    # -- convenience accessors used across the engine ------------------------

    @property
    def planning_keys(self) -> list[str]:
        """Columns that uniquely identify one planning row (e.g. down to SKU)."""
        return self.products.through(self.planning_level)

    @property
    def horizon_months(self) -> int:
        return int(self.forecast["horizon_months"])

    @property
    def history_months(self) -> int:
        return int(self.forecast["history_months"])

    @property
    def service_level(self) -> float:
        return float(self.replenishment["target_service_level"])

    def with_overrides(self, **changes: Any) -> "Config":
        """Return a copy with nested config blocks overridden.

        Used by the scenario runner: every scenario is the same code path with a
        different configuration, so a scenario can never quietly diverge from
        the baseline in some way the comparison does not capture.

            cfg.with_overrides(replenishment={"target_service_level": 0.99})
        """
        from dataclasses import replace as _replace

        merged: dict[str, Any] = {}
        for field_name, override in changes.items():
            current = getattr(self, field_name)
            if isinstance(current, dict) and isinstance(override, dict):
                merged[field_name] = {**current, **override}
            else:
                merged[field_name] = override
        return _replace(self, **merged)

    def describe(self) -> str:
        return (
            f"{self.profile_name} | plan at '{self.planning_level}' "
            f"({' > '.join(self.products.keys)}) | "
            f"stock at '{self.stocking_level}' | "
            f"{self.history_months}m history -> {self.horizon_months}m horizon"
        )


def _hierarchy(block: dict[str, Any]) -> Hierarchy:
    levels = block["levels"]
    return Hierarchy(
        keys=[lv["key"] for lv in levels],
        labels={lv["key"]: lv.get("label", lv["key"]) for lv in levels},
    )


def load_config(path: str | Path | None = None) -> Config:
    """Read a YAML profile and validate that its cross-references line up."""
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    products = _hierarchy(raw["product_hierarchy"])
    locations = _hierarchy(raw["location_hierarchy"])

    planning_level = raw["product_hierarchy"]["planning_level"]
    if planning_level not in products.keys:
        raise ValueError(
            f"planning_level {planning_level!r} is not one of the product levels "
            f"{products.keys}"
        )

    rollups = raw["product_hierarchy"].get("reporting_rollups", [])
    unknown = [r for r in rollups if r not in products.keys]
    if unknown:
        raise ValueError(f"reporting_rollups reference unknown levels: {unknown}")

    for name in ("allocation_level", "stocking_level"):
        value = raw["location_hierarchy"][name]
        if value not in locations.keys:
            raise ValueError(
                f"{name} {value!r} is not one of the location levels {locations.keys}"
            )

    return Config(
        profile_name=raw["profile_name"],
        currency=raw.get("currency", "USD"),
        units=raw.get("units", "each"),
        fiscal_year_start_month=int(raw.get("fiscal_year_start_month", 1)),
        products=products,
        locations=locations,
        planning_level=planning_level,
        reporting_rollups=rollups,
        allocation_level=raw["location_hierarchy"]["allocation_level"],
        stocking_level=raw["location_hierarchy"]["stocking_level"],
        forecast=raw["forecast"],
        replenishment=raw["replenishment"],
        landed_cost=raw["landed_cost"],
        kpi_targets=raw["kpi_targets"],
        raw=raw,
    )


if __name__ == "__main__":
    cfg = load_config()
    print(cfg.describe())
    print("planning keys :", cfg.planning_keys)
    print("rollups       :", cfg.reporting_rollups)
