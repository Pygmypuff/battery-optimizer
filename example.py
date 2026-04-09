"""
example.py
==========
Demonstrates the battery optimiser with a simulated 24-hour price curve
(EUR/MWh) that has a morning peak (~08:00) and a larger evening peak (~19:00).

All power values are in MW, energy in MWh, currency in EUR.

Run:
    python example.py

Scenarios
---------
  1. Full-day optimisation from 00:00 with normal station conditions.
  2. Mid-day rerun at 10:00 (slot 40) — station_power changes, ratio is
     recomputed automatically; scheduling budget resets to 0.
  3. Overflow demo — station_power > max_charge_rate so charge slots still
     produce overflow revenue.
  4. Discharge-loss demo — shows that the 5 % loss reduces effective sold
     power compared to a lossless run.
"""

import math
import random

from battery_optimizer import (
    BatteryAction,
    StationConfig,
    StationState,
    compute_charge_discharge_ratio,
    optimise_battery_schedule,
    print_schedule,
    rerun_for_remaining_day,
)


# ---------------------------------------------------------------------------
# Shared hardware config (used by all scenarios unless overridden)
# ---------------------------------------------------------------------------

BASE_CFG = StationConfig(
    max_charge_rate     = 0.050,   # C = 50 MW   (0.050 in MW)
    max_sell_rate       = 0.120,   # S = 120 MW  (0.120 in MW)
    battery_capacity    = 0.100,   # B = 100 MWh (0.100 in MWh)
    min_price_delta     = 20.0,    # Y = 20 EUR/MWh spread required
    min_discharge_price = 60.0,    # T = never discharge below 60 EUR/MWh
    discharge_loss_pct  = 5.0,     # 5 % of drawn energy is lost on discharge
)


# ---------------------------------------------------------------------------
# Simulated price curve (EUR/MWh)
# ---------------------------------------------------------------------------

def make_example_prices(seed: int = 42) -> list[float]:
    """
    Generate 96 synthetic prices (EUR/MWh) with two daily peaks and light
    noise.  Morning peak ~08:00, evening peak ~19:00.
    Values are scaled to realistic day-ahead market levels (30–150 EUR/MWh).
    """
    rng = random.Random(seed)
    prices = []
    for slot in range(96):
        hour         = slot / 4
        morning_peak = 40.0 * math.exp(-0.5 * ((hour - 8)  / 2) ** 2)
        evening_peak = 70.0 * math.exp(-0.5 * ((hour - 19) / 2) ** 2)
        noise        = rng.gauss(0, 3.0)
        prices.append(max(20.0, 30.0 + morning_peak + evening_peak + noise))
    return prices


# ---------------------------------------------------------------------------
# Scenario 1 – full-day run from midnight
# ---------------------------------------------------------------------------

def run_full_day(prices: list[float]):
    state = StationState(
        station_power = 0.080,   # P = 80 MW
        battery_level = 0.010,   # starting with 10 MWh stored
    )

    ratio = compute_charge_discharge_ratio(state.station_power, BASE_CFG)

    print("=" * 78)
    print("  SCENARIO 1 — Full-day optimisation from 00:00")
    print(f"  P = {state.station_power*1000:.0f} MW  |  "
          f"Computed ratio X = {ratio:.4f}  |  "
          f"Battery = {state.battery_level*1000:.0f} MWh")
    print(f"  min_price_delta = {BASE_CFG.min_price_delta} EUR/MWh  |  "
          f"min_discharge_price = {BASE_CFG.min_discharge_price} EUR/MWh  |  "
          f"discharge_loss = {BASE_CFG.discharge_loss_pct}%")
    print("=" * 78)

    result = optimise_battery_schedule(prices=prices, cfg=BASE_CFG, state=state)
    print_schedule(result, prices)
    return result


# ---------------------------------------------------------------------------
# Scenario 2 – mid-day rerun at slot 40 (10:00)
# ---------------------------------------------------------------------------

def run_midday_rerun(prices: list[float], full_day_result):
    slots_elapsed         = 40
    measured_battery_level = full_day_result.schedule[slots_elapsed - 1].battery_level_end

    updated_state = StationState(
        station_power = 0.070,                  # P dropped to 70 MW
        battery_level = measured_battery_level,  # only physical carry-over
    )

    new_ratio = compute_charge_discharge_ratio(updated_state.station_power, BASE_CFG)

    print("=" * 78)
    print(f"  SCENARIO 2 — Mid-day rerun from slot {slots_elapsed} (10:00)")
    print(f"  Updated P = {updated_state.station_power*1000:.0f} MW  |  "
          f"Recomputed ratio X = {new_ratio:.4f}  |  "
          f"Measured battery = {measured_battery_level*1000:.2f} MWh")
    print("  Scheduling budget resets to 0 — new X applies from this point only.")
    print("=" * 78)

    result = rerun_for_remaining_day(
        remaining_prices = prices[slots_elapsed:],
        cfg              = BASE_CFG,
        updated_state    = updated_state,
        slots_elapsed    = slots_elapsed,
    )
    print_schedule(result, prices)


# ---------------------------------------------------------------------------
# Scenario 3 – overflow: station_power > max_charge_rate
# ---------------------------------------------------------------------------

def run_overflow_example():
    """
    P = 90 MW > C = 50 MW.
    During CHARGE slots the battery absorbs 50 MW; the remaining 40 MW is
    sold as overflow.  Expect power_sold = 0.040 MW on CHARGE rows.
    """
    prices = [30.0] * 32 + [130.0] * 8 + [35.0] * 56   # cheap overnight, spike 08:00

    cfg = StationConfig(
        max_charge_rate     = 0.050,
        max_sell_rate       = 0.150,
        battery_capacity    = 0.080,
        min_price_delta     = 30.0,
        min_discharge_price = 80.0,
        discharge_loss_pct  = 5.0,
    )

    state = StationState(
        station_power = 0.090,   # P > C → overflow = 40 MW sold during charging
        battery_level = 0.000,
    )

    ratio = compute_charge_discharge_ratio(state.station_power, cfg)

    print("=" * 78)
    print("  SCENARIO 3 — Overflow: P (90 MW) > max_charge_rate (50 MW)")
    print(f"  Computed ratio X = {ratio:.4f}")
    print("  CHARGE slots should show power_sold = 0.0400 MW (overflow).")
    print("=" * 78)

    result = optimise_battery_schedule(prices=prices, cfg=cfg, state=state)
    print_schedule(result, prices)


# ---------------------------------------------------------------------------
# Scenario 4 – discharge loss comparison
# ---------------------------------------------------------------------------

def run_discharge_loss_comparison(prices: list[float]):
    """
    Compare a lossless run (0 %) vs a 10 % loss run on the same price window.
    The lossless run should produce higher total revenue.
    """
    state = StationState(station_power=0.080, battery_level=0.000)

    for loss_pct in (0.0, 10.0):
        cfg = StationConfig(
            max_charge_rate     = 0.050,
            max_sell_rate       = 0.120,
            battery_capacity    = 0.100,
            min_price_delta     = 20.0,
            min_discharge_price = 60.0,
            discharge_loss_pct  = loss_pct,
        )
        ratio = compute_charge_discharge_ratio(state.station_power, cfg)
        print("=" * 78)
        print(f"  SCENARIO 4 — Discharge loss = {loss_pct:.0f}%  |  "
              f"Ratio X = {ratio:.4f}")
        print("=" * 78)
        result = optimise_battery_schedule(prices=prices, cfg=cfg, state=state)
        print_schedule(result, prices)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    prices = make_example_prices()

    full_day_result = run_full_day(prices)
    run_midday_rerun(prices, full_day_result)
    run_overflow_example()
    run_discharge_loss_comparison(prices)