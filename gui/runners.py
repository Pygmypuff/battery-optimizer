"""
gui/runners.py
===============
Thin wrappers around calculator.py (live Nordpool run) and test_from_excel.py
(test-mode run from a saved input workbook) that the GUI's worker thread
calls into. Both return the path to the generated, chart-colored .xlsx.
"""

from __future__ import annotations

from pathlib import Path

import calculator
import test_from_excel

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR  = PROJECT_DIR / "gui_output"


def run_nordpool_mode(station_power: float, battery_pct: float) -> Path:
    return calculator.run_nordpool(
        station_power=station_power, battery_pct=battery_pct, output_dir=OUTPUT_DIR
    )


def run_test_mode(excel_path: Path) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = excel_path.stem
    xlsx_out = OUTPUT_DIR / f"{stem}_output.xlsx"
    csv_out  = OUTPUT_DIR / f"{stem}_schedule.csv"
    template = PROJECT_DIR / "excel_template.xlsx"

    test_from_excel.process_one_file(
        excel_path=str(excel_path),
        sheet="BESS (15)",
        power_unit="kw",
        battery_pct=12.0,
        initial_charge_price=0.0,
        threshold=test_from_excel.red_line_threshold,
        template=str(template),
        csv_out=str(csv_out),
        xlsx_out=str(xlsx_out),
    )
    return xlsx_out
