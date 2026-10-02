from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from fba.contracts.base import DataError
from fba.contracts.formula import FormulaTrace
from fba.contracts.inseason import CategoryForecast, WeekForecast
from fba.formulas.categories import category_evidence, category_values
from fba.formulas.registry import evaluate
from fba.formulas.simulation import mean_array, strict_win, variance_array
from fba.inseason.lineup_bounds import certified_loss
from fba.inseason.priority import matchup_priority

if TYPE_CHECKING:
    from fba.inseason.matchup import Array, Simulation


def category_changes(
    before: tuple[WeekForecast, ...], after: tuple[WeekForecast, ...]
) -> dict[str, dict[str, FormulaTrace]]:
    previous = {(w.week_id, c.id): c.probability for w in before for c in w.categories}
    changed = {(w.week_id, c.id): c.probability for w in after for c in w.categories}
    if previous.keys() != changed.keys():
        raise DataError("forecast.category_changes: before/after weeks or categories differ")
    return {
        week.week_id: {
            category.id: evaluate(
                "difference", before=previous[week.week_id, category.id], after=category.probability
            )
            for category in week.categories
        }
        for week in after
    }


def forecast_key(
    sim: Simulation,
    home: str,
    away: str,
    week_id: str,
    rosters: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, str, str, tuple[str, ...], tuple[str, ...]]:
    changed = rosters or {}
    return (
        home,
        away,
        week_id,
        tuple(sorted(changed.get(home, sim.roster(home)))),
        tuple(sorted(changed.get(away, sim.roster(away)))),
    )


def forecast_score(
    sim: Simulation,
    home: str,
    away: str,
    week: str,
    rosters: dict[str, tuple[str, ...]] | None = None,
) -> float:
    sim.check_limits()
    key = forecast_key(sim, home, away, week, rosters)
    if key in sim.forecast_cache:
        sim.forecast_cache.move_to_end(key)
        return sim.forecast_cache[key].score
    own, other = cached_matchup_points(sim, home, away, week, rosters)
    raw = (
        own
        if sim.league.scoring == "h2h_each_category"
        else strict_win({"own": own, "other": other})
    )
    return sim.calibrated_score(float(mean_array(raw))).result


def cached_matchup_points(
    sim: Simulation,
    home: str,
    away: str,
    week: str,
    rosters: dict[str, tuple[str, ...]] | None = None,
) -> tuple[Array, Array]:
    """Exact standings draws; never publish a points result as lineup details."""
    sim.check_limits()
    key = forecast_key(sim, home, away, week, rosters)
    if key not in sim.standings_point_cache:
        if key not in sim.matchup_cache and certified_loss(sim, home, away, week, rosters):
            points = np.zeros(sim.samples), np.ones(sim.samples)
        else:
            a, b, _ = sim.matchup_totals(home, away, week, rosters)
            points = sim.score(a, b, standings=True)[1], sim.score(b, a, standings=True)[1]
        sim.standings_point_cache[key] = points
        if len(sim.standings_point_cache) > sim.params.scenario_cache_entries.value:
            sim.standings_point_cache.popitem(last=False)
    sim.standings_point_cache.move_to_end(key)
    return sim.standings_point_cache[key]


def cached_forecast(
    sim: Simulation,
    home: str,
    away: str,
    week_id: str,
    rosters: dict[str, tuple[str, ...]] | None = None,
) -> WeekForecast:
    sim.check_limits()
    key = forecast_key(sim, home, away, week_id, rosters)
    if key not in sim.forecast_cache:
        sim.forecast_cache[key] = forecast(sim, home, away, week_id, rosters)
        if len(sim.forecast_cache) > sim.params.scenario_cache_entries.value:
            sim.forecast_cache.popitem(last=False)
    sim.forecast_cache.move_to_end(key)
    return sim.forecast_cache[key]


def forecast(
    sim: Simulation,
    home: str,
    away: str,
    week_id: str,
    rosters: dict[str, tuple[str, ...]] | None = None,
) -> WeekForecast:
    changed = rosters or {}
    priority = matchup_priority(sim, week_id)
    a, b, lineups = sim.matchup_totals(home, away, week_id, changed)
    points, scores = sim.score(a, b)
    raw = float(mean_array(scores))
    trace = sim.calibrated_score(raw)
    error = evaluate("error", variance=float(variance_array(scores)), samples=float(sim.samples))
    scaled_error = evaluate(
        "product",
        gain=error.result,
        probability=sim.params.week_calibration.value
        if sim.league.scoring == "h2h_one_win"
        else sim.params.calibration.value,
    )
    week = next(w for w in sim.league.matchups if w.id == week_id)
    today = sim.as_of.astimezone(sim.zone).date()
    team = next(t for t in sim.snapshot.teams if t.id == home)
    source_players = {p.player.id: p.player for p in sim.projection(today).players}
    remaining = {
        tid: sum(
            game.tipoff > sim.as_of
            for day in lineups
            if day.team_id == tid
            for pid in day.slots.values()
            for game in sim.games_on(source_players[pid].team_id, day.on)
        )
        for tid in (home, away)
    }
    no_moves_raw = raw
    if changed:
        baseline_a, baseline_b, _ = sim.matchup_totals(home, away, week_id)
        no_moves_raw = float(mean_array(sim.score(baseline_a, baseline_b)[1]))
    return WeekForecast(
        priority=priority,
        elapsed_days=max(0, min((today - week.start).days, (week.end - week.start).days + 1)),
        remaining_games=remaining,
        adds_remaining=max(0, sim.league.adds_per_week - team.adds_used)
        if team.adds_used is not None
        else None,
        no_moves_raw_score=no_moves_raw,
        no_moves_score=sim.calibrated_score(no_moves_raw).result,
        injury_returns=sim.injury_plan_cache.get(
            (home, tuple(sorted(changed.get(home, sim.roster(home))))), ()
        ),
        lineup_search=(
            "joint_exact"
            if (home, week_id, tuple(sorted(changed.get(home, sim.roster(home)))))
            in sim.joint_weeks
            else "daily_exact_coordinate"
        ),
        sample_coupling={
            team: "exchangeable_count_v1"
            if (team, week_id, tuple(sorted(changed.get(team, sim.roster(team)))))
            in sim.count_weeks
            else "game_id"
            for team in (home, away)
        },
        prior_players=tuple(
            sorted(
                {pid for day in lineups for pid in day.slots.values() if not sim.history.get(pid)}
            )
        ),
        week_id=week_id,
        home=home,
        away=away,
        scoring=sim.league.scoring,
        raw_score=raw,
        score=trace.result,
        standard_error=scaled_error.result,
        categories=tuple(
            category_forecast(a, b, points[:, i], i, sim) for i in range(len(sim.league.categories))
        ),
        lineups=lineups,
        simulations=sim.samples,
        opponent_policy="fixed_roster",
        traces=(trace, error, scaled_error),
    )


def category_forecast(
    a: Array, b: Array, points: Array, index: int, sim: Simulation
) -> CategoryForecast:
    category = sim.league.categories[index]
    means = np.stack((mean_array(a, axis=0), mean_array(b, axis=0)))
    values, numerator, denominator, value_traces = category_evidence(means, category, sim.axes)
    distribution_a = category_values(a, (category,), sim.axes)[:, 0]
    distribution_b = category_values(b, (category,), sim.axes)[:, 0]
    raw = float(mean_array(points))
    calibrated = evaluate("calibration", p=raw, c=sim.params.calibration.value)
    z = evaluate(
        "z",
        home=float(mean_array(distribution_a)),
        away=float(mean_array(distribution_b)),
        home_variance=float(variance_array(distribution_a)),
        away_variance=float(variance_array(distribution_b)),
        limit=1 / sim.params.tolerance.value,
    )
    normal = evaluate("normal", z=z.result)
    error = evaluate("error", variance=float(variance_array(points)), samples=float(sim.samples))
    scaled_error = evaluate("product", gain=error.result, probability=sim.params.calibration.value)
    p = calibrated.result
    return CategoryForecast(
        id=category.id,
        label=category.label,
        home=float(values[0]),
        away=float(values[1]),
        home_numerator=float(numerator[0]),
        away_numerator=float(numerator[1]),
        home_denominator=None if denominator is None else float(denominator[0]),
        away_denominator=None if denominator is None else float(denominator[1]),
        raw_probability=raw,
        probability=p,
        standard_error=scaled_error.result,
        z=z.result,
        normal_probability=normal.result,
        strategy="safe"
        if p >= sim.params.safe_probability.value
        else "abandon"
        if p <= sim.params.abandon_probability.value
        else "key",
        traces=(calibrated, z, normal, error, scaled_error),
        value_axes=sim.axes,
        value_traces=value_traces,
    )
