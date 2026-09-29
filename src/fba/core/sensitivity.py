from collections import abc

from fba.contracts.auction import (
    AuctionInput,
    AuctionResult,
    CapSensitivity,
    DraftState,
    Infeasible,
    ManagedFitSummary,
    SolverError,
)
from fba.contracts.base import DataError
from fba.contracts.config import ManagedPricingModel
from fba.core.auction import portfolio_for
from fba.core.roster import effective_players


def cap_sensitivity(
    inputs: AuctionInput,
    state: DraftState,
    fitted: AuctionResult,
    player_id: str,
    checkpoint: abc.Callable[[], None],
) -> CapSensitivity:
    summary, model = fitted.fit, inputs.config.model
    if (
        not isinstance(model, ManagedPricingModel)
        or not isinstance(summary, ManagedFitSummary)
        or summary.sensitivity is None
    ):
        raise DataError("sensitivity: current managed health-group estimates are unavailable")
    central = next((c for c in fitted.caps if c.player_id == player_id), None)
    if central is None or central.amount is None or central.reason is not None:
        raise DataError("sensitivity: player has no actionable current cap")
    groups: list[int] = []
    calls = 0
    players = effective_players(inputs.config.league, inputs.players, state)
    ids = tuple(p.id for p in players)
    expected = model.fit.health_blocks if summary.selected_step else 0
    if len(summary.sensitivity) != expected:
        raise DataError("sensitivity: health-group count differs from the configured partition")
    for sample in summary.sensitivity:
        checkpoint()
        if tuple(p.id for p in sample) != ids:
            raise DataError("sensitivity: player population differs from current state")
        if any(
            (p.utility is None) != (s.utility is None) for p, s in zip(players, sample, strict=True)
        ):
            raise DataError("sensitivity: health-group availability differs from current state")
        changed = tuple(
            p.model_copy(update={"utility": s.utility})
            for p, s in zip(players, sample, strict=True)
        )
        portfolio = portfolio_for(inputs, changed, fitted.market, state)
        base = portfolio.solve(canonical=True)
        if isinstance(base, Infeasible):
            raise SolverError("sensitivity: utility change unexpectedly changed feasibility")
        groups.append(portfolio.cap(ids.index(player_id), base)[0])
        calls += portfolio.calls
    checkpoint()
    return CapSensitivity(
        player_id=player_id,
        status="ready" if groups else "baseline_retained",
        central=central.amount,
        groups=tuple(groups),
        low=min(central.amount, *groups) if groups else None,
        high=max(central.amount, *groups) if groups else None,
        solver_calls=calls,
    )
