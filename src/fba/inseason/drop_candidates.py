"""Prioritize low-value roster members from the already computed team plan."""

from datetime import date
from math import fsum

import numpy as np

from fba.contracts.inseason import InseasonPreferences
from fba.formulas.registry import evaluate
from fba.formulas.simulation import mean_array, nonnegative_samples
from fba.inseason.matchup import Simulation
from fba.inseason.priority import matchup_priority
from fba.inseason.season import season_forecasts


def prioritized_drops(
    sim: Simulation,
    preferences: InseasonPreferences,
    roster: tuple[str, ...],
    acquired_today: tuple[str, ...],
    on: date,
    *,
    exhaustive: bool = False,
) -> tuple[str, ...]:
    team = next(t for t in sim.snapshot.teams if t.id == sim.snapshot.mine)
    movable = tuple(
        p
        for p in roster
        if p not in preferences.untouchable
        and p not in acquired_today
        and not (p in team.selected_slots.values() and sim.locked(p, on))
    )
    if exhaustive or len(movable) <= sim.params.drop_shortlist.value:
        return movable
    engine = sim.season()
    values: dict[str, list[float]] = {p: [] for p in movable}
    # Frozen lineup contribution is a screening approximation. It reuses one
    # baseline; it never reoptimizes a whole season for each possible drop.
    week = next(w for w in sim.league.matchups if w.start <= on <= w.end)
    if matchup_priority(sim, week.id).status == "must_win":
        pair = next(
            p for p in sim.snapshot.pairings if p.week_id == week.id and team.id in (p.home, p.away)
        )
        opponent = pair.away if pair.home == team.id else pair.home
        forecasts = (engine.week(team.id, opponent, week.id),)
    else:
        forecasts = season_forecasts(sim, team.id, after=on, include_playoffs=True)
    for forecast in forecasts:
        own, other, lineups = engine.matchup_totals(team.id, forecast.away, forecast.week_id)
        _, through = engine.actual(team.id, forecast.week_id)
        contributions = {p: np.zeros_like(own) for p in movable}
        for day in lineups:
            if day.team_id != team.id or day.on < on:
                continue
            draws = engine.daily_draws(
                tuple(day.slots.values()), day.on, max(engine.as_of, through)
            )
            for p in set(movable).intersection(day.slots.values()):
                contributions[p] += draws[p]
        before = engine.calibrated_score(float(mean_array(engine.score(own, other)[1]))).result
        for p in movable:
            engine.check_limits()
            without = engine.calibrated_score(
                float(
                    mean_array(
                        engine.score(
                            nonnegative_samples({"values": own - contributions[p]}), other
                        )[1]
                    )
                )
            ).result
            values[p].append(evaluate("difference", after=before, before=without).result)
    return tuple(
        sorted(movable, key=lambda p: (round(fsum(values[p]) / sim.params.tolerance.value), p))[
            : sim.params.drop_shortlist.value
        ]
    )
