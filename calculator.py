import copy
from dataclasses import dataclass
from enum import Enum
from openpyxl import load_workbook, Workbook
from openpyxl.drawing.colors import ColorChoice, SchemeColor
from openpyxl.chart.text import RichText
from openpyxl.drawing.text import (
    RichTextProperties, ListStyle, Paragraph,
    ParagraphProperties, CharacterProperties
)
import pandas as pd
from pathlib import Path
from datetime import datetime, time, date
import math
from nordpool import elspot
import pytz
import bisect
import shutil
import os


from battery_optimizer import (
    BatteryAction,
    StationConfig,
    StationState,
    compute_charge_discharge_ratio,
    optimise_battery_schedule,
    print_schedule,
    rerun_for_remaining_day,
)

BASE_CFG = StationConfig(
    max_charge_rate =      0.4, # C   (MW)
    max_sell_rate =        0.5, # S   (MW)
    battery_capacity =     0.77353, # B   (MWh)
    min_price_delta =      40, # Y   (EUR/MWh)
    min_discharge_price =  52.5, # T   (EUR/MWh)
    discharge_loss_pct =   2, # 0–100 (%)
)

# OTHER CONFIG VALUES
total_battery_capacity = 0.932 # MWh (100% battery capacity) (233*4 kWh)
bottom_unusable_pct = 12 # 12% of battery capacity is unusable (bottom)
top_unusable_pct = 5 # 5% of battery capacity is unusable (top)
red_line_threshold = 12.5 # EUR/MWh - price labels below this will be colored red

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Domain types ---

class BatteryAction(str, Enum):
    CHARGE    = "CHARGE"
    DISCHARGE = "DISCHARGE"
    HOLD      = "HOLD"

@dataclass
class SlotResult:
    """Outcome for a single 15-minute slot."""
    slot_index:        int
    action:            BatteryAction
    energy_charged:    float   # MWh added to battery         (0 unless CHARGE)
    energy_discharged: float   # MWh drawn from battery       (0 unless DISCHARGE)
    power_sold:        float   # MW sold to grid this slot
    revenue:           float   # EUR earned this slot
    battery_level_end: float   # MWh in battery at end of slot

COLOR_MAP = {
    BatteryAction.CHARGE:    "4169E1",  # blue
    BatteryAction.DISCHARGE: "ED7D31",  # orange
    BatteryAction.HOLD:      "A6A6A6",  # grey
}

def calculate_usable_battery_capacity(battery_percentage: float) -> float:
    """
    Calculate the usable battery capacity from total battery percentage.
    We are not using the bottom 12% and top 5% of the battery.
    Input: total battery capacity percentage
    Output: usable battery capacity in MWh
    """
    usable_capacity = total_battery_capacity * (battery_percentage - bottom_unusable_pct) / 100
    return usable_capacity


def datetime_to_slot_index(dt: datetime, slots: list[datetime]) -> int:
    """
    Return the index in *slots* of the first datetime >= *dt*.

    - If *dt* matches a datetime in *slots* exactly, return that index.
    - If *dt* falls between two entries, return the index of the next one.
    - Raises ValueError if *dt* is after the last entry in *slots*.
    """
    index = bisect.bisect_left(slots, dt)

    if index >= len(slots):
        raise ValueError(
            f"{dt} is after the last slot ({slots[-1]}); no valid slot exists."
        )

    return index


def battery_optimizer_run(prices: list[float], state: StationState, slots_elapsed: int):
    """
    Run the battery optimizer and return the result, starting from the given state and slot index.
    """
    print("=" * 78)
    print(f"  P = {state.station_power:.4f} MW  |  "
          f"Computed ratio X = {compute_charge_discharge_ratio(state.station_power, BASE_CFG):.4f}  |  "
          f"Battery = {state.battery_level:.4f} MWh")
    print("=" * 78)

    result = rerun_for_remaining_day(
            remaining_prices = prices[slots_elapsed:],
            cfg              = BASE_CFG,
            updated_state    = state,
            slots_elapsed    = slots_elapsed,
        )
    return result


def fetch_prices():
    """
    Fetch electricity prices from nordpool for today and tomorrow,
    Converts prices to Riga timezone and filters out prices before 14:00 today.
    Returns: dictionary with "datetime" and "price" lists.
    throws: ValueError if tomorrow's prices cannot be fetched (e.g. not available yet).
    """
    riga_tz = pytz.timezone('Europe/Riga')
    now = datetime.now(riga_tz)
    target = now.replace(hour=14, minute=0, second=0, microsecond=0) # 14:00 today

    prices_spot = elspot.Prices()
    today_prices = prices_spot.fetch( # get todays prices
        end_date=date.today(),
        areas=["LV"],
        resolution=15,
    )
    tomorrow_prices = prices_spot.fetch( # get tomorrows prices
        areas=["LV"],
        resolution=15,
    )

    # Check if prices were successfully fetched
    if not tomorrow_prices:
        raise ValueError("Failed to fetch tomorrow's prices. Prices might not be available yet.")
    
    prices_list = {
    "datetime": [],
    "price": [],
    }

    # add prices to prices_list, converting timestamps to Riga timezone
    for entry in today_prices["areas"]["LV"]["values"]:
        prices_list["datetime"].append(entry["start"].astimezone(riga_tz))
        prices_list["price"].append(entry["value"])

    for entry in tomorrow_prices["areas"]["LV"]["values"]:
        prices_list["datetime"].append(entry["start"].astimezone(riga_tz))
        prices_list["price"].append(entry["value"])

    # Filter out prices that are before the target time
    prices_list["datetime"], prices_list["price"] = zip(*[
        (dt, price) for dt, price in zip(prices_list["datetime"], prices_list["price"])
        if dt >= target
    ])

    return prices_list


def duplicate_excel(src_path: str, dst_path: str) -> None:
    #duplicate an excel file, preserving all formatting and charts
    shutil.copy2(src_path, dst_path)

def replace_column_c(filepath: str, values: list[float]) -> None:
    """
    Replace values in column C (starting from C2) with the provided list of values.
    """
    if len(values) != 140:
        raise ValueError("Expected 140 values, got {len(values)}")

    wb = load_workbook(filepath)
    ws = wb["tabula"]

    for i, value in enumerate(values):
        ws.cell(row=i + 2, column=3, value=value)

    wb.save(filepath)

def color_chart_bars(filepath: str, slots: list[SlotResult], start_index: int) -> None:
    """
    Colors the bars in the excel chart to match the slot actions.
    """
    if len(slots) + start_index != 140:
        raise ValueError(f"Expected {140 - start_index} SlotResults, got {len(slots)}")

    wb = load_workbook(filepath)
    ws = wb["tabula"]
    chart = ws._charts[0]
    ser = chart.series[0]

    dpt_by_idx = {pt.idx: pt for pt in ser.dPt}

    for i, slot in enumerate(slots):
        bar_index = start_index + i
        pt = dpt_by_idx[bar_index]
        pt.spPr.solidFill.schemeClr = None
        pt.spPr.solidFill.srgbClr = COLOR_MAP[slot.action]

    wb.save(filepath)

"""
Helper functions to create a RichText object with the specified hex color for the text.
"""
def _make_txPr(hex_color: str) -> RichText:
    rPr = CharacterProperties(sz=1000.0, b=True)
    rPr.solidFill = ColorChoice()
    rPr.solidFill.srgbClr = hex_color
    pPr = ParagraphProperties(defRPr=rPr)
    para = Paragraph(pPr=pPr, endParaRPr=CharacterProperties())
    bodyPr = RichTextProperties(rot=-5400000, anchor="ctr", anchorCtr=True)
    return RichText(bodyPr=bodyPr, lstStyle=ListStyle(), p=[para])

def _make_txPr_default() -> RichText:
    solidFill = ColorChoice()
    solidFill.schemeClr = SchemeColor(val="tx1")
    rPr = CharacterProperties(sz=1000.0, b=True)
    rPr.solidFill = solidFill
    pPr = ParagraphProperties(defRPr=rPr)
    para = Paragraph(pPr=pPr, endParaRPr=CharacterProperties())
    bodyPr = RichTextProperties(rot=-5400000, anchor="ctr", anchorCtr=True)
    return RichText(bodyPr=bodyPr, lstStyle=ListStyle(), p=[para])


def color_label_text_below_threshold(
    filepath: str,
    values: list[float],
    threshold: float,
    sheet_name: str = "tabula",
    chart_index: int = 0,
    series_index: int = 0,
) -> None:
    """
    Colors the data labels in the chart red if they are below red-line-threshold.
    """
    if len(values) != 140:
        raise ValueError(f"Expected 140 values, got {len(values)}")

    wb = load_workbook(filepath)
    ws = wb[sheet_name]
    chart = ws._charts[chart_index]
    ser = chart.series[series_index]

    for lbl in ser.dLbls.dLbl:
        lbl.txPr = _make_txPr("FF0000") if values[lbl.idx] < threshold else _make_txPr_default()

    wb.save(filepath)










# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":

    prices_list = fetch_prices()
    print("="*78)
    print(f"Fetched {len(prices_list['price'])} electricity prices")

    riga_tz = pytz.timezone('Europe/Riga')
    now = datetime.now(riga_tz)
    target = now.replace(hour=14, minute=0, second=0, microsecond=0) # 14:00 today

    slot_index = datetime_to_slot_index(target, prices_list["datetime"])
    print(f"Running battery optimizer from {target} (slot index {slot_index})...")   
    
    result = battery_optimizer_run(
        prices = prices_list["price"],
        state = StationState(
            station_power = 0.350,
            battery_level = calculate_usable_battery_capacity(12),
        ),
        slots_elapsed = slot_index,
    )
    print("Battery optimizer run complete.")
    print(f"  BATTERY SCHEDULE  —  {result.slots_optimised} slots  |  "
          f"Expected revenue: {result.total_revenue:.2f} EUR")
    print("="*78)
    
    src_filepath = os.path.join(SCRIPT_DIR, "excel_template.xlsx")
    dst_filepath = os.path.join(SCRIPT_DIR, "output.xlsx")

    print("Duplicating template file...")
    duplicate_excel(src_path=src_filepath, dst_path=dst_filepath)

    print("Replacing prices in output file...")
    replace_column_c(dst_filepath, prices_list["price"])

    print("Coloring chart bars in output file...")
    color_chart_bars(dst_filepath, result.schedule, start_index=slot_index)

    print("Coloring labels below threshold in output file...")
    color_label_text_below_threshold(dst_filepath, prices_list["price"], red_line_threshold)

    print("Done. Output saved to output.xlsx")
