"""
test_from_excel.py
===================
Test harness for the battery optimizer that sources its inputs (station
power + 15-minute price series) from a BESS forecast Excel file instead of
a live nordpool fetch. This lets you re-run the exact same optimisation
logic used in calculator.py against any historical/example day.

Input file expectations (sheet "BESS (15)" by default):
  - C3            : station power for the day. Stored in kW in this sheet
                     (confirmed against the "jaudas" sheet's 0-140 kW power
                     scale and the "Ranka(15)" sheet's C1 ~ 100-150 value),
                     so it is divided by 1000 to get MW for StationConfig/
                     StationState. Pass --power-unit mw if your sheet is
                     already in MW.
  - B4:B143       : time-of-day labels for each 15-min slot (informational
                     only, not used numerically).
  - C4:C143       : price EUR/MWh for each of the 140 slots (must be
                     exactly 140 rows, matching calculator.py's expectation).

This script intentionally does NOT import calculator.py, because
calculator.py imports the `nordpool` package at module scope purely to
fetch live prices, which isn't needed for offline testing and may not even
be installed in a test environment. Config values (StationConfig fields,
total_battery_capacity, bottom_unusable_pct, red_line_threshold) come from
station_config.py instead, and the actual workbook-writing logic comes from
excel_output.py — both dependency-light shared modules with no nordpool
import, so this script and calculator.py can both use them freely.

Optionally also writes a clean, single-sheet formatted .xlsx (schedule
table + colored bar chart matching calculator.py's chart styling: red
price labels below a threshold, and a light gradient background marking
day segments) built from scratch — it does not carry over any of the extra
sheets from the input workbook.

Usage:
    python test_from_excel.py path/to/BESS2026_08_06.xlsx
    python test_from_excel.py path/to/file.xlsx --sheet "BESS (15)" \
        --battery-pct 12 --initial-charge-price 0 \
        --out schedule.csv --xlsx-out schedule.xlsx
"""

from __future__ import annotations

import argparse
import csv
import os
from dataclasses import dataclass

from openpyxl import load_workbook

from battery_optimizer import StationState, optimise_battery_schedule, print_schedule
from excel_output import build_color_map, generate_formatted_excel
from station_config import calculate_usable_battery_capacity, load_config

# Snapshot for module-level defaults (e.g. the --threshold CLI flag's
# default). process_one_file() always reloads fresh via load_config() so a
# change saved from the GUI's config window applies on the very next run.
_DEFAULT_APP_CFG = load_config()
BASE_CFG = _DEFAULT_APP_CFG.station_config()
total_battery_capacity = _DEFAULT_APP_CFG.total_battery_capacity
bottom_unusable_pct = _DEFAULT_APP_CFG.bottom_unusable_pct
red_line_threshold = _DEFAULT_APP_CFG.red_line_threshold


# ---------------------------------------------------------------------------
# Excel extraction
# ---------------------------------------------------------------------------

EXPECTED_SLOTS = 140
FIRST_ROW = 4
LAST_ROW = FIRST_ROW + EXPECTED_SLOTS - 1  # 143

POWER_CELL = "C3"
TIME_COL = "B"
PRICE_COL = "C"


@dataclass
class ExcelInputs:
    station_power_mw: float
    prices: list[float]
    time_labels: list[str]


def read_inputs_from_excel(
    filepath: str,
    sheet_name: str,
    power_unit: str,
) -> ExcelInputs:
    # data_only=True to get the last-cached *values* of formula cells
    # (C4:C143 are `=SUM(NordPool!...)` formulas in the example file).
    wb = load_workbook(filepath, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise ValueError(
            f"Sheet '{sheet_name}' not found. Available sheets: {wb.sheetnames}"
        )
    ws = wb[sheet_name]

    raw_power = ws[POWER_CELL].value
    if raw_power is None:
        raise ValueError(f"{POWER_CELL} (station power) is empty in sheet '{sheet_name}'.")
    station_power_mw = float(raw_power) / 1000.0 if power_unit == "kw" else float(raw_power)

    prices: list[float] = []
    time_labels: list[str] = []
    for row in range(FIRST_ROW, LAST_ROW + 1):
        price_val = ws[f"{PRICE_COL}{row}"].value
        time_val = ws[f"{TIME_COL}{row}"].value
        if price_val is None:
            raise ValueError(
                f"Missing price at {PRICE_COL}{row}. Expected {EXPECTED_SLOTS} "
                f"contiguous values in {PRICE_COL}{FIRST_ROW}:{PRICE_COL}{LAST_ROW}."
            )
        prices.append(float(price_val))
        time_labels.append(str(time_val))

    if len(prices) != EXPECTED_SLOTS:
        raise ValueError(f"Expected {EXPECTED_SLOTS} prices, got {len(prices)}.")

    return ExcelInputs(station_power_mw=station_power_mw, prices=prices, time_labels=time_labels)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_schedule_csv(path: str, result, prices: list[float], time_labels: list[str]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["slot_index", "time_label", "price_eur_mwh", "action", "power_sold_mw",
             "energy_charged_mwh", "energy_discharged_mwh", "battery_level_end_mwh", "revenue_eur"]
        )
        for i, r in enumerate(result.schedule):
            writer.writerow([
                r.slot_index,
                time_labels[i] if i < len(time_labels) else "",
                prices[r.slot_index] if r.slot_index < len(prices) else "",
                r.action.value,
                f"{r.power_sold:.4f}",
                f"{r.energy_charged:.4f}",
                f"{r.energy_discharged:.4f}",
                f"{r.battery_level_end:.4f}",
                f"{r.revenue:.4f}",
            ])


# ---------------------------------------------------------------------------
# Per-file processing (shared by single-file and batch mode)
# ---------------------------------------------------------------------------

def process_one_file(
    excel_path: str,
    sheet: str,
    power_unit: str,
    battery_pct: float,
    initial_charge_price: float,
    threshold: float,
    template: str | None,
    csv_out: str | None,
    xlsx_out: str | None,
) -> None:
    # Reload config fresh on every run so a change saved from the GUI's
    # config window takes effect on the very next run, without needing to
    # restart the app.
    app_cfg = load_config()
    cfg = app_cfg.station_config()

    print(f"Reading inputs from '{excel_path}' (sheet '{sheet}')...")
    inputs = read_inputs_from_excel(excel_path, sheet, power_unit)
    print(f"  Station power : {inputs.station_power_mw:.4f} MW "
          f"(raw C3 value {'/1000' if power_unit == 'kw' else ''})")
    print(f"  Prices        : {len(inputs.prices)} slots, "
          f"{inputs.prices[0]:.2f}..{inputs.prices[-1]:.2f} EUR/MWh")

    battery_level = calculate_usable_battery_capacity(battery_pct, app_cfg)
    state = StationState(
        station_power=inputs.station_power_mw,
        battery_level=battery_level,
        initial_charge_price=initial_charge_price,
    )
    print(f"  Battery level : {battery_level:.4f} MWh (from {battery_pct:.1f}% total charge)")

    print("Running optimiser...")
    result = optimise_battery_schedule(
        prices=inputs.prices,
        cfg=cfg,
        state=state,
        start_slot=0,
    )

    print_schedule(result, inputs.prices)

    if csv_out:
        write_schedule_csv(csv_out, result, inputs.prices, inputs.time_labels)
        print(f"Schedule written to {csv_out}")

    if xlsx_out:
        generate_formatted_excel(
            template_path=template,
            output_path=xlsx_out,
            schedule=result.schedule,
            prices=inputs.prices,
            threshold=threshold,
            color_map=build_color_map(app_cfg),
        )
        print(f"Formatted Excel written to {xlsx_out}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "excel_path", nargs="?", default=None,
        help="Path to a single input .xlsx file. Omit this and use --input-dir instead "
             "to process a whole folder of files.",
    )
    parser.add_argument(
        "--input-dir", default=None,
        help="Batch mode: process every .xlsx file in this folder (instead of a single "
             "excel_path). The template file, if it lives in this folder, is skipped "
             "automatically.",
    )
    parser.add_argument(
        "--output-dir", default="output",
        help="Batch mode: folder to write outputs into (created if missing). "
             "Each input file 'foo.xlsx' produces 'foo_schedule.csv' and/or "
             "'foo_output.xlsx' here. Default: 'output'.",
    )
    parser.add_argument("--sheet", default="BESS (15)", help="Sheet name to read (default: 'BESS (15)')")
    parser.add_argument(
        "--power-unit", choices=["kw", "mw"], default="kw",
        help="Unit of the value in C3. Default 'kw' (divided by 1000 to get MW). "
             "Use 'mw' if your sheet already stores station power in MW.",
    )
    parser.add_argument(
        "--battery-pct", type=float, default=12.0,
        help="Starting total battery charge %% (same default as calculator.py's main entry point: 12).",
    )
    parser.add_argument(
        "--initial-charge-price", type=float, default=0.0,
        help="Average price the currently-stored energy was charged at (EUR/MWh). Default 0.0 (unknown/most permissive).",
    )
    parser.add_argument(
        "--out", default=None,
        help="Single-file mode only: path to write the schedule as CSV. "
             "(In batch mode, CSV filenames are generated automatically.)",
    )
    parser.add_argument(
        "--xlsx-out", default=None,
        help="Single-file mode only: path to write a formatted .xlsx matching calculator.py's "
             "output. (In batch mode, xlsx filenames are generated automatically whenever "
             "--template is provided.)",
    )
    parser.add_argument(
        "--template", default="excel_template.xlsx",
        help="Path to the real excel_template.xlsx that calculator.py duplicates. "
             "In single-file mode this is only used if --xlsx-out is given. In batch mode, "
             "providing this enables xlsx output for every processed file. "
             "Default: 'excel_template.xlsx' in the cwd.",
    )
    parser.add_argument(
        "--threshold", type=float, default=red_line_threshold,
        help=f"Price threshold (EUR/MWh) below which chart labels are colored red (default {red_line_threshold}).",
    )
    args = parser.parse_args()

    if args.input_dir:
        # ------------------------------------------------------------
        # Batch mode: every .xlsx in --input-dir becomes its own
        # schedule.csv (+ formatted .xlsx if --template resolves)
        # ------------------------------------------------------------
        input_dir = args.input_dir
        output_dir = args.output_dir
        os.makedirs(output_dir, exist_ok=True)

        template_basename = os.path.basename(args.template) if args.template else None
        make_xlsx = bool(args.template) and os.path.isfile(args.template)
        if args.template and not make_xlsx:
            print(f"Note: template '{args.template}' not found — xlsx output will be skipped "
                  f"for all files (CSV output still generated).")

        candidates = sorted(
            f for f in os.listdir(input_dir)
            if f.lower().endswith((".xlsx", ".xlsm")) and not f.startswith("~$")
        )
        # Don't try to process the template itself if it happens to live in input_dir.
        if template_basename:
            candidates = [f for f in candidates if f != template_basename]

        if not candidates:
            print(f"No .xlsx files found in '{input_dir}'.")
            return

        print(f"Found {len(candidates)} file(s) in '{input_dir}':")
        for f in candidates:
            print(f"  - {f}")
        print()

        succeeded, failed = [], []
        for fname in candidates:
            stem = os.path.splitext(fname)[0]
            in_path = os.path.join(input_dir, fname)
            csv_out = os.path.join(output_dir, f"{stem}_schedule.csv")
            xlsx_out = os.path.join(output_dir, f"{stem}_output.xlsx") if make_xlsx else None

            print("=" * 78)
            print(f"Processing '{fname}'")
            print("=" * 78)
            try:
                process_one_file(
                    excel_path=in_path,
                    sheet=args.sheet,
                    power_unit=args.power_unit,
                    battery_pct=args.battery_pct,
                    initial_charge_price=args.initial_charge_price,
                    threshold=args.threshold,
                    template=args.template if make_xlsx else None,
                    csv_out=csv_out,
                    xlsx_out=xlsx_out,
                )
                succeeded.append(fname)
            except Exception as e:
                print(f"FAILED on '{fname}': {e}")
                failed.append((fname, str(e)))
            print()

        print("=" * 78)
        print(f"Batch complete: {len(succeeded)} succeeded, {len(failed)} failed.")
        if failed:
            print("Failures:")
            for fname, err in failed:
                print(f"  - {fname}: {err}")
        print(f"Outputs written to '{output_dir}'.")

    else:
        # ------------------------------------------------------------
        # Single-file mode (original behaviour)
        # ------------------------------------------------------------
        if not args.excel_path:
            parser.error("excel_path is required unless --input-dir is given.")

        process_one_file(
            excel_path=args.excel_path,
            sheet=args.sheet,
            power_unit=args.power_unit,
            battery_pct=args.battery_pct,
            initial_charge_price=args.initial_charge_price,
            threshold=args.threshold,
            template=args.template,
            csv_out=args.out,
            xlsx_out=args.xlsx_out,
        )


if __name__ == "__main__":
    main()