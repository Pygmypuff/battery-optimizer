from pathlib import Path
from datetime import datetime, date
from nordpool import elspot
import pytz
import bisect
import os

from app_paths import bundle_dir, output_dir as default_output_dir
from battery_optimizer import (
    StationConfig,
    StationState,
    compute_charge_discharge_ratio,
    rerun_for_remaining_day,
)
from excel_output import build_color_map, generate_formatted_excel
from station_config import calculate_usable_battery_capacity, load_config

# Config values (StationConfig fields, total_battery_capacity,
# bottom_unusable_pct, red_line_threshold) now live in station_config.py /
# config.json, editable from the GUI's config window. This module-level
# snapshot is only a fallback default for callers that don't go through
# run_nordpool() (e.g. direct StationConfig-needing calls); run_nordpool()
# itself always reloads fresh so GUI-saved changes apply on the next run
# without restarting the app.
_DEFAULT_APP_CFG = load_config()
BASE_CFG = _DEFAULT_APP_CFG.station_config()

# OTHER CONFIG VALUES
total_battery_capacity = _DEFAULT_APP_CFG.total_battery_capacity
bottom_unusable_pct = _DEFAULT_APP_CFG.bottom_unusable_pct
top_unusable_pct = 5 # 5% of battery capacity is unusable (top) — not currently used below
red_line_threshold = _DEFAULT_APP_CFG.red_line_threshold

# Where excel_template.xlsx (a bundled, read-only resource) lives. Uses
# bundle_dir() rather than raw __file__ so this still resolves correctly
# once frozen by PyInstaller — see app_paths.py.
SCRIPT_DIR = str(bundle_dir())


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


def battery_optimizer_run(prices: list[float], state: StationState, slots_elapsed: int, cfg: StationConfig):
    """
    Run the battery optimizer and return the result, starting from the given state and slot index.
    """
    print("=" * 78)
    print(f"  P = {state.station_power:.4f} MW  |  "
          f"Charge:discharge ratio X (informational only, not a solver "
          f"constraint) = {compute_charge_discharge_ratio(state.station_power, cfg):.4f}  |  "
          f"Battery = {state.battery_level:.4f} MWh")
    print("=" * 78)

    result = rerun_for_remaining_day(
            remaining_prices = prices[slots_elapsed:],
            cfg              = cfg,
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



# ── Live (Nordpool) run ──────────────────────────────────────────────────────

def _run_nordpool_from(
    target: datetime,
    station_power: float,
    battery_pct: float,
    initial_charge_price: float,
    output_dir: str | Path | None,
) -> Path:
    """
    Shared implementation for run_nordpool() (from 14:00) and
    run_nordpool_from_now() (from the current moment): fetch live Nordpool
    prices, run the battery optimizer starting at `target`, and write a
    formatted output workbook. Returns the path to the generated .xlsx.

    Progress is reported via plain `print()` calls, same as the rest of
    this module — callers that want to capture it (e.g. a GUI) can redirect
    stdout around the call.
    """
    # Reload config fresh on every run so a change saved from the GUI's
    # config window takes effect on the very next run, without needing to
    # restart the app.
    app_cfg = load_config()
    cfg = app_cfg.station_config()

    # Defaults to the OS-managed per-user output folder (see app_paths.py),
    # not SCRIPT_DIR — once packaged, SCRIPT_DIR is inside the app bundle,
    # and generated workbooks would be lost every time the app updates.
    out_dir = Path(output_dir) if output_dir is not None else default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching prices from nordpool...")
    prices_list = fetch_prices()
    print("=" * 78)
    print(f"Fetched {len(prices_list['price'])} electricity prices")

    slot_index = datetime_to_slot_index(target, prices_list["datetime"])
    print(f"Running battery optimizer from {target} (slot index {slot_index})...")

    result = battery_optimizer_run(
        prices=prices_list["price"],
        state=StationState(
            station_power=station_power,
            battery_level=calculate_usable_battery_capacity(battery_pct, app_cfg),
            initial_charge_price=initial_charge_price,
        ),
        slots_elapsed=slot_index,
        cfg=cfg,
    )
    print("Battery optimizer run complete.")
    print(f"  BATTERY SCHEDULE  —  {result.slots_optimised} slots  |  "
          f"Expected revenue: {result.total_revenue:.2f} EUR")
    print("=" * 78)

    src_filepath = os.path.join(SCRIPT_DIR, "excel_template.xlsx")
    timestamp = datetime.now(target.tzinfo).strftime("%Y%m%d_%H%M%S")
    dst_filepath = str(out_dir / f"output_{timestamp}.xlsx")

    generate_formatted_excel(
        template_path=src_filepath,
        output_path=dst_filepath,
        schedule=result.schedule,
        prices=prices_list["price"],
        threshold=app_cfg.red_line_threshold,
        color_map=build_color_map(app_cfg),
        start_index=slot_index,
    )

    print(f"Done. Output saved to {dst_filepath}")
    return Path(dst_filepath)


def run_nordpool(
    station_power: float,
    battery_pct: float = 12.0,
    initial_charge_price: float = 0.0,
    output_dir: str | Path | None = None,
) -> Path:
    """Run the optimizer for the rest of today, starting at 14:00 today."""
    riga_tz = pytz.timezone('Europe/Riga')
    now = datetime.now(riga_tz)
    target = now.replace(hour=14, minute=0, second=0, microsecond=0)  # 14:00 today
    return _run_nordpool_from(target, station_power, battery_pct, initial_charge_price, output_dir)


def run_nordpool_from_now(
    station_power: float,
    battery_pct: float = 12.0,
    initial_charge_price: float = 0.0,
    output_dir: str | Path | None = None,
) -> Path:
    """
    Run the optimizer for the rest of today, starting right now — a mid-day
    rerun, e.g. after the station's actual power output has drifted from
    what was assumed at the last 14:00 run.
    """
    riga_tz = pytz.timezone('Europe/Riga')
    now = datetime.now(riga_tz)
    return _run_nordpool_from(now, station_power, battery_pct, initial_charge_price, output_dir)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_nordpool(station_power=0.310)
