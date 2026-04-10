import copy
from dataclasses import dataclass
from enum import Enum
from openpyxl import load_workbook, Workbook
from openpyxl.drawing.colors import ColorChoice, SchemeColor
import pandas as pd
from pathlib import Path
from datetime import time
import math

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
    BatteryAction.CHARGE:    "4472C4",  # blue
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


def time_to_slot(t: time) -> int:
    """
    Return the slot index that should be used as the starting slot for a
    rerun triggered at time *t*.

    Rules
    -----
    - If *t* falls exactly on a 15-minute boundary, return that slot's index.
    - If *t* falls between boundaries, return the index of the next slot
      (i.e. the first slot that has not yet started).
    """
    SLOTS_PER_DAY:      int   = 96
    SLOT_DURATION_MINS: int   = 15

    total_minutes = t.hour * 60 + t.minute + t.second / 60 + t.microsecond / 6e7
    slot = total_minutes / SLOT_DURATION_MINS
    return math.ceil(slot) % SLOTS_PER_DAY

def battery_optimizer_run(prices: list[float], state: StationState, slots_elapsed: int):

    print("=" * 78)
    print("  Run optimisation from 00:00")
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