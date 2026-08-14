"""
station_config.py
==================
Single source of truth for the station's hardware limits and economic
thresholds — the values that used to be hardcoded separately into both
calculator.py and test_from_excel.py.

`DEFAULTS` matches exactly what was previously hardcoded in those two
files. The GUI's config window (gui/config_window.py) reads/writes
`config.json` next to this file via `load_config()` / `save_config()` /
`reset_config()`; calculator.py and test_from_excel.py call `load_config()`
at the start of each run so a change saved from the GUI takes effect on the
very next run, without needing to restart the app.

Deliberately has no dependency on `calculator.py`, `nordpool`, or anything
else heavy — `battery_optimizer.py` (stdlib + optional numpy/scipy) is the
only import, so this stays cheap to import from either module.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from battery_optimizer import StationConfig

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULTS: dict[str, float] = {
    "max_charge_rate":        0.4,      # C   (MW)
    "max_sell_rate":          0.5,      # S   (MW)
    "battery_capacity":       0.77353,  # B   (MWh)
    "min_price_delta":        40.0,     # Y   (EUR/MWh)
    "min_discharge_price":    52.5,     # T   (EUR/MWh)
    "discharge_loss_pct":     2.0,      # 0-100 (%)
    "total_battery_capacity": 0.932,    # MWh (100% battery capacity) (233*4 kWh)
    "bottom_unusable_pct":    12.0,     # % of battery capacity unusable (bottom)
    "red_line_threshold":     12.5,     # EUR/MWh - price labels below this are colored red
}


@dataclass
class AppConfig:
    max_charge_rate:        float
    max_sell_rate:          float
    battery_capacity:       float
    min_price_delta:        float
    min_discharge_price:    float
    discharge_loss_pct:     float
    total_battery_capacity: float
    bottom_unusable_pct:    float
    red_line_threshold:     float

    def station_config(self) -> StationConfig:
        return StationConfig(
            max_charge_rate=self.max_charge_rate,
            max_sell_rate=self.max_sell_rate,
            battery_capacity=self.battery_capacity,
            min_price_delta=self.min_price_delta,
            min_discharge_price=self.min_discharge_price,
            discharge_loss_pct=self.discharge_loss_pct,
        )


def default_config() -> AppConfig:
    return AppConfig(**DEFAULTS)


def load_config() -> AppConfig:
    """Reads config.json, falling back to defaults for any missing/invalid field."""
    if not CONFIG_PATH.exists():
        return default_config()
    try:
        saved = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return default_config()

    merged = dict(DEFAULTS)
    for key in DEFAULTS:
        value = saved.get(key)
        if isinstance(value, (int, float)):
            merged[key] = float(value)
    return AppConfig(**merged)


def save_config(cfg: AppConfig) -> None:
    CONFIG_PATH.write_text(json.dumps(asdict(cfg), indent=2))


def reset_config() -> AppConfig:
    """Deletes config.json (if present) and returns the default config."""
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
    return default_config()


def calculate_usable_battery_capacity(battery_percentage: float, cfg: AppConfig) -> float:
    """
    Usable MWh from a total-battery %. We're not using the bottom
    `cfg.bottom_unusable_pct` of the battery.
    """
    if battery_percentage < cfg.bottom_unusable_pct:
        return 0.0
    return cfg.total_battery_capacity * (battery_percentage - cfg.bottom_unusable_pct) / 100
