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

Algorithm design
-----------------
The schedule is solved as a mixed-integer program (MILP) via
`scipy.optimize.milp` (HiGHS backend), which finds the mathematically
maximum-profit schedule subject to every constraint below — not a
heuristic approximation of it. If scipy isn't installed, or the solver
can't return an optimal solution for some reason, this falls back to a
greedy pairwise-matching heuristic (`_greedy_schedule`) that is still
exactly constraint-correct, just not provably optimal.

Formulation (`_milp_schedule`)
-------------------------------
  1. Enumerate every candidate (source, sink) pair (i, j) with i
     chronologically before j, where charging at i and discharging at j
     would clear both the wear threshold (`price[j] - price[i] >= Y`) and
     the discharge floor (`price[j] >= T`). Pre-existing battery charge
     (StationState.battery_level) is one extra virtual source, priced at
     `initial_charge_price`, available to pair with any j.
  2. One continuous decision variable q_ij >= 0 per candidate pair — how
     much energy (MWh) flows from source i to sink j.
  3. One binary decision variable y_i per real slot — whether slot i acts
     as a charge source (y_i=1) or a discharge sink (y_i=0) this run; this
     is what makes it a MILP rather than a plain LP, since a slot
     physically cannot be both in the same 15 minutes.
  4. Maximise sum(q_ij * profit_per_mwh_ij) — see "Why profit-per-MWh"
     below — subject to:
       - source capacity   : sum_j q_ij <= max_charge_energy * y_i
       - sink capacity     : sum_i q_ij <= max_discharge_energy * (1 - y_j)
       - virtual source cap: sum_j q_(-1,j) <= initial battery_level
       - 0 <= battery level at every slot boundary <= B (a linear running
         sum of committed charges minus discharges up to that point)
  Every constraint the user specified (C, S, P, B, Y, T) is therefore
  modelled exactly, not approximated — the solver is free to use *any*
  combination of pairs, in any order, that a feasible physical schedule
  could realise, and proves it found the best one.

Why profit-per-MWh uses `price[j]*eff - price[i]` and not
`price[j] - price[i]`
---------------------------------------------------------------------------
Charging or discharging a slot is always compared to the counterfactual of
holding it instead (selling P directly). Working through the algebra:
holding forgoes `price[i]` of revenue per MWh diverted into the battery at
slot i (this holds regardless of whether i's overflow_power is 0), and
discharging nets `price[j] * eff` of extra revenue per MWh drawn from the
battery at slot j, because only `eff` of what's drawn actually reaches the
grid (see `discharge_efficiency`). Y itself is still checked on the raw,
unscaled price spread — that's the human-facing wear threshold the user
defines it against — only the objective coefficients need the
efficiency-adjusted figure.

Rate limits
-----------
  eff_charge_rate     = min(C, P)              — charging: all of P goes to
                        the battery, capped by the battery's own max rate.
  eff_discharge_rate  = min(C, max(0, S - P))  — discharging: the station's
                        own power P is sold directly; the battery makes up
                        the difference to reach the sell cap S, capped by
                        the battery's own max discharge rate.
  overflow_power      = max(0, P - C)          — power that can't physically
                        fit into the battery even while charging; sold
                        immediately at the spot price.

The charge:discharge ratio X described by the user (how many charge slots
are needed, at the current P, to fill the battery relative to how many
discharge slots are needed to empty it) is not tracked as a separate
scheduling budget — it falls out naturally from the source/sink capacities
above. `compute_charge_discharge_ratio` is kept as a standalone,
informational figure (e.g. for logging/reporting), matching how it was
used before.

Performance
-----------
O(N^2) candidate pairs (continuous variables) plus N binary variables and
O(N) boundary constraints. HiGHS solves this in well under a second for a
day of 15-minute slots (N ~ 100-150). If this is ever run over a much
longer horizon, the pair set should be pruned or the boundary constraints
should move to an incremental/sparse formulation (already sparse here, but
the O(N^2) pair count is the part that would need attention first).

Pre-existing battery charge
----------------------------
If battery_level > 0 at the start of a run, StationState.initial_charge_price
prices a virtual source slot (see step 1 above) so early discharges are
evaluated against it correctly.

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
import warnings
from dataclasses import dataclass
from enum import Enum
from typing import Optional

try:
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    _HAS_MILP = True
except ImportError:
    _HAS_MILP = False


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
    min_price_delta      : Y – minimum price spread (EUR/MWh) required
                           between the price a unit of energy was charged at
                           and the price it is later discharged at, for that
                           trade to be worth the battery wear.
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
                           Seeds the cost-basis ledger for early discharges.
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
# Shared setup / finalisation
# ---------------------------------------------------------------------------

_EPS = 1e-9


def _rates(cfg: StationConfig, state: StationState, t: float) -> tuple[float, float, float]:
    """Returns (max_charge_energy, max_discharge_energy, overflow_power) for one slot."""
    C, S, P = cfg.max_charge_rate, cfg.max_sell_rate, state.station_power
    eff_charge_rate    = min(C, P)
    eff_discharge_rate = min(C, max(0.0, S - P))
    overflow_power     = max(0.0, P - C)
    return eff_charge_rate * t, eff_discharge_rate * t, overflow_power


def _build_candidate_pairs(
    prices: list[float],
    cfg:    StationConfig,
    state:  StationState,
    max_charge_energy:    float,
    max_discharge_energy: float,
    initial_level: float,
) -> list[tuple[int, int, float]]:
    """
    Every (source, sink, profit_per_mwh) triple that clears Y and T.
    source == -1 is the pre-existing battery charge (see module docstring).
    """
    num_slots = len(prices)
    eff = cfg.discharge_efficiency
    Y   = cfg.min_price_delta
    T   = cfg.min_discharge_price
    initial_price = state.initial_charge_price

    pairs: list[tuple[int, int, float]] = []
    if max_discharge_energy <= _EPS:
        return pairs

    if initial_level > _EPS:
        for j in range(num_slots):
            pj = prices[j]
            if pj - initial_price >= Y and pj >= T:
                pairs.append((-1, j, pj * eff - initial_price))

    if max_charge_energy > _EPS:
        for i in range(num_slots - 1):
            pi = prices[i]
            for j in range(i + 1, num_slots):
                pj = prices[j]
                if pj - pi >= Y and pj >= T:
                    pairs.append((i, j, pj * eff - pi))

    return pairs


def _finalize_schedule(
    prices:             list[float],
    cfg:                StationConfig,
    state:              StationState,
    start_slot:         int,
    energy_charged:     list[float],
    energy_discharged:  list[float],
    overflow_power:     float,
) -> OptimisationResult:
    """Turns per-slot charge/discharge amounts into the public result type."""
    num_slots = len(prices)
    t   = 0.25
    eff = cfg.discharge_efficiency
    S   = cfg.max_sell_rate
    P   = state.station_power

    schedule: list[SlotResult] = []
    total_revenue = 0.0
    battery_level = min(max(state.battery_level, 0.0), cfg.battery_capacity)

    for slot in range(num_slots):
        price = prices[slot]
        ec = energy_charged[slot]
        ed = energy_discharged[slot]

        if ec > _EPS:
            action = BatteryAction.CHARGE
            battery_level += ec
            power_sold = overflow_power
            revenue    = price * overflow_power * t
        elif ed > _EPS:
            action = BatteryAction.DISCHARGE
            battery_level -= ed
            energy_delivered = ed * eff
            power_sold       = min(S, P + energy_delivered / t)
            revenue          = price * power_sold * t
        else:
            action = BatteryAction.HOLD
            power_sold = P
            revenue    = price * P * t

        total_revenue += revenue

        schedule.append(SlotResult(
            slot_index        = start_slot + slot,
            action            = action,
            energy_charged    = ec,
            energy_discharged = ed,
            power_sold        = power_sold,
            revenue           = revenue,
            battery_level_end = battery_level,
        ))

    return OptimisationResult(
        schedule        = schedule,
        total_revenue   = total_revenue,
        slots_optimised = num_slots,
    )


# ---------------------------------------------------------------------------
# Greedy pairwise-matching fallback (used when scipy/HiGHS isn't available,
# or the solver can't return an optimal result)
# ---------------------------------------------------------------------------

def _greedy_schedule(
    prices:     list[float],
    cfg:        StationConfig,
    state:      StationState,
    start_slot: int,
    max_charge_energy:    float,
    max_discharge_energy: float,
    overflow_power:       float,
) -> OptimisationResult:
    num_slots = len(prices)
    B = cfg.battery_capacity
    initial_level = min(max(state.battery_level, 0.0), B)

    pairs = _build_candidate_pairs(
        prices, cfg, state, max_charge_energy, max_discharge_energy, initial_level
    )
    pairs.sort(key=lambda c: -c[2])

    remaining_charge    = [max_charge_energy] * num_slots
    remaining_discharge = [max_discharge_energy] * num_slots
    remaining_initial    = initial_level
    slot_role: list[Optional[str]] = [None] * num_slots  # 'charge' | 'discharge'

    # level[k] = battery level at the boundary *before* slot k's action.
    # Starts flat at initial_level; charge/discharge pairs are range updates.
    level = [initial_level] * (num_slots + 1)

    energy_charged    = [0.0] * num_slots
    energy_discharged = [0.0] * num_slots

    for i, j, _ in pairs:
        if slot_role[j] == 'charge':
            continue
        if i == -1:
            src_cap = remaining_initial
        else:
            if slot_role[i] == 'discharge':
                continue
            src_cap = remaining_charge[i]
        if src_cap <= _EPS or remaining_discharge[j] <= _EPS:
            continue

        if i == -1:
            # Consuming pre-existing charge only ever removes energy from
            # slot j+1 onward (it's already reflected in the flat baseline).
            room = min(level[k] for k in range(j + 1, num_slots + 1))
        else:
            # A same-pair charge-then-discharge nets to zero after j, so the
            # only range that matters is the open interval (i, j].
            room = B - max(level[k] for k in range(i + 1, j + 1))

        q = min(src_cap, remaining_discharge[j], room)
        if q <= _EPS:
            continue

        if i == -1:
            remaining_initial -= q
            for k in range(j + 1, num_slots + 1):
                level[k] -= q
        else:
            remaining_charge[i] -= q
            energy_charged[i] += q
            slot_role[i] = 'charge'
            for k in range(i + 1, j + 1):
                level[k] += q

        remaining_discharge[j] -= q
        energy_discharged[j] += q
        slot_role[j] = 'discharge'

    return _finalize_schedule(
        prices, cfg, state, start_slot, energy_charged, energy_discharged, overflow_power
    )


# ---------------------------------------------------------------------------
# Exact MILP scheduler
# ---------------------------------------------------------------------------

def _milp_schedule(
    prices:     list[float],
    cfg:        StationConfig,
    state:      StationState,
    start_slot: int,
    max_charge_energy:    float,
    max_discharge_energy: float,
    overflow_power:       float,
) -> Optional[OptimisationResult]:
    """Returns None if the solver doesn't return an optimal solution."""
    num_slots = len(prices)
    B = cfg.battery_capacity
    initial_level = min(max(state.battery_level, 0.0), B)

    pairs = _build_candidate_pairs(
        prices, cfg, state, max_charge_energy, max_discharge_energy, initial_level
    )
    n_pairs = len(pairs)

    energy_charged    = [0.0] * num_slots
    energy_discharged = [0.0] * num_slots
    if n_pairs == 0:
        return _finalize_schedule(
            prices, cfg, state, start_slot, energy_charged, energy_discharged, overflow_power
        )

    n_vars = n_pairs + num_slots  # [q_0..q_{P-1}, y_0..y_{N-1}]

    pairs_by_source: dict[int, list[int]] = {}
    pairs_by_sink:   dict[int, list[int]] = {}
    for k, (i, j, _profit) in enumerate(pairs):
        pairs_by_source.setdefault(i, []).append(k)
        pairs_by_sink.setdefault(j, []).append(k)

    objective = np.zeros(n_vars)
    for k, (_i, _j, profit) in enumerate(pairs):
        objective[k] = -profit  # milp minimises, so negate to maximise profit

    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    lb: list[float] = []
    ub: list[float] = []
    row_idx = 0

    def add_row(col_coef: dict[int, float], row_lb: float, row_ub: float) -> None:
        nonlocal row_idx
        for col, coef in col_coef.items():
            rows.append(row_idx)
            cols.append(col)
            data.append(coef)
        lb.append(row_lb)
        ub.append(row_ub)
        row_idx += 1

    for i in range(num_slots):
        y_col = n_pairs + i
        src = pairs_by_source.get(i)
        if src:
            add_row({**{k: 1.0 for k in src}, y_col: -max_charge_energy}, -math.inf, 0.0)
        snk = pairs_by_sink.get(i)
        if snk:
            add_row({**{k: 1.0 for k in snk}, y_col: max_discharge_energy},
                    -math.inf, max_discharge_energy)

    if -1 in pairs_by_source:
        add_row({k: 1.0 for k in pairs_by_source[-1]}, -math.inf, initial_level)

    # Battery level at every boundary m=1..N, expressed relative to
    # initial_level: charge_before_m - discharge_before_m in [-initial_level,
    # B - initial_level]. Built incrementally so this stays O(pairs + N)
    # instead of O(pairs * N).
    active: dict[int, float] = {}
    for m in range(1, num_slots + 1):
        for k in pairs_by_source.get(m - 1, []):
            active[k] = active.get(k, 0.0) + 1.0
        for k in pairs_by_sink.get(m - 1, []):
            active[k] = active.get(k, 0.0) - 1.0
        if active:
            add_row(dict(active), -initial_level, B - initial_level)

    if not rows:
        return _finalize_schedule(
            prices, cfg, state, start_slot, energy_charged, energy_discharged, overflow_power
        )

    from scipy.sparse import coo_matrix
    A = coo_matrix((data, (rows, cols)), shape=(row_idx, n_vars))
    constraint = LinearConstraint(A, lb=lb, ub=ub)

    var_lb = np.zeros(n_vars)
    var_ub = np.full(n_vars, np.inf)
    var_ub[:n_pairs] = [max_charge_energy if i != -1 else initial_level for i, _j, _p in pairs]
    var_ub[n_pairs:] = 1.0
    integrality = np.zeros(n_vars)
    integrality[n_pairs:] = 1.0

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = milp(
            objective,
            constraints=[constraint],
            integrality=integrality,
            bounds=Bounds(var_lb, var_ub),
        )

    if not res.success:
        return None

    for k, (i, j, _profit) in enumerate(pairs):
        q = res.x[k]
        if q <= _EPS:
            continue
        if i != -1:
            energy_charged[i] += q
        energy_discharged[j] += q

    return _finalize_schedule(
        prices, cfg, state, start_slot, energy_charged, energy_discharged, overflow_power
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def optimise_battery_schedule(
    prices:     list[float],
    cfg:        StationConfig,
    state:      StationState,
    start_slot: int = 0,
) -> OptimisationResult:
    """
    Schedule CHARGE/DISCHARGE/HOLD for every slot in `prices` to maximise
    total revenue, subject to rate limits (C, P), the sell cap (S), battery
    capacity (B), the wear threshold (Y, checked per matched pair) and the
    discharge floor (T).

    Solved exactly via MILP when scipy is available (see module docstring);
    falls back to a constraint-correct greedy heuristic otherwise.
    """
    num_slots = len(prices)
    if num_slots == 0:
        return OptimisationResult(schedule=[], total_revenue=0.0, slots_optimised=0)

    t = 0.25
    max_charge_energy, max_discharge_energy, overflow_power = _rates(cfg, state, t)

    if _HAS_MILP:
        result = _milp_schedule(
            prices, cfg, state, start_slot,
            max_charge_energy, max_discharge_energy, overflow_power,
        )
        if result is not None:
            return result
        warnings.warn(
            "MILP solver did not return an optimal solution; "
            "falling back to the greedy heuristic scheduler.",
            RuntimeWarning,
        )
    else:
        warnings.warn(
            "scipy is not installed; using the greedy heuristic scheduler "
            "instead of the exact MILP solver. Install scipy for a "
            "mathematically guaranteed maximum-profit schedule.",
            RuntimeWarning,
        )

    return _greedy_schedule(
        prices, cfg, state, start_slot,
        max_charge_energy, max_discharge_energy, overflow_power,
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
