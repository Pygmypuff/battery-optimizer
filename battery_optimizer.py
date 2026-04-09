"""
battery_optimizer.py
====================
Determines the optimal battery action (CHARGE / DISCHARGE / HOLD) for each
15-minute slot in the upcoming planning window in order to maximise revenue.

Units
-----
  Power     : MW
  Energy    : MWh  (1 slot = 0.25 h, so 1 MW for one slot = 0.25 MWh)
  Price     : EUR / MWh
  Revenue   : EUR

Key design rules
----------------
  • The charge/discharge ratio X is derived automatically from station_power
    and the hardware config.  It is never supplied manually.
  • The scheduling_budget is a within-run-only counter that resets to 0 on
    every call (including reruns).  It enforces the X constraint going forward.
  • battery_level (MWh) is the sole physical value carried into a rerun.
  • Each discharge incurs a configurable percentage energy loss.
  • Two independent price-based discharge guards:
      - min_price_delta      (Y): spread between the cheapest prior charge
                                  price and the current discharge price must
                                  be at least this large.
      - min_discharge_price  (T): absolute floor — never discharge when the
                                  spot price is below this threshold regardless
                                  of the spread.

Public API
----------
    compute_charge_discharge_ratio(station_power, cfg) -> float
    optimise_battery_schedule(prices, cfg, state, start_slot) -> OptimisationResult
    rerun_for_remaining_day(remaining_prices, cfg, updated_state, slots_elapsed)
        -> OptimisationResult
    print_schedule(result, all_prices) -> None
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLOT_DURATION_HOURS: float = 0.25   # 15 minutes expressed in hours
_BATTERY_RESOLUTION: int   = 1_000  # discretisation steps per MWh unit
                                     # 1 000 → 0.001 MWh (1 kWh) precision


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class BatteryAction(str, Enum):
    CHARGE    = "CHARGE"
    DISCHARGE = "DISCHARGE"
    HOLD      = "HOLD"


@dataclass(frozen=True)
class StationConfig:
    """
    Static hardware limits and economic thresholds for the power station.
    All values are fixed for the lifetime of the station and do not change
    between optimisation runs.

    Attributes
    ----------
    max_charge_rate      : C – maximum rate at which the battery can absorb
                           or release energy (MW).
    max_sell_rate        : S – hard cap on power the station may sell to the
                           grid at any moment (MW).
    battery_capacity     : B – total usable energy the battery can store (MWh).
    min_price_delta      : Y – minimum price spread (EUR/MWh) between the
                           cheapest prior charge slot and the current discharge
                           slot for battery cycling to be economically
                           justified after accounting for battery wear.
    min_discharge_price  : T – absolute price floor (EUR/MWh).  Discharging
                           is never allowed when the spot price is below this
                           value, regardless of the spread.  Must be >= Y.
    discharge_loss_pct   : percentage of stored energy lost during each
                           discharge event (0–100).  For example, 5.0 means
                           that for every 1 MWh drawn from the battery only
                           0.95 MWh is delivered to the grid.
    """
    max_charge_rate:     float   = 0.4 # C   (MW)
    max_sell_rate:       float   = 0.5 # S   (MW)
    battery_capacity:    float   = 0.77353 # B   (MWh)
    min_price_delta:     float   = 40 # Y   (EUR/MWh)
    min_discharge_price: float   = 52.5 # T   (EUR/MWh)
    discharge_loss_pct:  float   = 2 # 0–100 (%)

    def __post_init__(self) -> None:
        if self.discharge_loss_pct < 0 or self.discharge_loss_pct >= 100:
            raise ValueError("discharge_loss_pct must be in [0, 100)")
        if self.min_discharge_price < self.min_price_delta:
            raise ValueError(
                "min_discharge_price should be >= min_price_delta "
                "(the absolute floor should be at least as large as the spread requirement)"
            )

    @property
    def discharge_efficiency(self) -> float:
        """Fraction of drawn energy that actually reaches the grid (0–1]."""
        return 1.0 - self.discharge_loss_pct / 100.0


@dataclass(frozen=True)
class StationState:
    """
    Dynamic snapshot of the station at the moment the optimiser is called.

    On every call (including reruns) this is built from fresh measurements.
    The charge_discharge_ratio is computed automatically via
    compute_charge_discharge_ratio() and does not need to be supplied.

    Attributes
    ----------
    station_power : P – current generation output (MW).
    battery_level : measured stored energy right now (MWh).
                    This is the ONLY value carried into a rerun — it is a
                    physical measurement, not a scheduling artefact.
    """
    station_power: float   # P  (MW)
    battery_level: float   # MWh, must satisfy 0 <= level <= battery_capacity


# ---------------------------------------------------------------------------
# Charge/discharge ratio calculation
# ---------------------------------------------------------------------------

def compute_charge_discharge_ratio(
    station_power: float,
    cfg:           StationConfig,
) -> float:
    """
    Derive the charge/discharge ratio X from the current station output P
    and the hardware configuration.

    Definition of X
    ---------------
    X = (charge slots to fill battery from empty)
      / (discharge slots to drain battery from full)

    X > 1  →  charging is slow relative to discharging (need more charge slots)
    X < 1  →  charging is fast (each charge slot earns more than one discharge)
    X = 1  →  symmetric

    Derivation
    ----------
    Charge slots to full battery:
        charge_slots = B / (P * t)
        (station output P all goes into the battery at rate P MW)

    Discharge slots from full battery:
        Case A — P < S - C:
            The battery can discharge at the full max_charge_rate C, because
            the station's own output P leaves at least C MW of headroom below
            the sell cap S.  Discharge rate = C MW from battery.
            discharge_slots = B / (C * t)

        Case B — P >= S - C:
            The headroom below S is only (S - P) MW, so the battery can only
            drain at (S - P) MW.
            discharge_slots = B / ((S - P) * t)

    Parameters
    ----------
    station_power : current station output P (MW).
    cfg           : hardware configuration.

    Returns
    -------
    X (dimensionless, > 0).  Returns inf if the station is producing at or
    above max_sell_rate (no room to discharge).
    """
    t = SLOT_DURATION_HOURS
    B = cfg.battery_capacity
    C = cfg.max_charge_rate
    S = cfg.max_sell_rate
    P = station_power

    # --- slots needed to fully charge from empty ---
    if P <= 0:
        return math.inf  # no generation → charging impossible
    charge_slots = B / (P * t)

    # --- slots available to fully discharge from full ---
    if P < S - C:
        # Case A: full battery discharge rate available
        battery_discharge_rate = C
    else:
        # Case B: headroom below sell cap limits discharge
        battery_discharge_rate = S - P

    if battery_discharge_rate <= 0:
        # P >= S: station already at or above sell cap, no room to discharge
        return math.inf

    discharge_slots = B / (battery_discharge_rate * t)

    return charge_slots / discharge_slots


# ---------------------------------------------------------------------------
# Y and T constraint pre-filter
# ---------------------------------------------------------------------------

def _build_profitable_discharge_mask(
    prices:              list[float],
    min_price_delta:     float,
    min_discharge_price: float,
) -> list[bool]:
    """
    For each slot j return True iff ALL of the following hold:

      1. prices[j] >= min_discharge_price          (T constraint — absolute floor)
      2. prices[j] - min_price_seen_before_j >= min_price_delta
                                                   (Y constraint — spread vs cheapest charge)

    Pre-computed in O(N) using a running minimum of prices seen so far.
    """
    n = len(prices)
    mask = [False] * n
    min_price_seen = math.inf

    for j in range(n):
        if j > 0:
            min_price_seen = min(min_price_seen, prices[j - 1])

        passes_floor  = prices[j] >= min_discharge_price
        passes_spread = (prices[j] - min_price_seen) >= min_price_delta

        mask[j] = passes_floor and passes_spread

    return mask


# ---------------------------------------------------------------------------
# Physics layer
# ---------------------------------------------------------------------------

def _slot_physics(
    action:                 BatteryAction,
    battery_level:          float,
    scheduling_budget:      float,
    price:                  float,
    station_power:          float,
    charge_discharge_ratio: float,
    cfg:                    StationConfig,
    profitable_discharge:   bool,
) -> Optional[tuple[float, float, float, float, float]]:
    """
    Compute the physical outcome of attempting *action* in one slot.

    All energy values in MWh, power values in MW, revenue in EUR.

    scheduling_budget
        Within-run counter of discharge-slot equivalents earned (via charging)
        minus those spent (via discharging).  Starts at 0 every run/rerun.
        Never transferred between runs.

        CHARGE    budget += (energy_charged / full_charge_step) / X
        DISCHARGE budget -= (energy_discharged / full_discharge_step)
        HOLD      budget unchanged

    Returns
    -------
    (slot_revenue, energy_charged, energy_discharged,
     new_battery_level, new_scheduling_budget)
    or None if the action is physically or economically infeasible.

    Physics summary
    ---------------
    CHARGE
      • Battery absorbs min(C, P) MW, clamped to remaining headroom (MWh).
        Partial fills are always allowed.
      • Overflow power max(0, P - C) is sold to the grid regardless.
      • Revenue = overflow_power * price * t   (EUR)

    DISCHARGE
      • Station sells its full output P; battery supplements up to S.
      • Battery draw MW = min(C, S - P) when P < S - C, else (S - P).
        Clamped to available stored energy.  Partial discharges are allowed.
      • discharge_loss_pct % of the drawn energy is lost; only the remainder
        is delivered to the grid and counted as sold.
      • Blocked when: profitable_discharge is False (Y or T constraint),
        scheduling_budget is insufficient (X constraint), or battery empty.
      • Revenue = min(S, P + energy_delivered / t) * price * t   (EUR)

    HOLD
      • Station output P sold directly; battery untouched.
      • Revenue = P * price * t   (EUR)
    """
    t   = SLOT_DURATION_HOURS
    eff = cfg.discharge_efficiency

    effective_charge_rate    = min(cfg.max_charge_rate, station_power)
    effective_discharge_rate = max(0.0, cfg.max_sell_rate - station_power)
    overflow_power           = max(0.0, station_power - cfg.max_charge_rate)

    # ------------------------------------------------------------------
    if action is BatteryAction.HOLD:
        revenue = price * station_power * t
        return revenue, 0.0, 0.0, battery_level, scheduling_budget

    # ------------------------------------------------------------------
    if action is BatteryAction.CHARGE:
        headroom  = cfg.battery_capacity - battery_level
        energy_in = min(effective_charge_rate * t, headroom)

        if energy_in <= 1e-9:
            return None  # battery already full

        full_charge_step      = effective_charge_rate * t
        budget_earned         = (energy_in / full_charge_step) / charge_discharge_ratio

        new_battery_level     = battery_level + energy_in
        new_scheduling_budget = scheduling_budget + budget_earned
        revenue               = price * overflow_power * t

        return revenue, energy_in, 0.0, new_battery_level, new_scheduling_budget

    # ------------------------------------------------------------------
    if action is BatteryAction.DISCHARGE:
        if not profitable_discharge:
            return None  # T or Y constraint: price too low or spread too small

        energy_drawn = min(effective_discharge_rate * t, battery_level)

        if energy_drawn <= 1e-9:
            return None  # battery empty or P already at/above sell cap

        full_discharge_step   = effective_discharge_rate * t
        budget_needed         = energy_drawn / full_discharge_step

        if scheduling_budget < budget_needed - 1e-9:
            return None  # X constraint: not enough charging done yet this run

        # Apply discharge loss: only a fraction of drawn energy reaches the grid
        energy_delivered      = energy_drawn * eff

        new_battery_level     = battery_level - energy_drawn
        new_scheduling_budget = scheduling_budget - budget_needed

        # Power delivered to grid this slot (MW equivalent)
        power_delivered       = energy_delivered / t
        sold_power            = min(cfg.max_sell_rate, station_power + power_delivered)
        revenue               = price * sold_power * t

        return revenue, 0.0, energy_drawn, new_battery_level, new_scheduling_budget

    raise ValueError(f"Unknown action: {action}")


# ---------------------------------------------------------------------------
# Discretisation helpers
# ---------------------------------------------------------------------------

def _to_key(value: float) -> int:
    """Map a continuous MWh value to a hashable integer key."""
    return round(value * _BATTERY_RESOLUTION)


def _from_key(key: int) -> float:
    """Inverse of _to_key."""
    return key / _BATTERY_RESOLUTION


# ---------------------------------------------------------------------------
# Dynamic programming optimiser
# ---------------------------------------------------------------------------

def optimise_battery_schedule(
    prices:     list[float],
    cfg:        StationConfig,
    state:      StationState,
    start_slot: int = 0,
) -> OptimisationResult:
    """
    Compute the globally optimal battery schedule for the given price window.

    The charge/discharge ratio X is derived internally from state.station_power
    and cfg.  The scheduling_budget always starts at 0.

    DP state: (slot_index, battery_level_key, scheduling_budget_key)

    Parameters
    ----------
    prices     : electricity price (EUR/MWh) for each remaining 15-minute slot.
                 96 entries for a full day; fewer for a mid-day rerun.
    cfg        : static hardware configuration.
    state      : current station snapshot (station_power + battery_level).
    start_slot : label offset for SlotResult.slot_index.  Pass 0 for a fresh
                 run; pass slots_elapsed for a mid-day rerun.

    Returns
    -------
    OptimisationResult with the full schedule and total expected revenue (EUR).
    """
    num_slots = len(prices)
    if num_slots == 0:
        return OptimisationResult(schedule=[], total_revenue=0.0, slots_optimised=0)

    ratio = compute_charge_discharge_ratio(state.station_power, cfg)

    profitable_mask = _build_profitable_discharge_mask(
        prices,
        cfg.min_price_delta,
        cfg.min_discharge_price,
    )

    max_budget = num_slots / ratio if ratio > 0 and not math.isinf(ratio) else 0.0

    @lru_cache(maxsize=None)
    def best_from(
        slot:       int,
        batt_key:   int,
        budget_key: int,
    ) -> tuple[float, BatteryAction]:
        """
        Returns (best total revenue reachable from *slot* onwards,
                 optimal action to take at *slot*).
        """
        if slot >= num_slots:
            return 0.0, BatteryAction.HOLD  # terminal sentinel

        batt   = _from_key(batt_key)
        budget = _from_key(budget_key)

        best_revenue = -math.inf
        best_action  = BatteryAction.HOLD

        for action in BatteryAction:
            outcome = _slot_physics(
                action                 = action,
                battery_level          = batt,
                scheduling_budget      = budget,
                price                  = prices[slot],
                station_power          = state.station_power,
                charge_discharge_ratio = ratio,
                cfg                    = cfg,
                profitable_discharge   = profitable_mask[slot],
            )
            if outcome is None:
                continue

            slot_rev, _, _, new_batt, new_budget = outcome

            new_batt   = max(0.0, min(cfg.battery_capacity, new_batt))
            new_budget = max(0.0, min(max_budget, new_budget))

            future_rev, _ = best_from(slot + 1, _to_key(new_batt), _to_key(new_budget))
            total          = slot_rev + future_rev

            if total > best_revenue:
                best_revenue = total
                best_action  = action

        return best_revenue, best_action

    init_batt_key   = _to_key(state.battery_level)
    init_budget_key = _to_key(0.0)   # budget always resets on every run

    total_revenue, _ = best_from(0, init_batt_key, init_budget_key)

    # ------------------------------------------------------------------
    # Reconstruct the schedule by replaying the optimal path forward
    # ------------------------------------------------------------------
    schedule: list[SlotResult] = []
    batt   = state.battery_level
    budget = 0.0
    overflow_power = max(0.0, state.station_power - cfg.max_charge_rate)

    for slot in range(num_slots):
        _, chosen_action = best_from(slot, _to_key(batt), _to_key(budget))

        outcome = _slot_physics(
            action                 = chosen_action,
            battery_level          = batt,
            scheduling_budget      = budget,
            price                  = prices[slot],
            station_power          = state.station_power,
            charge_discharge_ratio = ratio,
            cfg                    = cfg,
            profitable_discharge   = profitable_mask[slot],
        )

        # Fallback: discretisation can rarely cause a tiny feasibility gap
        if outcome is None:
            chosen_action = BatteryAction.HOLD
            outcome = _slot_physics(
                action                 = BatteryAction.HOLD,
                battery_level          = batt,
                scheduling_budget      = budget,
                price                  = prices[slot],
                station_power          = state.station_power,
                charge_discharge_ratio = ratio,
                cfg                    = cfg,
                profitable_discharge   = profitable_mask[slot],
            )

        slot_rev, energy_charged, energy_discharged, new_batt, new_budget = outcome

        # Power sold this slot (MW)
        if chosen_action is BatteryAction.DISCHARGE:
            energy_delivered = energy_discharged * cfg.discharge_efficiency
            sold_power = min(
                cfg.max_sell_rate,
                state.station_power + energy_delivered / SLOT_DURATION_HOURS,
            )
        elif chosen_action is BatteryAction.CHARGE:
            sold_power = overflow_power
        else:
            sold_power = state.station_power

        schedule.append(SlotResult(
            slot_index        = start_slot + slot,
            action            = chosen_action,
            energy_charged    = energy_charged,
            energy_discharged = energy_discharged,
            power_sold        = sold_power,
            revenue           = slot_rev,
            battery_level_end = new_batt,
        ))

        batt   = new_batt
        budget = new_budget

    return OptimisationResult(
        schedule        = schedule,
        total_revenue   = total_revenue,
        slots_optimised = num_slots,
    )


# ---------------------------------------------------------------------------
# Mid-day rerun entry point
# ---------------------------------------------------------------------------

def rerun_for_remaining_day(
    remaining_prices: list[float],
    cfg:              StationConfig,
    updated_state:    StationState,
    slots_elapsed:    int,
) -> OptimisationResult:
    """
    Re-optimise the schedule for the rest of the day after conditions change.

    The charge/discharge ratio is recomputed from updated_state.station_power.
    The scheduling budget resets to 0 — the new efficiency regime applies
    from this point forward with no memory of the previous regime.
    Only updated_state.battery_level carries physical state from the past.

    Parameters
    ----------
    remaining_prices : prices[slots_elapsed:] from the original 24-hour forecast.
    cfg              : unchanged hardware config.
    updated_state    : fresh StationState built from current measurements.
                       battery_level is the only value derived from the past.
    slots_elapsed    : completed slots count (for SlotResult.slot_index labelling).
    """
    return optimise_battery_schedule(
        prices     = remaining_prices,
        cfg        = cfg,
        state      = updated_state,
        start_slot = slots_elapsed,
    )


# ---------------------------------------------------------------------------
# Result types (defined after OptimisationResult deps to avoid forward refs)
# ---------------------------------------------------------------------------

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


@dataclass
class OptimisationResult:
    """Full schedule returned by the optimiser."""
    schedule:        list[SlotResult]
    total_revenue:   float          # EUR
    slots_optimised: int


# ---------------------------------------------------------------------------
# Pretty-printer
# ---------------------------------------------------------------------------

def print_schedule(result: OptimisationResult, all_prices: list[float]) -> None:
    """Print a human-readable summary of the optimised schedule."""
    print(f"\n{'='*78}")
    print(f"  BATTERY SCHEDULE  —  {result.slots_optimised} slots  |  "
          f"Expected revenue: {result.total_revenue:.2f} EUR")
    print(f"{'='*78}")
    print(f"  {'Slot':>4}  {'Time':>5}  {'EUR/MWh':>8}  {'Action':>10}  "
          f"{'Sold MW':>8}  {'Batt MWh':>9}  {'Rev EUR':>9}")
    print(f"  {'-'*70}")

    for r in result.schedule:
        hour   = (r.slot_index * 15) // 60
        minute = (r.slot_index * 15) % 60
        price  = all_prices[r.slot_index] if r.slot_index < len(all_prices) else 0.0

        action_label = {
            BatteryAction.CHARGE:    "⚡ CHARGE",
            BatteryAction.DISCHARGE: "💰 SELL  ",
            BatteryAction.HOLD:      "   HOLD  ",
        }[r.action]

        print(
            f"  {r.slot_index:>4}  {hour:02d}:{minute:02d}  "
            f"  {price:>7.2f}   {action_label}  "
            f"{r.power_sold:>8.4f}  {r.battery_level_end:>9.4f}  "
            f"{r.revenue:>9.4f}"
        )

    print(f"{'='*78}\n")