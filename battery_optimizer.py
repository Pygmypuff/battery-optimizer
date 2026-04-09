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

Y-constraint design
-------------------
The min_price_delta (Y) constraint is enforced at every block transition
inside the DP.  A "block" is a consecutive run of CHARGE slots or a
consecutive run of DISCHARGE slots (HOLD slots do not break a block).

The DP carries two extra state dimensions:

  current_block_direction  — NONE / CHARGE / DISCHARGE
      Which direction the current open block is running.

  worst_block_price_key    — discretised price
      The *worst* price seen so far within the current open block:
        • for a CHARGE block: the HIGHEST (most expensive) charge price seen,
          because any future discharge must clear Y above even this worst slot.
        • for a DISCHARGE block: the LOWEST (cheapest) discharge price seen,
          because any future charge must be at least Y below even this worst slot.

At a block transition (e.g. CHARGE → DISCHARGE or DISCHARGE → CHARGE) the
Y spread is checked against worst_block_price_key.  If it fails the action is
blocked.  If it passes, worst_block_price_key resets to the new action's price.

Continuing in the same direction simply updates worst_block_price_key (max for
charge, min for discharge).  No spread check is needed mid-block.

This is provably correct for any number of cycles, any block lengths, and any
charge/discharge ratio, because:
  • It checks the spread against the single most unfavourable price in the
    opposing block — the price that would make the cycle least profitable.
  • No pre-filter is used; the constraint is fully dynamic inside the DP.
  • Arbitrarily long blocks (many charge slots per discharge slot, or vice
    versa) are handled naturally — the worst_block_price just keeps updating.

Pre-existing battery charge
---------------------------
If the battery already contains energy at the start of a run, StationState
accepts an initial_charge_price field.  This is used as the initial
worst_block_price for an open CHARGE block, so the Y constraint is correctly
applied to any early discharge slots relative to when the stored energy was
actually purchased.  If unknown, pass 0.0 (any discharge will be allowed
relative to this reference, which is the most conservative / permissive choice).

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
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLOT_DURATION_HOURS: float = 0.25   # 15 minutes expressed in hours
_BATTERY_RESOLUTION: int   = 1_000  # discretisation steps per MWh unit
                                     # → 0.001 MWh (1 kWh) precision
_PRICE_RESOLUTION:   int   = 10     # discretisation steps per EUR/MWh unit
                                     # → 0.1 EUR/MWh precision for block price

# Ordinals for block direction stored in the DP state tuple
_DIR_NONE      = 0
_DIR_CHARGE    = 1
_DIR_DISCHARGE = 2


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

    Attributes
    ----------
    max_charge_rate      : C – maximum battery charge/discharge rate (MW).
    max_sell_rate        : S – hard cap on power sold to the grid (MW).
    battery_capacity     : B – total usable battery storage (MWh).
    min_price_delta      : Y – minimum price spread (EUR/MWh) required at
                           every block transition.  At a CHARGE→DISCHARGE
                           transition the discharge price must exceed the
                           highest charge price in the preceding charge block
                           by at least Y.  At a DISCHARGE→CHARGE transition
                           the new charge price must be at least Y below the
                           lowest discharge price in the preceding discharge
                           block.
    min_discharge_price  : T – absolute floor (EUR/MWh): never discharge when
                           the spot price is below this value.  Must be >= Y.
    discharge_loss_pct   : percentage of drawn energy lost during discharge
                           (0–100).  E.g. 5.0 → only 95 % reaches the grid.
    """
    max_charge_rate:     float   # C  (MW)
    max_sell_rate:       float   # S  (MW)
    battery_capacity:    float   # B  (MWh)
    min_price_delta:     float   # Y  (EUR/MWh)
    min_discharge_price: float   # T  (EUR/MWh)
    discharge_loss_pct:  float   # 0–100 (%)

    def __post_init__(self) -> None:
        if not (0.0 <= self.discharge_loss_pct < 100.0):
            raise ValueError("discharge_loss_pct must be in [0, 100)")
        if self.min_discharge_price < self.min_price_delta:
            raise ValueError("min_discharge_price must be >= min_price_delta")

    @property
    def discharge_efficiency(self) -> float:
        """Fraction of drawn energy that actually reaches the grid."""
        return 1.0 - self.discharge_loss_pct / 100.0


@dataclass(frozen=True)
class StationState:
    """
    Dynamic snapshot of the station at the moment the optimiser is called.

    Attributes
    ----------
    station_power        : P – current generation output (MW).
    battery_level        : measured stored energy right now (MWh).
                           The ONLY physical value carried into a rerun.
    initial_charge_price : the effective price (EUR/MWh) at which the energy
                           currently stored in the battery was charged.
                           Used as the starting worst_block_price so that the
                           Y constraint is correctly applied to early discharge
                           slots relative to pre-existing stored energy.
                           Pass 0.0 if unknown (most permissive default).
    """
    station_power:        float         # P  (MW)
    battery_level:        float         # MWh
    initial_charge_price: float = 0.0   # EUR/MWh, default 0 = unknown


@dataclass
class SlotResult:
    """Outcome for a single 15-minute slot."""
    slot_index:        int
    action:            BatteryAction
    energy_charged:    float   # MWh added to battery        (0 unless CHARGE)
    energy_discharged: float   # MWh drawn from battery      (0 unless DISCHARGE)
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
# Charge/discharge ratio calculation
# ---------------------------------------------------------------------------

def compute_charge_discharge_ratio(
    station_power: float,
    cfg:           StationConfig,
) -> float:
    """
    Derive the charge/discharge ratio X from station output P and hardware.

    X = (charge slots to fill battery from empty)
      / (discharge slots to drain battery from full)

    Charge slots to full:    B / (P * t)

    Discharge slots from full:
      Case A — P < S - C:  full rate C available → B / (C * t)
      Case B — P >= S - C: only headroom (S-P) available → B / ((S-P) * t)

    Returns inf if P <= 0 or P >= S.
    """
    t = SLOT_DURATION_HOURS
    B, C, S, P = cfg.battery_capacity, cfg.max_charge_rate, cfg.max_sell_rate, station_power

    if P <= 0:
        return math.inf
    charge_slots = B / (P * t)

    battery_discharge_rate = C if P < S - C else S - P
    if battery_discharge_rate <= 0:
        return math.inf

    discharge_slots = B / (battery_discharge_rate * t)
    return charge_slots / discharge_slots


# ---------------------------------------------------------------------------
# Discretisation helpers
# ---------------------------------------------------------------------------

def _to_key(value: float, resolution: int) -> int:
    return round(value * resolution)


def _from_key(key: int, resolution: int) -> float:
    return key / resolution


# ---------------------------------------------------------------------------
# Physics layer
# ---------------------------------------------------------------------------

def _slot_physics(
    action:                 BatteryAction,
    battery_level:          float,
    scheduling_budget:      float,
    worst_block_price:      float,
    current_block_dir:      int,          # _DIR_NONE / _DIR_CHARGE / _DIR_DISCHARGE
    price:                  float,
    station_power:          float,
    charge_discharge_ratio: float,
    cfg:                    StationConfig,
) -> Optional[tuple[float, float, float, float, float, float, int]]:
    """
    Attempt *action* at the current slot and return its outcome, or None if
    the action is physically or economically infeasible.

    Returns
    -------
    (slot_revenue,
     energy_charged, energy_discharged,
     new_battery_level, new_scheduling_budget,
     new_worst_block_price, new_block_dir)

    Y-constraint logic
    ------------------
    worst_block_price is the most unfavourable price seen in the currently
    open block:
      • CHARGE block → highest charge price (the one that requires the biggest
        future sell price to clear Y)
      • DISCHARGE block → lowest discharge price (the one that requires the
        cheapest future buy price to clear Y)

    On a block transition:
      CHARGE  after DISCHARGE block: price must be <= worst_block_price - Y
                                     (new charge cheap enough vs the weakest
                                      discharge already done)
      DISCHARGE after CHARGE  block: price must be >= worst_block_price + Y
                                     (new discharge expensive enough vs the
                                      most expensive charge already done)

    On block continuation (same direction):
      worst_block_price updates: max(current, price) for CHARGE blocks,
                                  min(current, price) for DISCHARGE blocks.

    HOLD: no Y check, block state unchanged.
    """
    t   = SLOT_DURATION_HOURS
    eff = cfg.discharge_efficiency
    Y   = cfg.min_price_delta
    T   = cfg.min_discharge_price

    effective_charge_rate    = min(cfg.max_charge_rate, station_power)
    effective_discharge_rate = max(0.0, cfg.max_sell_rate - station_power)
    overflow_power           = max(0.0, station_power - cfg.max_charge_rate)

    # ------------------------------------------------------------------
    if action is BatteryAction.HOLD:
        revenue = price * station_power * t
        # Block state is unchanged by HOLD
        return revenue, 0.0, 0.0, battery_level, scheduling_budget, worst_block_price, current_block_dir

    # ------------------------------------------------------------------
    if action is BatteryAction.CHARGE:
        # Y check: only needed when transitioning OUT of a discharge block
        if current_block_dir == _DIR_DISCHARGE:
            # worst_block_price is the lowest discharge price seen so far.
            # New charge must be at least Y below that weakest discharge.
            if worst_block_price - price < Y:
                return None

        headroom  = cfg.battery_capacity - battery_level
        energy_in = min(effective_charge_rate * t, headroom)
        if energy_in <= 1e-9:
            return None  # battery full

        full_charge_step      = effective_charge_rate * t
        budget_earned         = (energy_in / full_charge_step) / charge_discharge_ratio
        new_battery_level     = battery_level + energy_in
        new_scheduling_budget = scheduling_budget + budget_earned
        revenue               = price * overflow_power * t

        # Update block state
        if current_block_dir == _DIR_CHARGE:
            # Continuing charge block: worst = most expensive charge seen
            new_worst = max(worst_block_price, price)
        else:
            # Starting a new charge block (from NONE or DISCHARGE)
            new_worst = price

        return (revenue, energy_in, 0.0,
                new_battery_level, new_scheduling_budget,
                new_worst, _DIR_CHARGE)

    # ------------------------------------------------------------------
    if action is BatteryAction.DISCHARGE:
        # T constraint: absolute price floor
        if price < T:
            return None

        # Y check: only needed when transitioning OUT of a charge block
        if current_block_dir == _DIR_CHARGE:
            # worst_block_price is the highest charge price seen so far.
            # New discharge must be at least Y above that worst charge.
            if price - worst_block_price < Y:
                return None
        elif current_block_dir == _DIR_NONE:
            # Battery has pre-existing charge; worst_block_price was set to
            # initial_charge_price in StationState.  Apply the same check.
            if price - worst_block_price < Y:
                return None

        energy_drawn = min(effective_discharge_rate * t, battery_level)
        if energy_drawn <= 1e-9:
            return None  # battery empty or P at sell cap

        full_discharge_step   = effective_discharge_rate * t
        budget_needed         = energy_drawn / full_discharge_step
        if scheduling_budget < budget_needed - 1e-9:
            return None  # X constraint

        energy_delivered      = energy_drawn * eff
        new_battery_level     = battery_level - energy_drawn
        new_scheduling_budget = scheduling_budget - budget_needed
        sold_power            = min(cfg.max_sell_rate, station_power + energy_delivered / t)
        revenue               = price * sold_power * t

        # Update block state
        if current_block_dir == _DIR_DISCHARGE:
            # Continuing discharge block: worst = cheapest discharge seen
            new_worst = min(worst_block_price, price)
        else:
            # Starting a new discharge block (from CHARGE or NONE)
            new_worst = price

        return (revenue, 0.0, energy_drawn,
                new_battery_level, new_scheduling_budget,
                new_worst, _DIR_DISCHARGE)

    raise ValueError(f"Unknown action: {action}")


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

    DP state:
        (slot,
         battery_level_key,      — discretised MWh
         budget_key,             — discretised scheduling budget
         worst_block_price_key,  — discretised EUR/MWh, worst price in open block
         block_dir)              — _DIR_NONE / _DIR_CHARGE / _DIR_DISCHARGE

    The scheduling_budget and block state reset at the start of every call.
    Only battery_level is a physical carry-over (and initial_charge_price
    seeds the block state when energy is already stored).

    Parameters
    ----------
    prices     : EUR/MWh for each remaining 15-minute slot (up to 96).
    cfg        : static hardware configuration.
    state      : current station snapshot.
    start_slot : slot_index label offset (0 for a full-day run).
    """
    num_slots = len(prices)
    if num_slots == 0:
        return OptimisationResult(schedule=[], total_revenue=0.0, slots_optimised=0)

    ratio      = compute_charge_discharge_ratio(state.station_power, cfg)
    max_budget = num_slots / ratio if ratio > 0 and not math.isinf(ratio) else 0.0

    @lru_cache(maxsize=None)
    def best_from(
        slot:               int,
        batt_key:           int,
        budget_key:         int,
        worst_price_key:    int,
        block_dir:          int,
    ) -> tuple[float, BatteryAction]:
        if slot >= num_slots:
            return 0.0, BatteryAction.HOLD

        batt        = _from_key(batt_key,       _BATTERY_RESOLUTION)
        budget      = _from_key(budget_key,     _BATTERY_RESOLUTION)
        worst_price = _from_key(worst_price_key, _PRICE_RESOLUTION)

        best_revenue = -math.inf
        best_action  = BatteryAction.HOLD

        for action in BatteryAction:
            outcome = _slot_physics(
                action                 = action,
                battery_level          = batt,
                scheduling_budget      = budget,
                worst_block_price      = worst_price,
                current_block_dir      = block_dir,
                price                  = prices[slot],
                station_power          = state.station_power,
                charge_discharge_ratio = ratio,
                cfg                    = cfg,
            )
            if outcome is None:
                continue

            slot_rev, _, _, new_batt, new_budget, new_worst, new_dir = outcome

            new_batt   = max(0.0, min(cfg.battery_capacity, new_batt))
            new_budget = max(0.0, min(max_budget, new_budget))

            future_rev, _ = best_from(
                slot + 1,
                _to_key(new_batt,   _BATTERY_RESOLUTION),
                _to_key(new_budget, _BATTERY_RESOLUTION),
                _to_key(new_worst,  _PRICE_RESOLUTION),
                new_dir,
            )
            total = slot_rev + future_rev
            if total > best_revenue:
                best_revenue = total
                best_action  = action

        return best_revenue, best_action

    # Determine initial block state from pre-existing stored energy
    if state.battery_level > 1e-6:
        # Battery already has charge: treat it as an open CHARGE block whose
        # worst price is the initial_charge_price supplied by the caller.
        init_block_dir   = _DIR_CHARGE
        init_worst_price = state.initial_charge_price
    else:
        init_block_dir   = _DIR_NONE
        init_worst_price = 0.0

    init_batt_key   = _to_key(state.battery_level,  _BATTERY_RESOLUTION)
    init_budget_key = _to_key(0.0,                  _BATTERY_RESOLUTION)
    init_worst_key  = _to_key(init_worst_price,      _PRICE_RESOLUTION)

    total_revenue, _ = best_from(
        0, init_batt_key, init_budget_key, init_worst_key, init_block_dir
    )

    # ------------------------------------------------------------------
    # Reconstruct schedule by replaying the optimal path forward
    # ------------------------------------------------------------------
    schedule:      list[SlotResult] = []
    batt           = state.battery_level
    budget         = 0.0
    worst_price    = init_worst_price
    block_dir      = init_block_dir
    overflow_power = max(0.0, state.station_power - cfg.max_charge_rate)

    for slot in range(num_slots):
        _, chosen_action = best_from(
            slot,
            _to_key(batt,        _BATTERY_RESOLUTION),
            _to_key(budget,      _BATTERY_RESOLUTION),
            _to_key(worst_price, _PRICE_RESOLUTION),
            block_dir,
        )

        outcome = _slot_physics(
            action                 = chosen_action,
            battery_level          = batt,
            scheduling_budget      = budget,
            worst_block_price      = worst_price,
            current_block_dir      = block_dir,
            price                  = prices[slot],
            station_power          = state.station_power,
            charge_discharge_ratio = ratio,
            cfg                    = cfg,
        )

        # Fallback: discretisation rounding can rarely cause a tiny gap
        if outcome is None:
            chosen_action = BatteryAction.HOLD
            outcome = _slot_physics(
                action                 = BatteryAction.HOLD,
                battery_level          = batt,
                scheduling_budget      = budget,
                worst_block_price      = worst_price,
                current_block_dir      = block_dir,
                price                  = prices[slot],
                station_power          = state.station_power,
                charge_discharge_ratio = ratio,
                cfg                    = cfg,
            )

        slot_rev, energy_charged, energy_discharged, new_batt, new_budget, new_worst, new_dir = outcome

        if chosen_action is BatteryAction.DISCHARGE:
            energy_delivered = energy_discharged * cfg.discharge_efficiency
            sold_power = min(cfg.max_sell_rate,
                             state.station_power + energy_delivered / SLOT_DURATION_HOURS)
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

        batt        = new_batt
        budget      = new_budget
        worst_price = new_worst
        block_dir   = new_dir

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
    Re-optimise for the rest of the day after conditions change.

    The scheduling budget and block state reset completely.
    updated_state.battery_level is the only physical carry-over.
    updated_state.initial_charge_price should reflect the average price at
    which the currently stored energy was acquired (pass 0.0 if unknown).

    Parameters
    ----------
    remaining_prices : prices[slots_elapsed:] from the original forecast.
    cfg              : unchanged hardware config.
    updated_state    : fresh snapshot; battery_level is from measurement.
    slots_elapsed    : completed slot count (for slot_index labelling).
    """
    return optimise_battery_schedule(
        prices     = remaining_prices,
        cfg        = cfg,
        state      = updated_state,
        start_slot = slots_elapsed,
    )


# ---------------------------------------------------------------------------
# Pretty-printer
# ---------------------------------------------------------------------------

def print_schedule(result: OptimisationResult, all_prices: list[float]) -> None:
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
        label  = {BatteryAction.CHARGE:    "⚡ CHARGE",
                  BatteryAction.DISCHARGE: "💰 SELL  ",
                  BatteryAction.HOLD:      "   HOLD  "}[r.action]
        print(f"  {r.slot_index:>4}  {hour:02d}:{minute:02d}  "
              f"  {price:>7.2f}   {label}  "
              f"{r.power_sold:>8.4f}  {r.battery_level_end:>9.4f}  "
              f"{r.revenue:>9.4f}")
    print(f"{'='*78}\n")