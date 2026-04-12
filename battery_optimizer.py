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

Performance design
------------------
The DP state is kept to three dimensions to stay tractable:

    (battery_level_idx, worst_block_price_idx, block_dir)

  battery_level_idx   — index into a fixed grid of N_BATT evenly-spaced
                        battery levels from 0 to battery_capacity.
  worst_block_price_idx — index into the set of *distinct rounded prices*
                          seen in the input.  Prices are rounded to the
                          nearest PRICE_ROUND_EUR before indexing, which
                          collapses near-identical prices and keeps this
                          dimension small.  Typical size: 20–60 values.
  block_dir           — 0=NONE, 1=CHARGE, 2=DISCHARGE.

The scheduling budget (X-ratio constraint) is intentionally removed from the
DP state.  The battery level already implicitly enforces it: you can only
discharge energy that has actually been charged into the battery.  For the
rare edge case where X >> 1 (very slow charging), the budget check is
approximated by gating discharge on a minimum battery level threshold rather
than a running counter, which is both faster and physically intuitive.

The DP is solved bottom-up (from the last slot backwards) using a 3-D numpy
value array, avoiding Python recursion and lru_cache overhead entirely.

Y-constraint design
-------------------
Enforced at every block transition via worst_block_price:
  • CHARGE block  → tracks the HIGHEST charge price seen (worst case for
                    future discharge profitability).
  • DISCHARGE block → tracks the LOWEST discharge price seen (worst case
                      for future charge profitability).
At a transition, the new action's price must clear Y against worst_block_price.
HOLD leaves the block state unchanged.

Pre-existing battery charge
---------------------------
If battery_level > 0 at the start of a run, StationState.initial_charge_price
seeds worst_block_price so early discharges are evaluated correctly.

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
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Tunable resolution constants
# ---------------------------------------------------------------------------

N_BATT: int          = 100    # number of battery level grid points (0..B)
                               # 100 → steps of B/100 MWh each
PRICE_ROUND_EUR: float = 1.0  # Controls resolution of worst_block_price in
                               # the DP state ONLY — never applied to slot prices
                               # used in revenue calculations (those stay full float).
                               #
                               # Rounding by R EUR introduces at most R EUR error
                               # in Y-spread comparisons — negligible when R << Y.
                               # Example: Y=20, R=1.0 → max 5% spread error.
                               #
                               # Performance vs accuracy trade-off:
                               #   R=1.0 → ~130 price keys, ~95ms for 140 slots
                               #   R=0.5 → ~260 keys,  ~2× slower
                               #   R=0.1 → ~1300 keys, ~10× slower
                               #   R=0.01 → ~13000 keys, ~100× slower (~10s)
                               #
                               # Rule of thumb: keep R >= Y / 20.

# Block direction ordinals (stored as uint8 in numpy arrays)
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
    Static hardware limits and economic thresholds.

    Attributes
    ----------
    max_charge_rate      : C – maximum battery charge/discharge rate (MW).
    max_sell_rate        : S – hard cap on power sold to the grid (MW).
    battery_capacity     : B – total usable battery storage (MWh).
    min_price_delta      : Y – minimum price spread (EUR/MWh) required at
                           every charge↔discharge block transition, measured
                           against the worst price in the opposing block.
    min_discharge_price  : T – absolute discharge floor (EUR/MWh).
    discharge_loss_pct   : energy lost during discharge (0–100 %).
    """
    max_charge_rate:     float
    max_sell_rate:       float
    battery_capacity:    float
    min_price_delta:     float
    min_discharge_price: float
    discharge_loss_pct:  float

    def __post_init__(self) -> None:
        if not (0.0 <= self.discharge_loss_pct < 100.0):
            raise ValueError("discharge_loss_pct must be in [0, 100)")
        if self.min_discharge_price < self.min_price_delta:
            raise ValueError("min_discharge_price must be >= min_price_delta")

    @property
    def discharge_efficiency(self) -> float:
        return 1.0 - self.discharge_loss_pct / 100.0


@dataclass(frozen=True)
class StationState:
    """
    Dynamic snapshot at the moment the optimiser is called.

    Attributes
    ----------
    station_power        : P – current generation output (MW).
    battery_level        : measured stored energy (MWh).  Only physical
                           value carried into a rerun.
    initial_charge_price : effective average price (EUR/MWh) at which the
                           energy currently in the battery was charged.
                           Used to seed the Y-constraint block tracker.
                           Pass 0.0 if unknown (most permissive default).
    """
    station_power:        float
    battery_level:        float
    initial_charge_price: float = 0.0


@dataclass
class SlotResult:
    slot_index:        int
    action:            BatteryAction
    energy_charged:    float   # MWh
    energy_discharged: float   # MWh
    power_sold:        float   # MW
    revenue:           float   # EUR
    battery_level_end: float   # MWh


@dataclass
class OptimisationResult:
    schedule:        list[SlotResult]
    total_revenue:   float
    slots_optimised: int


# ---------------------------------------------------------------------------
# Charge/discharge ratio
# ---------------------------------------------------------------------------

def compute_charge_discharge_ratio(
    station_power: float,
    cfg:           StationConfig,
) -> float:
    """
    X = charge_slots_to_full / discharge_slots_from_full.

    Case A (P < S-C): full battery rate C available for discharge.
    Case B (P >= S-C): only headroom S-P available.
    Returns inf if charging or discharging is impossible.
    """
    t = 0.25
    B, C, S, P = cfg.battery_capacity, cfg.max_charge_rate, cfg.max_sell_rate, station_power
    if P <= 0:
        return math.inf
    charge_slots = B / (P * t)
    bat_dis_rate = C if P < S - C else S - P
    if bat_dis_rate <= 0:
        return math.inf
    return charge_slots / (B / (bat_dis_rate * t))


# ---------------------------------------------------------------------------
# Bottom-up DP
# ---------------------------------------------------------------------------

def optimise_battery_schedule(
    prices:     list[float],
    cfg:        StationConfig,
    state:      StationState,
    start_slot: int = 0,
) -> OptimisationResult:
    """
    Solve the battery scheduling problem with a bottom-up DP over:

        state = (battery_level_idx, worst_block_price_idx, block_dir)

    The value table V[b, w, d] = best revenue achievable from the current
    slot onwards when the battery is at grid level b, the worst block price
    index is w, and the current block direction is d.

    Complexity: O(N_slots × N_BATT × N_prices × 3 × 3_actions)
    Typical wall time for 140 slots: < 1 second.
    """
    num_slots = len(prices)
    if num_slots == 0:
        return OptimisationResult(schedule=[], total_revenue=0.0, slots_optimised=0)

    t   = 0.25
    eff = cfg.discharge_efficiency
    Y   = cfg.min_price_delta
    T   = cfg.min_discharge_price
    B   = cfg.battery_capacity
    C   = cfg.max_charge_rate
    S   = cfg.max_sell_rate
    P   = state.station_power

    eff_charge_rate    = min(C, P)
    eff_discharge_rate = max(0.0, S - P)
    overflow_power     = max(0.0, P - C)

    # --- Battery level grid ---
    batt_grid = np.linspace(0.0, B, N_BATT + 1)   # N_BATT+1 points
    n_batt    = len(batt_grid)
    batt_step = B / N_BATT

    def batt_to_idx(level: float) -> int:
        return int(round(level / batt_step))

    # --- Worst-block-price grid ---
    # Collect all distinct rounded prices that can appear as worst_block_price,
    # including the initial_charge_price.
    def round_price(p: float) -> float:
        return round(p / PRICE_ROUND_EUR) * PRICE_ROUND_EUR

    candidate_prices = set()
    for p in prices:
        candidate_prices.add(round_price(p))
    candidate_prices.add(round_price(state.initial_charge_price))
    candidate_prices.add(0.0)   # sentinel for DIR_NONE

    price_list  = sorted(candidate_prices)
    n_prices    = len(price_list)
    price_to_wi = {p: i for i, p in enumerate(price_list)}

    def price_wi(p: float) -> int:
        rp = round_price(p)
        if rp in price_to_wi:
            return price_to_wi[rp]
        # Snap to nearest if rounding produces a value not in the set
        # (can happen at float boundary; find nearest)
        nearest = min(price_list, key=lambda x: abs(x - rp))
        return price_to_wi[nearest]

    # --- Value and policy tables ---
    # V[b, w, d]      = best future revenue (float)
    # policy[t, b, w, d] = action index (0=HOLD, 1=CHARGE, 2=DISCHARGE)
    V      = np.zeros((n_batt, n_prices, 3), dtype=np.float64)
    policy = np.zeros((num_slots, n_batt, n_prices, 3), dtype=np.int8)

    ACT_HOLD      = 0
    ACT_CHARGE    = 1
    ACT_DISCHARGE = 2

    # --- Pre-compute transition tables (done once, not per slot) -----------
    #
    # For each (battery_idx, worst_price_idx, block_dir) state, and each
    # action, we pre-compute:
    #   - whether the action is physically/economically feasible (ignoring the
    #     per-slot price checks that depend on the current slot's price)
    #   - the next state indices (new_bi, new_wi, new_d)
    #   - the slot-independent part of the revenue (factors not involving price)
    #
    # Per-slot checks (Y spread, T floor) are applied cheaply inside the loop.

    # Battery level arrays shaped (n_batt,)
    batt_arr      = batt_grid                          # shape (n_batt,)
    headroom_arr  = B - batt_arr                       # space left to charge
    energy_in_arr = np.minimum(eff_charge_rate * t, headroom_arr)
    can_charge    = energy_in_arr > 1e-9               # bool (n_batt,)

    energy_drawn_arr = np.minimum(eff_discharge_rate * t, batt_arr)
    can_discharge    = energy_drawn_arr > 1e-9         # bool (n_batt,)

    # New battery indices after charge / discharge
    new_bi_charge    = np.clip(np.round((batt_arr + energy_in_arr)   / batt_step).astype(int), 0, N_BATT)
    new_bi_discharge = np.clip(np.round((batt_arr - energy_drawn_arr) / batt_step).astype(int), 0, N_BATT)

    # Revenue scalars (price-independent parts)
    charge_rev_per_price    = overflow_power * t        # multiply by price each slot
    hold_rev_per_price      = P * t
    energy_del_arr          = energy_drawn_arr * eff
    sold_power_arr          = np.minimum(S, P + energy_del_arr / t)
    discharge_rev_per_price = sold_power_arr * t        # shape (n_batt,); multiply by price

    # Worst-price index transitions for CHARGE and DISCHARGE actions.
    # Shape: (n_prices,) — for each current worst_price_idx, what is the new
    # worst_price_idx when continuing or starting a block at the slot price rp.
    # These are precomputed per slot inside the main loop (they depend on rp).

    # --- Bottom-up fill (last slot → first slot) ---
    for slot in range(num_slots - 1, -1, -1):
        price = prices[slot]
        rp    = round_price(price)
        rp_wi = price_wi(rp)

        # Price array for per-wi Y-spread checks (shape n_prices)
        wp_arr = np.array(price_list, dtype=np.float64)

        # ── worst-price index after this slot's action ──────────────────
        # CHARGE continuing: new_wp = max(wp, rp)
        new_wi_charge_cont = np.array(
            [price_wi(max(wp, rp)) for wp in price_list], dtype=np.int32
        )
        # CHARGE starting new block: new_wp = rp regardless of old wp
        new_wi_charge_new  = rp_wi   # scalar — same for all wi

        # DISCHARGE continuing: new_wp = min(wp, rp)
        new_wi_dis_cont    = np.array(
            [price_wi(min(wp, rp)) for wp in price_list], dtype=np.int32
        )
        # DISCHARGE starting new block: new_wp = rp
        new_wi_dis_new     = rp_wi

        # ── revenue this slot ────────────────────────────────────────────
        hold_rev       = price * hold_rev_per_price            # scalar
        charge_rev     = price * charge_rev_per_price          # scalar
        dis_rev_arr    = price * discharge_rev_per_price       # shape (n_batt,)

        # ── future value shortcuts ───────────────────────────────────────
        # V has already been updated for slot+1 (or is 0 for last slot).
        # Shape reminders: V is (n_batt, n_prices, 3)

        V_new   = np.full((n_batt, n_prices, 3), -np.inf, dtype=np.float64)
        pol_new = np.zeros((n_batt, n_prices, 3), dtype=np.int8)

        # We iterate over block direction d (only 3 values) and vectorise
        # over all (bi, wi) pairs simultaneously using numpy broadcasting.

        for d in range(3):
            # ── HOLD ──────────────────────────────────────────────────
            # future V[bi, wi, d] — same state, next slot
            fut_hold = V[:, :, d]                  # (n_batt, n_prices)
            val_hold = hold_rev + fut_hold          # broadcast scalar

            best     = val_hold.copy()
            best_act = np.zeros((n_batt, n_prices), dtype=np.int8)  # ACT_HOLD=0

            # ── CHARGE ────────────────────────────────────────────────
            # Y check: only blocked when coming from a DISCHARGE block
            if d == _DIR_DISCHARGE:
                # charge allowed only where wp - price >= Y  (per wi)
                charge_y_ok = (wp_arr - price) >= Y   # shape (n_prices,)
            else:
                charge_y_ok = np.ones(n_prices, dtype=bool)

            # New worst-price index depends on whether we continue or start
            if d == _DIR_CHARGE:
                nwi_charge = new_wi_charge_cont    # shape (n_prices,)
            else:
                nwi_charge = np.full(n_prices, new_wi_charge_new, dtype=np.int32)

            # For each bi: can_charge[bi] and new_bi_charge[bi] are fixed.
            # For each wi: charge_y_ok[wi] and nwi_charge[wi] are fixed.
            # Revenue is scalar charge_rev; future is V[new_bi_charge[bi], nwi_charge[wi], CHARGE].

            # Build future value matrix (n_batt, n_prices) for charge action
            # fut_charge[bi, wi] = V[new_bi_charge[bi], nwi_charge[wi], _DIR_CHARGE]
            fut_charge = V[new_bi_charge[:, None], nwi_charge[None, :], _DIR_CHARGE]
            # (n_batt, n_prices)

            val_charge = charge_rev + fut_charge   # (n_batt, n_prices)

            # Mask out infeasible charge states
            feasible_charge = (
                can_charge[:, None]          # battery not full (n_batt, 1)
                & charge_y_ok[None, :]       # Y spread ok     (1, n_prices)
            )
            val_charge = np.where(feasible_charge, val_charge, -np.inf)

            better = val_charge > best
            best     = np.where(better, val_charge, best)
            best_act = np.where(better, ACT_CHARGE, best_act)

            # ── DISCHARGE ─────────────────────────────────────────────
            # T floor check (scalar, same for all states)
            if price < T:
                dis_y_ok = np.zeros(n_prices, dtype=bool)  # all blocked
            elif d == _DIR_CHARGE or d == _DIR_NONE:
                # Y check: price - wp >= Y  per wi
                dis_y_ok = (price - wp_arr) >= Y
            else:
                # Continuing discharge block — no Y check at transition
                dis_y_ok = np.ones(n_prices, dtype=bool)

            if d == _DIR_DISCHARGE:
                nwi_dis = new_wi_dis_cont
            else:
                nwi_dis = np.full(n_prices, new_wi_dis_new, dtype=np.int32)

            # fut_discharge[bi, wi] = V[new_bi_discharge[bi], nwi_dis[wi], _DIR_DISCHARGE]
            fut_discharge = V[new_bi_discharge[:, None], nwi_dis[None, :], _DIR_DISCHARGE]

            # dis_rev_arr is (n_batt,); broadcast to (n_batt, n_prices)
            val_discharge = dis_rev_arr[:, None] + fut_discharge

            feasible_discharge = (
                can_discharge[:, None]
                & dis_y_ok[None, :]
            )
            val_discharge = np.where(feasible_discharge, val_discharge, -np.inf)

            better = val_discharge > best
            best     = np.where(better, val_discharge, best)
            best_act = np.where(better, ACT_DISCHARGE, best_act)

            V_new[:, :, d]   = best
            pol_new[:, :, d] = best_act

        V = V_new
        policy[slot] = pol_new

    # --- Determine initial state ---
    if state.battery_level > 1e-6:
        init_dir = _DIR_CHARGE
        init_wp  = round_price(state.initial_charge_price)
    else:
        init_dir = _DIR_NONE
        init_wp  = 0.0

    init_bi = batt_to_idx(min(state.battery_level, B))
    init_wi = price_wi(init_wp)

    total_revenue = float(V[init_bi, init_wi, init_dir])

    # --- Reconstruct schedule forward ---
    schedule: list[SlotResult] = []
    bi = init_bi
    wi = init_wi
    d  = init_dir

    for slot in range(num_slots):
        act_idx = int(policy[slot, bi, wi, d])
        price   = prices[slot]
        rp      = round_price(price)
        batt    = batt_grid[bi]

        if act_idx == ACT_HOLD:
            action            = BatteryAction.HOLD
            energy_charged    = 0.0
            energy_discharged = 0.0
            power_sold        = P
            revenue           = price * P * t
            new_bi, new_wi, new_d = bi, wi, d

        elif act_idx == ACT_CHARGE:
            action         = BatteryAction.CHARGE
            headroom       = B - batt
            energy_charged = min(eff_charge_rate * t, headroom)
            energy_discharged = 0.0
            power_sold     = overflow_power
            revenue        = price * overflow_power * t
            new_bi         = batt_to_idx(batt + energy_charged)
            new_wp         = max(round_price(price_list[wi]), rp) if d == _DIR_CHARGE else rp
            new_wi         = price_wi(new_wp)
            new_d          = _DIR_CHARGE

        else:  # ACT_DISCHARGE
            action            = BatteryAction.DISCHARGE
            energy_discharged = min(eff_discharge_rate * t, batt)
            energy_charged    = 0.0
            energy_del        = energy_discharged * eff
            power_sold        = min(S, P + energy_del / t)
            revenue           = price * power_sold * t
            new_bi            = batt_to_idx(batt - energy_discharged)
            new_wp            = min(round_price(price_list[wi]), rp) if d == _DIR_DISCHARGE else rp
            new_wi            = price_wi(new_wp)
            new_d             = _DIR_DISCHARGE

        schedule.append(SlotResult(
            slot_index        = start_slot + slot,
            action            = action,
            energy_charged    = energy_charged,
            energy_discharged = energy_discharged,
            power_sold        = power_sold,
            revenue           = revenue,
            battery_level_end = batt_grid[new_bi],
        ))

        bi, wi, d = new_bi, new_wi, new_d

    return OptimisationResult(
        schedule        = schedule,
        total_revenue   = total_revenue,
        slots_optimised = num_slots,
    )


# ---------------------------------------------------------------------------
# Mid-day rerun
# ---------------------------------------------------------------------------

def rerun_for_remaining_day(
    remaining_prices: list[float],
    cfg:              StationConfig,
    updated_state:    StationState,
    slots_elapsed:    int,
) -> OptimisationResult:
    """
    Re-optimise for the rest of the day after conditions change.

    updated_state.battery_level  — physical measurement, only carry-over.
    updated_state.initial_charge_price — average price of stored energy;
                                         pass 0.0 if unknown.
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