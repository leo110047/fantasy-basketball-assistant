from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from hashlib import sha256
from time import monotonic
from zoneinfo import ZoneInfo

import numpy as np
from numpy.typing import NDArray

from fba.contracts.base import DataError
from fba.contracts.config import Linear, StarterSlot
from fba.contracts.formula import FormulaTrace
from fba.contracts.inseason import (
    AdjustmentLedger,
    CalculationTimeout,
    CategoryForecast,
    DayLineup,
    EffectiveProjection,
    FrozenPriors,
    InseasonLeague,
    InseasonParameters,
    LeagueSnapshot,
    PlayerSnapshot,
    SeasonGame,
    WeekForecast,
)
from fba.core.lineups import best_lineup
from fba.formulas.arrays import evaluate_array
from fba.formulas.categories import category_values, derive_games, score_samples, total_terms
from fba.formulas.registry import evaluate
from fba.inseason.projection import effective_projection, observed_boxes, visible_games

type Array = NDArray[np.float64]


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


class Simulation:
    def __init__(
        self,
        league: InseasonLeague,
        params: InseasonParameters,
        players: PlayerSnapshot,
        priors: FrozenPriors,
        ledger: AdjustmentLedger,
        snapshot: LeagueSnapshot,
        as_of: datetime,
        samples: int | None = None,
    ) -> None:
        if snapshot.as_of > as_of:
            raise DataError("league_snapshot.as_of: roster snapshot is from the future")
        if snapshot.league_id != league.league_id or players.season_id != league.season_id:
            raise DataError("simulation: snapshot identity or season differs from configuration")
        self.league, self.params, self.players, self.priors = league, params, players, priors
        self.ledger, self.snapshot, self.as_of = ledger, snapshot, as_of
        self.samples = samples if samples is not None else params.simulations.value
        self.axes = (*league.base_stats, *(d.id for d in league.derived))
        self.transitions: dict[str, tuple[tuple[date, tuple[str, ...]], ...]] = {}
        self.projections: dict[date, EffectiveProjection] = {}
        self.draws: dict[tuple[str, str], Array] = {}
        self.team_cache: dict[
            tuple[str, str, tuple[str, ...]], tuple[Array, tuple[DayLineup, ...]]
        ] = {}
        self.matchup_cache: dict[
            tuple[str, str, str, tuple[str, ...], tuple[str, ...]],
            tuple[Array, Array, tuple[DayLineup, ...]],
        ] = {}
        self.acceptance_fit: dict[str, float] | None = None
        self.cancelled: Callable[[], bool] | None = None
        self.deadline: tuple[float, str] | None = None
        self.history = observed_boxes(players, as_of)
        self.zone = ZoneInfo(league.timezone)
        self.games = visible_games(players, as_of)

    def check_limits(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise CalculationTimeout("calculation: cancelled during shutdown")
        if self.deadline is not None and monotonic() >= self.deadline[0]:
            raise CalculationTimeout(
                f"{self.deadline[1]}: time budget exceeded; no partial result published"
            )

    @contextmanager
    def budget(self, name: str) -> Generator[None]:
        previous = self.deadline
        limit = (monotonic() + self.params.budgets[name].value, name)
        self.deadline = min(previous, limit) if previous is not None else limit
        try:
            yield
            self.check_limits()
        finally:
            self.deadline = previous

    def projection(self, on: date) -> EffectiveProjection:
        self.check_limits()
        if on not in self.projections:
            self.projections[on] = effective_projection(
                self.players, self.priors, self.ledger, self.league, self.params, self.as_of, on
            )
        return self.projections[on]

    def game_draw(self, player_id: str, game: SeasonGame) -> Array:
        key = (player_id, game.id)
        if key in self.draws:
            return self.draws[key]
        on = game.tipoff.astimezone(self.zone).date()
        player = next(p for p in self.projection(on).players if p.player.id == player_id)
        history = self.history.get(player_id, ())
        if not history:
            raise DataError(
                f"simulation.{player_id}: no historical single-game samples; acquire game logs"
            )
        seed = int.from_bytes(
            sha256(f"{self.params.seed.value}:{player_id}:{game.id}".encode()).digest()[:8],
            "little",
        )
        rng = np.random.default_rng(seed)
        source = np.array([[b.stats.get(s, 0.0) for s in self.league.base_stats] for b in history])
        sampled = source[rng.integers(len(source), size=self.samples)].copy()
        mean = source.mean(axis=0)
        target = np.array(
            [player.rates.get(s, 0.0) * player.minutes for s in self.league.base_stats]
        )
        # A zero-observation statistic cannot be rescaled. The explicit prior
        # predictive Poisson component preserves its positive model mean.
        zero_mean = np.zeros_like(sampled)
        for i in np.flatnonzero(mean == 0):
            zero_mean[:, i] = rng.poisson(target[i], size=self.samples)
        sampled = evaluate_array(
            "bootstrap_scale",
            history=source,
            samples=sampled,
            target=target,
            zero_mean_draws=zero_mean,
        ).result.copy()
        for shot in self.league.shots:
            i, j = (
                self.league.base_stats.index(shot.made),
                self.league.base_stats.index(shot.attempted),
            )
            sampled[:, i] = np.minimum(sampled[:, i], sampled[:, j])
        sampled *= (rng.random(self.samples) < player.probability)[:, None]
        result = derive_games(sampled, self.league.base_stats, self.league.derived)
        self.draws[key] = result
        return result

    def actual(self, team_id: str, week_id: str) -> tuple[Array, datetime]:
        scores = tuple(
            s
            for s in self.snapshot.actual
            if s.team_id == team_id and s.week_id == week_id and s.through <= self.as_of
        )
        week = next(w for w in self.league.matchups if w.id == week_id)
        if not scores:
            if week.start <= self.as_of.astimezone(self.zone).date():
                raise DataError(
                    f"actual.{team_id}.{week_id}: Yahoo current-week totals are required"
                )
            return np.zeros((self.samples, len(self.axes))), self.as_of
        score = max(scores, key=lambda s: s.through)
        needed = {
            t.stat_id
            for c in self.league.categories
            for t in (
                c.formula.terms
                if isinstance(c.formula, Linear)
                else (*c.formula.numerator, *c.formula.denominator)
            )
        }
        missing = needed - score.totals.keys()
        if missing:
            raise DataError(f"actual.{team_id}.{week_id}: missing base totals {sorted(missing)}")
        return np.tile(
            np.array([score.totals.get(s, 0.0) for s in self.axes]), (self.samples, 1)
        ), score.through

    def daily_draws(self, roster: tuple[str, ...], on: date, after: datetime) -> dict[str, Array]:
        players = {p.player.id: p for p in self.projection(on).players}
        result: dict[str, Array] = {}
        for pid in roster:
            if pid not in players:
                raise DataError(f"roster.{pid}: no effective projection")
            games = tuple(
                g
                for g in self.games
                if g.tipoff > after
                and g.tipoff.astimezone(self.zone).date() == on
                and players[pid].player.team_id in (g.home, g.away)
            )
            if games:
                result[pid] = sum(
                    (self.game_draw(pid, g) for g in games),
                    start=np.zeros((self.samples, len(self.axes))),
                )
            elif on == self.as_of.astimezone(self.zone).date() and self.locked(pid, on):
                # Yahoo actual totals already own this player's recorded stats.
                # A zero remaining draw still occupies the immutable lineup slot.
                result[pid] = np.zeros((self.samples, len(self.axes)))
        return result

    def roster(self, team_id: str) -> tuple[str, ...]:
        return next(t.players for t in self.snapshot.teams if t.id == team_id)

    def total(
        self, team_id: str, week_id: str, roster: tuple[str, ...] | None = None
    ) -> tuple[Array, tuple[DayLineup, ...]]:
        roster = tuple(sorted(roster if roster is not None else self.roster(team_id)))
        key = team_id, week_id, roster
        if key in self.team_cache:
            return self.team_cache[key]
        week = next(w for w in self.league.matchups if w.id == week_id)
        total, through = self.actual(team_id, week_id)
        lineups: list[DayLineup] = []
        on = max(week.start, self.as_of.astimezone(self.zone).date())
        while on <= week.end:
            current_roster = roster
            for effective, changed_roster in self.transitions.get(team_id, ()):
                if effective <= on:
                    current_roster = changed_roster
            draws = self.daily_draws(current_roster, on, max(through, self.as_of))
            fixed, slots, positions = self.lineup_constraints(team_id, on, draws)

            # Initial plan is a deterministic, legal expected-category lineup.
            # optimize_day below evaluates the actual whole-week objective.
            def objective(
                ids: tuple[str, ...],
                draws: dict[str, Array] = draws,
                fixed_ids: tuple[str, ...] = tuple(fixed.values()),
            ) -> float:
                box = sum(
                    (draws[p].mean(axis=0) for p in (*fixed_ids, *ids)),
                    start=np.zeros(len(self.axes)),
                )
                return float(category_values(box, self.league.categories, self.axes).sum())

            assignment, _ = best_lineup(slots, positions, objective, self.params.tolerance.value)
            assignment = {**fixed, **assignment}
            total += sum((draws[p] for p in assignment.values()), start=np.zeros_like(total))
            lineups.append(
                DayLineup(
                    on=on,
                    team_id=team_id,
                    slots=assignment,
                    bench=tuple(p for p in current_roster if p not in assignment.values()),
                )
            )
            on += timedelta(days=1)
        result = total, tuple(lineups)
        self.team_cache[key] = result
        return result

    def score(self, home: Array, away: Array, *, standings: bool = False) -> tuple[Array, Array]:
        return score_samples(
            home,
            away,
            self.league.categories,
            self.axes,
            self.league.scoring,
            self.league.category_ties,
            self.league.week_tie_value if standings else 0.0,
        )

    def optimize_total(
        self,
        team: str,
        week: str,
        opponent_total: Array,
        roster: tuple[str, ...] | None = None,
    ) -> tuple[Array, tuple[DayLineup, ...]]:
        own, initial = self.total(team, week, roster)
        own = own.copy()
        _, through = self.actual(team, week)
        started = monotonic()
        current = initial
        while True:
            result: list[DayLineup] = []
            for day in current:
                active = roster if roster is not None else self.roster(team)
                for effective, changed in self.transitions.get(team, ()):
                    if effective <= day.on:
                        active = changed
                draws = self.daily_draws(active, day.on, max(self.as_of, through))
                rest = own - sum((draws[p] for p in day.slots.values()), start=np.zeros_like(own))
                assignment, _ = self.optimize_assignment(
                    team, day.on, draws, rest, opponent_total, started
                )
                own = rest + sum((draws[p] for p in assignment.values()), start=np.zeros_like(rest))
                result.append(
                    DayLineup(
                        on=day.on,
                        team_id=team,
                        slots=assignment,
                        bench=tuple(p for p in active if p not in assignment.values()),
                    )
                )
            updated = tuple(result)
            if updated == current:
                return own, updated
            current = updated

    def lineup_constraints(
        self, team: str, on: date, draws: dict[str, Array]
    ) -> tuple[dict[str, str], tuple[StarterSlot, ...], dict[str, tuple[str, ...]]]:
        players = {p.player.id: p for p in self.projection(on).players}
        roster = next(t for t in self.snapshot.teams if t.id == team)
        locked = {pid for pid in draws if self.locked(pid, on)}
        fixed = {slot: pid for slot, pid in roster.selected_slots.items() if pid in locked}
        slots = tuple(s for s in self.league.starter_slots if s.id not in fixed)
        free = {p: players[p].player.positions for p in draws if p not in locked}
        return fixed, slots, free

    def optimize_assignment(
        self,
        team: str,
        on: date,
        draws: dict[str, Array],
        rest: Array,
        opponent_total: Array,
        started: float,
        required: tuple[str, ...] = (),
        excluded: tuple[str, ...] = (),
    ) -> tuple[dict[str, str], float]:
        def objective(ids: tuple[str, ...]) -> float:
            self.check_limits()
            if monotonic() - started > self.params.budgets["week"].value:
                raise CalculationTimeout("lineup: exact daily optimization exceeded time budget")
            total = rest + sum((draws[p] for p in ids), start=np.zeros_like(rest))
            return self.calibrated_score(float(self.score(total, opponent_total)[1].mean())).result

        fixed, slots, free = self.lineup_constraints(team, on, draws)
        fixed_ids = tuple(fixed.values())
        free = {p: positions for p, positions in free.items() if p not in excluded}

        def movable_objective(ids: tuple[str, ...]) -> float:
            return objective((*fixed_ids, *ids))

        assigned, value = best_lineup(
            slots, free, movable_objective, self.params.tolerance.value, required
        )
        return {**fixed, **assigned}, value

    def locked(self, pid: str, on: date) -> bool:
        if self.league.lineup_lock == "daily":
            return self.as_of >= datetime.combine(on, self.league.lineup_lock_time, self.zone)
        player = next(p.player for p in self.projection(on).players if p.player.id == pid)
        return any(
            g.tipoff <= self.as_of
            and g.tipoff.astimezone(self.zone).date() == on
            and player.team_id in (g.home, g.away)
            for g in self.games
        )

    def matchup_totals(
        self,
        home: str,
        away: str,
        week: str,
        rosters: dict[str, tuple[str, ...]] | None = None,
    ) -> tuple[Array, Array, tuple[DayLineup, ...]]:
        changed = rosters or {}
        # The opponent keeps a deterministic legal baseline lineup. Optimize
        # our daily choices against that fixed forecast, including future days.
        # Revisiting days until stable makes F3 and F5 use the same conditional
        # whole-week optimum; it does not claim a global multi-day optimum.
        key = (
            home,
            away,
            week,
            changed.get(home, self.roster(home)),
            changed.get(away, self.roster(away)),
        )
        if key not in self.matchup_cache:
            other, opponent_days = self.total(away, week, changed.get(away))
            own, days = self.optimize_total(home, week, other, changed.get(home))
            self.matchup_cache[key] = (own, other, (*days, *opponent_days))
        return self.matchup_cache[key]

    def optimize_day(
        self, team: str, week: str, on: date, opponent: str, roster: tuple[str, ...] | None = None
    ) -> tuple[DayLineup, float, float]:
        changed = {team: roster} if roster is not None else None
        own, other, days = self.matchup_totals(team, opponent, week, changed)
        lineup = next((d for d in days if d.on == on and d.team_id == team), None)
        if lineup is None:
            raise DataError("today.date: outside remaining matchup dates")
        initial, _ = self.total(team, week, roster)
        return (
            lineup,
            self.calibrated_score(float(self.score(initial, other)[1].mean())).result,
            self.calibrated_score(float(self.score(own, other)[1].mean())).result,
        )

    def calibrated_score(self, raw: float) -> FormulaTrace:
        return evaluate(
            "score_calibration",
            score=raw,
            scale=1.0
            if self.league.scoring == "h2h_one_win"
            else float(len(self.league.categories)),
            c=self.params.calibration.value,
        )

    def week(
        self, home: str, away: str, week_id: str, rosters: dict[str, tuple[str, ...]] | None = None
    ) -> WeekForecast:
        changed = rosters or {}
        a, b, lineups = self.matchup_totals(home, away, week_id, changed)
        points, scores = self.score(a, b)
        raw = float(scores.mean())
        trace = self.calibrated_score(raw)
        error = evaluate("error", variance=float(scores.var()), samples=float(self.samples))
        scaled_error = evaluate(
            "product", gain=error.result, probability=self.params.calibration.value
        )
        return WeekForecast(
            week_id=week_id,
            home=home,
            away=away,
            scoring=self.league.scoring,
            raw_score=raw,
            score=trace.result,
            standard_error=scaled_error.result,
            categories=tuple(
                category_forecast(a, b, points[:, i], i, self)
                for i in range(len(self.league.categories))
            ),
            lineups=lineups,
            simulations=self.samples,
            opponent_policy="fixed_roster",
            traces=(trace, error, scaled_error),
        )


def category_forecast(
    a: Array, b: Array, points: Array, index: int, sim: Simulation
) -> CategoryForecast:
    category = sim.league.categories[index]
    means = np.stack((a.mean(axis=0), b.mean(axis=0)))
    values = category_values(means, (category,), sim.axes, directed=False)[:, 0]
    distribution_a = category_values(a, (category,), sim.axes)[:, 0]
    distribution_b = category_values(b, (category,), sim.axes)[:, 0]
    raw = float(points.mean())
    calibrated = evaluate("calibration", p=raw, c=sim.params.calibration.value)
    z = evaluate(
        "z",
        home=float(distribution_a.mean()),
        away=float(distribution_b.mean()),
        home_variance=float(distribution_a.var()),
        away_variance=float(distribution_b.var()),
        limit=1 / sim.params.tolerance.value,
    )
    normal = evaluate("normal", z=z.result)
    error = evaluate("error", variance=float(points.var()), samples=float(sim.samples))
    scaled_error = evaluate("product", gain=error.result, probability=sim.params.calibration.value)
    formula = category.formula
    numerator = total_terms(
        means, formula.terms if isinstance(formula, Linear) else formula.numerator, sim.axes
    )
    denominator = (
        None if isinstance(formula, Linear) else total_terms(means, formula.denominator, sim.axes)
    )
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
    )
