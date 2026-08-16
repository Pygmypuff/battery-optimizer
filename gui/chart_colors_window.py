"""
gui/chart_colors_window.py
============================
The output-chart color customization dialog: edits station_config.py's
AppConfig.{charge_color,discharge_color,hold_color} — the CHARGE/DISCHARGE/
HOLD bar colors in the generated output workbook's chart. Opened from a
"Customize output chart…" button in gui.config_window.ConfigDialog.

Each color has both a clickable swatch (opens Qt's native QColorDialog —
a visual color wheel/grid, not just text) and a hex code field, kept in
sync in both directions, so typing a code still works exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import replace

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QColor, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QColorDialog,
)

from station_config import default_config, load_config, save_config

_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")

# (AppConfig attr, label)
_COLOR_FIELDS = [
    ("charge_color", "Charge"),
    ("discharge_color", "Discharge"),
    ("hold_color", "Hold"),
]


class ColorPickerRow(QWidget):
    """A clickable color swatch + a hex code field, kept in sync."""

    def __init__(self, initial_hex: str) -> None:
        super().__init__()
        self._hex = initial_hex.upper()

        self.swatch = QPushButton()
        self.swatch.setFixedSize(36, 24)
        self.swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch.clicked.connect(self._pick_color)

        self.hex_edit = QLineEdit()
        self.hex_edit.setMaxLength(6)
        self.hex_edit.setPlaceholderText("RRGGBB")
        self.hex_edit.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{0,6}")))
        self.hex_edit.textEdited.connect(self._on_hex_edited)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.swatch)
        layout.addWidget(self.hex_edit)

        self._refresh_swatch()
        self.hex_edit.setText(self._hex)

    def set_hex(self, hex_color: str) -> None:
        self._hex = hex_color.upper()
        self.hex_edit.setText(self._hex)
        self._refresh_swatch()

    def hex_value(self) -> str:
        return self._hex

    def _refresh_swatch(self) -> None:
        self.swatch.setStyleSheet(
            f"background-color: #{self._hex}; border: 1px solid #888888;"
        )

    def _pick_color(self) -> None:
        current = QColor(f"#{self._hex}") if _HEX_RE.match(self._hex) else QColor("#FFFFFF")
        color = QColorDialog.getColor(current, self, "Choose a color")
        if color.isValid():
            self.set_hex(color.name()[1:])

    def _on_hex_edited(self, text: str) -> None:
        if len(text) == 6:
            self.set_hex(text)


class ChartColorsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Customize Output Chart")
        self.setMinimumWidth(320)

        self._rows: dict[str, ColorPickerRow] = {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        for attr, label in _COLOR_FIELDS:
            row = ColorPickerRow("000000")
            form.addRow(f"{label}:", row)
            self._rows[attr] = row
        layout.addLayout(form)

        buttons = QDialogButtonBox()
        self.reset_button = buttons.addButton("Reset to Defaults", QDialogButtonBox.ButtonRole.ResetRole)
        self.save_button = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.reset_button.clicked.connect(self._reset_to_defaults)
        self.save_button.clicked.connect(self._save)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)

        self._populate(load_config())

    def _populate(self, cfg) -> None:
        for attr, row in self._rows.items():
            row.set_hex(getattr(cfg, attr))

    def _reset_to_defaults(self) -> None:
        self._populate(default_config())

    def _save(self) -> None:
        values = {}
        for attr, row in self._rows.items():
            hex_value = row.hex_value()
            if not _HEX_RE.match(hex_value):
                QMessageBox.warning(
                    self, "Invalid color", f"'{hex_value}' isn't a valid 6-digit hex color."
                )
                return
            values[attr] = hex_value.upper()

        # Merge into a freshly loaded config, same reasoning as
        # ConfigDialog._save() — don't clobber fields this dialog doesn't
        # manage (station config etc.).
        cfg = replace(load_config(), **values)
        save_config(cfg)
        self.accept()
