"""
gui/config_window.py
=====================
The station-config dialog: edits the values in station_config.py's
AppConfig (StationConfig's 6 fields, plus total_battery_capacity,
bottom_unusable_pct, red_line_threshold). Hidden by default — opened from a
button on the main window. "Save" persists to config.json (so the next run,
even in a fresh process, uses it); "Reset to Defaults" just repopulates the
form with the hardcoded defaults — you still need to hit Save to persist
that. "Cancel" discards whatever's in the form.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QMessageBox,
    QVBoxLayout,
)

from station_config import AppConfig, default_config, load_config, save_config

# (attr name, label, suffix, decimals, minimum, maximum, single step)
_STATION_FIELDS = [
    ("max_charge_rate",     "Max charge rate (C)",     " MW",     3, 0.0, 1000.0, 0.01),
    ("max_sell_rate",       "Max sell rate (S)",        " MW",     3, 0.0, 1000.0, 0.01),
    ("battery_capacity",    "Battery capacity (B)",     " MWh",    5, 0.0, 1000.0, 0.001),
    ("min_price_delta",     "Min price delta (Y)",      " EUR/MWh", 2, 0.0, 100000.0, 1.0),
    ("min_discharge_price", "Min discharge price (T)",  " EUR/MWh", 2, 0.0, 100000.0, 1.0),
    ("discharge_loss_pct",  "Discharge loss",           " %",      2, 0.0, 99.999, 0.1),
]

_OTHER_FIELDS = [
    ("total_battery_capacity", "Total battery capacity", " MWh", 5, 0.0, 1000.0, 0.001),
    ("bottom_unusable_pct",    "Bottom unusable",         " %",   2, 0.0, 100.0, 0.5),
    ("red_line_threshold",     "Red-line price threshold", " EUR/MWh", 2, 0.0, 100000.0, 1.0),
]


class ConfigDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Station Configuration")
        self.setMinimumWidth(380)

        self._spinboxes: dict[str, QDoubleSpinBox] = {}

        layout = QVBoxLayout(self)

        station_group = QGroupBox("Station Config")
        station_form = QFormLayout(station_group)
        for attr, label, suffix, decimals, lo, hi, step in _STATION_FIELDS:
            self._add_field(station_form, attr, label, suffix, decimals, lo, hi, step)
        layout.addWidget(station_group)

        other_group = QGroupBox("Other Settings")
        other_form = QFormLayout(other_group)
        for attr, label, suffix, decimals, lo, hi, step in _OTHER_FIELDS:
            self._add_field(other_form, attr, label, suffix, decimals, lo, hi, step)
        layout.addWidget(other_group)

        buttons = QDialogButtonBox()
        self.reset_button = buttons.addButton("Reset to Defaults", QDialogButtonBox.ButtonRole.ResetRole)
        self.save_button = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.reset_button.clicked.connect(self._reset_to_defaults)
        self.save_button.clicked.connect(self._save)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)

        self._populate(load_config())

    def _add_field(
        self,
        form: QFormLayout,
        attr: str,
        label: str,
        suffix: str,
        decimals: int,
        lo: float,
        hi: float,
        step: float,
    ) -> None:
        box = QDoubleSpinBox()
        box.setRange(lo, hi)
        box.setDecimals(decimals)
        box.setSingleStep(step)
        box.setSuffix(suffix)
        form.addRow(f"{label}:", box)
        self._spinboxes[attr] = box

    def _populate(self, cfg: AppConfig) -> None:
        for attr, box in self._spinboxes.items():
            box.setValue(getattr(cfg, attr))

    def _reset_to_defaults(self) -> None:
        self._populate(default_config())

    def _current_values(self) -> dict[str, float]:
        return {attr: box.value() for attr, box in self._spinboxes.items()}

    def _save(self) -> None:
        values = self._current_values()
        cfg = AppConfig(**values)
        try:
            cfg.station_config()  # validates (e.g. T >= Y, loss_pct in [0, 100))
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid configuration", str(exc))
            return
        save_config(cfg)
        self.accept()
