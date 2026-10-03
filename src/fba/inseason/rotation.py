"""Allocate playing opportunities before producing any player statistics."""

from collections import defaultdict

from fba.contracts.base import DataError
from fba.contracts.formula import FormulaTrace
from fba.contracts.inseason import (
    AdjustmentField,
    EffectivePlayer,
    InseasonParameters,
    PlayerPrior,
    ProjectionRules,
)
from fba.formulas.registry import evaluate


def estimate_role(
    prior: PlayerPrior, minutes: tuple[float, ...], k: float, half_life: float
) -> tuple[FormulaTrace, FormulaTrace]:
    if prior.appearance_probability is None:
        raise DataError("prior: 舊預測缺少出賽場數；請重新載入已鎖定的賽季前預測")
    conditional = evaluate(
        "minutes",
        k=k,
        prior=prior.minutes,
        minutes=tuple(m for m in minutes if m > 0),
        half_life=half_life,
    )
    appearance = evaluate(
        "blend",
        k=k,
        prior=prior.appearance_probability,
        total=float(sum(m > 0 for m in minutes)),
        sample=float(len(minutes)),
    )
    return conditional, appearance


def allocate_rotation(
    players: tuple[EffectivePlayer, ...],
    rules: ProjectionRules,
    params: InseasonParameters,
    known_roles: set[str],
    back_to_back: set[str],
) -> tuple[EffectivePlayer, ...]:
    """Preserve manual roles; give modeled roles priority over unknown peer roles.

    Minutes remain conditional on an appearance. Scarce team capacity reduces
    playing opportunities, never increases a player's requested role or rate.
    """
    budget = evaluate(
        "minute_budget",
        players=float(rules.players_on_court),
        regulation=float(rules.regulation_minutes),
        overtime=0.0,
    ).result
    fields = {f.id: f for f in params.fields}
    teams: dict[str, list[EffectivePlayer]] = defaultdict(list)
    for player in players:
        teams[player.player.team_id].append(player)
    allocated: dict[str, EffectivePlayer] = {}
    for _team, members in sorted(teams.items()):
        rows = allocate_team(
            members, budget, params.tolerance.value, fields, known_roles, back_to_back
        )
        allocated.update((p.player.id, p) for p in rows)
    return tuple(allocated[p.player.id] for p in players)


def allocate_team(
    members: list[EffectivePlayer],
    budget: float,
    tolerance: float,
    fields: dict[str, AdjustmentField],
    known_roles: set[str],
    back_to_back: set[str],
) -> tuple[EffectivePlayer, ...]:
    groups: dict[int, list[EffectivePlayer]] = defaultdict(list)
    for player in members:
        manual = any(
            "minutes" in fields[e.field].targets
            for e in player.adjustments
            if not fields[e.field].only_back_to_back or player.player.team_id in back_to_back
        )
        tier = 0 if manual else 1 if player.player.id in known_roles else 2
        groups[tier].append(player)
    remaining = budget
    allocated: list[EffectivePlayer] = []
    for tier, group in sorted(groups.items()):
        demand = evaluate(
            "linear",
            values=tuple(p.minutes for p in group),
            weights=tuple(p.probability for p in group),
        ).result
        if tier == 0 and demand > remaining + tolerance:
            team = members[0].player.team_id
            raise DataError(f"rotation.{team}: 手調預期分鐘超過全隊 {budget:g} 分鐘預算")
        fraction = evaluate("budget_fraction", budget=remaining, demand=demand)
        factor = 1.0 if tier == 0 else fraction.result
        for player in group:
            probability = evaluate("product", gain=player.probability, probability=factor)
            allocated.append(
                player.model_copy(
                    update={
                        "probability": probability.result,
                        "traces": {
                            **player.traces,
                            "rotation:budget": fraction,
                            "rotation:probability": probability,
                        },
                    }
                )
            )
        used = evaluate("product", gain=demand, probability=factor).result
        remaining = max(0.0, evaluate("difference", before=used, after=remaining).result)
    return tuple(allocated)
