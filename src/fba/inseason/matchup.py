from collections import OrderedDict
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
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
    DayLineup,
    EffectiveProjection,
    FrozenPriors,
    InjuryReturn,
    InseasonLeague,
    InseasonParameters,
    LeagueSnapshot,
    MatchupPriority,
    PlayerSnapshot,
    SeasonGame,
    WeekForecast,
)
from fba.core.lineups import best_lineup
from fba.formulas.categories import (
    category_values,
    sample_scores,
    score_samples,
)
from fba.formulas.registry import evaluate
from fba.inseason.forecast import cached_forecast
from fba.inseason.lineup_bounds import assignment_ceiling
from fba.inseason.lineup_space import cached_subsets, ordered_row_sums
from fba.inseason.projection import (
    ProjectionIndex,
    effective_projection,
    observed_boxes,
    projection_profile_key,
    visible_games,
)
from fba.inseason.roster_timeline import RosterChange, merge_changes, roster_after
from fba.inseason.sampling import sample_game
from fba.inseason.weekly_lineups import joint_lineup

type Array = NDArray[np.float64]


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
        *,
        untouchable: tuple[str, ...] = (),
    ) -> None:
        if snapshot.as_of > as_of:
            raise DataError("league_snapshot.as_of: roster snapshot is from the future")
        if snapshot.league_id != league.league_id or players.season_id != league.season_id:
            raise DataError("simulation: snapshot identity or season differs from configuration")
        self.league, self.params, self.players, self.priors = league, params, players, priors
        self.ledger, self.snapshot, self.as_of = ledger, snapshot, as_of
        self.samples = samples if samples is not None else params.simulations.value
        self.untouchable = frozenset(untouchable)
        self.axes = (*league.base_stats, *(d.id for d in league.derived))
        self.transitions: dict[str, tuple[RosterChange, ...]] = {}
        self.projections: dict[date, EffectiveProjection] = {}
        self.projection_profiles: dict[tuple[tuple[str, ...], ...], EffectiveProjection] = {}
        self.player_index = ProjectionIndex()
        self.draws: dict[tuple[str, str], Array] = {}
        self.team_cache: OrderedDict[
            tuple[str, str, tuple[str, ...]], tuple[Array, tuple[DayLineup, ...]]
        ] = OrderedDict()
        self.expected_cache: OrderedDict[
            tuple[
                tuple[tuple[str, tuple[str, ...]], ...],
                tuple[tuple[str, tuple[str, ...], bytes], ...],
                tuple[bytes, ...],
            ],
            tuple[tuple[str, str], ...],
        ] = OrderedDict()
        self.forecast_cache: OrderedDict[
            tuple[str, str, str, tuple[str, ...], tuple[str, ...]], WeekForecast
        ] = OrderedDict()
        self.matchup_cache: OrderedDict[
            tuple[str, str, str, tuple[str, ...], tuple[str, ...]],
            tuple[Array, Array, tuple[DayLineup, ...]],
        ] = OrderedDict()
        self.acceptance_fit: dict[str, float] | None = None
        self.cancelled: Callable[[], bool] | None = None
        self.deadline: tuple[float, str] | None = None
        self.history = observed_boxes(players, as_of)
        self.zone = ZoneInfo(league.timezone)
        self.games = visible_games(players, as_of)
        self.indexed_games: tuple[SeasonGame, ...] | None = None
        self.indexed_zone: ZoneInfo | None = None
        self.schedule_index: dict[tuple[str, date], tuple[SeasonGame, ...]] = {}
        self.trade_search_counts: dict[str, int] = {}
        self.season_score_cache: OrderedDict[
            tuple[str, tuple[tuple[str, str], ...], tuple[tuple[str, tuple[str, ...]], ...]], float
        ] = OrderedDict()
        self.standings_point_cache: OrderedDict[
            tuple[str, str, str, tuple[str, ...], tuple[str, ...]], tuple[Array, Array]
        ] = OrderedDict()
        self.playoff_probabilities: dict[str, float] | None = None
        self.project_injury_returns = True
        self.injury_plan_cache: dict[tuple[str, tuple[str, ...]], tuple[InjuryReturn, ...]] = {}
        self.season_engine: Simulation | None = None
        self.joint_weeks: set[tuple[str, str, tuple[str, ...]]] = set()
        self.priority_cache: dict[str, MatchupPriority] = {}

    def season(self) -> "Simulation":
        """ROS has its own sample contract; never mix arrays of different sizes."""
        if self.samples == self.params.season_simulations.value:
            return self
        if self.season_engine is None:
            self.season_engine = Simulation(
                self.league,
                self.params,
                self.players,
                self.priors,
                self.ledger,
                self.snapshot,
                self.as_of,
                self.params.season_simulations.value,
                untouchable=tuple(self.untouchable),
            )
            self.season_engine.projections = self.projections
            self.season_engine.projection_profiles = self.projection_profiles
            self.season_engine.priority_cache = self.priority_cache
        child = self.season_engine
        child.project_injury_returns = self.project_injury_returns
        child.untouchable = self.untouchable
        child.transitions = self.transitions
        child.cancelled, child.deadline = self.cancelled, self.deadline
        child.acceptance_fit = self.acceptance_fit
        return child

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
            profile = projection_profile_key(
                self.players, self.ledger, self.params, self.games, self.zone, self.as_of, on
            )
            if profile not in self.projection_profiles:
                self.projection_profiles[profile] = effective_projection(
                    self.players, self.priors, self.ledger, self.league, self.params, self.as_of, on
                )
            self.projections[on] = self.projection_profiles[profile].model_copy(update={"on": on})
        return self.projections[on]

    def game_draw(self, player_id: str, game: SeasonGame) -> Array:
        key = (player_id, game.id)
        if key in self.draws:
            return self.draws[key]
        on = game.tipoff.astimezone(self.zone).date()
        player = self.player_index.get(self.projection(on))[player_id]
        result = sample_game(
            player,
            self.history.get(player_id, ()),
            self.league,
            self.params,
            self.priors,
            self.samples,
            game,
        )
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
        self.validate_actual_coverage(
            team_id, week_id, score.through, score.complete_through, score.final
        )
        return np.tile(
            np.array([score.totals.get(s, 0.0) for s in self.axes]), (self.samples, 1)
        ), score.through

    def validate_actual_coverage(
        self,
        team_id: str,
        week_id: str,
        through: datetime,
        complete_through: datetime | None,
        final: bool,
    ) -> None:
        if complete_through is not None and complete_through > through:
            raise DataError("actual.complete_through: coverage cannot exceed retrieval time")
        if final:
            return
        week = next(w for w in self.league.matchups if w.id == week_id)
        team = next(t for t in self.snapshot.teams if t.id == team_id)
        today = self.as_of.astimezone(self.zone).date()
        projections = {p.player.id: p.player for p in self.projection(today).players}
        roster_teams = {projections[pid].team_id for pid in team.players}
        selected_teams = {projections[pid].team_id for pid in team.selected_slots.values()}
        for game in self.games:
            on = game.tipoff.astimezone(self.zone).date()
            if not week.start <= on <= week.end or game.tipoff > self.as_of:
                continue
            if game.status == "completed" and roster_teams.intersection((game.home, game.away)):
                if game.known_at > through:
                    raise DataError(
                        f"actual.{team_id}.{week_id}: completed game {game.id} is newer than "
                        "Yahoo totals; synchronize confirmed scores before calculating"
                    )
                if complete_through is None or game.known_at > complete_through:
                    raise DataError(
                        f"actual.{team_id}.{week_id}: completed game {game.id} has no verified "
                        "Yahoo score coverage. Retrieval time does not prove credited stats; "
                        "forecast unavailable until complete coverage is confirmed"
                    )
            if (
                on == today
                and game.status != "completed"
                and selected_teams.intersection((game.home, game.away))
            ):
                raise DataError(
                    f"actual.{team_id}.{week_id}: game {game.id} has started; Yahoo weekly "
                    "totals do not identify partial coverage or remaining game statistics. "
                    "Forecast unavailable until completed-game scores are synchronized"
                )

    def games_on(self, nba_team: str, on: date) -> tuple[SeasonGame, ...]:
        if self.indexed_games is not self.games or self.indexed_zone != self.zone:
            rows: dict[tuple[str, date], list[SeasonGame]] = {}
            for game in self.games:
                day = game.tipoff.astimezone(self.zone).date()
                for team in dict.fromkeys((game.home, game.away)):
                    rows.setdefault((team, day), []).append(game)
            self.schedule_index = {key: tuple(games) for key, games in rows.items()}
            self.indexed_games, self.indexed_zone = self.games, self.zone
        return self.schedule_index.get((nba_team, on), ())

    def daily_draws(self, roster: tuple[str, ...], on: date, after: datetime) -> dict[str, Array]:
        players = self.player_index.get(self.projection(on))
        result: dict[str, Array] = {}
        for pid in roster:
            if pid not in players:
                raise DataError(f"roster.{pid}: no effective projection")
            games = tuple(
                g for g in self.games_on(players[pid].player.team_id, on) if g.tipoff > after
            )
            if len(games) == 1:
                result[pid] = np.add(np.float64(0), self.game_draw(pid, games[0]))
            elif games:
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

    def projected_roster(self, team: str, on: date, roster: tuple[str, ...]) -> tuple[str, ...]:
        roster = tuple(sorted(roster))
        returns: tuple[InjuryReturn, ...] = ()
        if self.project_injury_returns and next(
            t.injury_players for t in self.snapshot.teams if t.id == team
        ):
            from fba.inseason.injury_returns import return_plan

            key = team, roster
            if key not in self.injury_plan_cache:
                self.injury_plan_cache[key] = return_plan(self, team, roster)
            returns = self.injury_plan_cache[key]
        return roster_after(roster, merge_changes(self.transitions.get(team, ()), returns), on)

    def total(
        self, team_id: str, week_id: str, roster: tuple[str, ...] | None = None
    ) -> tuple[Array, tuple[DayLineup, ...]]:
        roster = tuple(sorted(roster if roster is not None else self.roster(team_id)))
        key = team_id, week_id, roster
        if key in self.team_cache:
            self.team_cache.move_to_end(key)
            return self.team_cache[key]
        week = next(w for w in self.league.matchups if w.id == week_id)
        total, through = self.actual(team_id, week_id)
        lineups: list[DayLineup] = []
        on = max(week.start, self.as_of.astimezone(self.zone).date())
        while on <= week.end:
            current_roster = self.projected_roster(team_id, on, roster)
            draws = self.daily_draws(current_roster, on, max(through, self.as_of))
            fixed, slots, positions = self.lineup_constraints(team_id, on, draws)
            means = {pid: draw.mean(axis=0) for pid, draw in draws.items()}

            # Optimize the whole-week samples below; only the deterministic
            # expected-stat starting assignment is reused here.
            assignment = self.expected_assignment(slots, positions, means, tuple(fixed.values()))
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
        if len(self.team_cache) > self.params.scenario_cache_entries.value:
            self.team_cache.popitem(last=False)
        return result

    def expected_assignment(
        self,
        slots: tuple[StarterSlot, ...],
        positions: dict[str, tuple[str, ...]],
        means: dict[str, Array],
        fixed: tuple[str, ...],
    ) -> dict[str, str]:
        self.check_limits()
        key = (
            tuple((slot.id, slot.eligible_positions) for slot in slots),
            tuple((pid, positions[pid], means[pid].tobytes()) for pid in sorted(positions)),
            tuple(means[pid].tobytes() for pid in fixed),
        )
        if key not in self.expected_cache:

            def objective(ids: tuple[str, ...]) -> float:
                box = sum((means[p] for p in (*fixed, *ids)), start=np.zeros(len(self.axes)))
                return float(category_values(box, self.league.categories, self.axes).sum())

            def objectives(rows: tuple[tuple[str, ...], ...]) -> tuple[float, ...]:
                boxes = ordered_row_sums(rows, means, (len(self.axes),), fixed)
                return tuple(
                    float(v)
                    for v in category_values(boxes, self.league.categories, self.axes).sum(axis=-1)
                )

            assignment, _ = best_lineup(
                slots,
                positions,
                objective,
                self.params.tolerance.value,
                batch_objective=objectives,
                batch_size=self.params.lineup_batch.value,
                subset_solver=cached_subsets,
            )
            self.expected_cache[key] = tuple(assignment.items())
            if len(self.expected_cache) > self.params.scenario_cache_entries.value:
                self.expected_cache.popitem(last=False)
        self.expected_cache.move_to_end(key)
        return dict(self.expected_cache[key])

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
        joint = joint_lineup(self, team, week, opponent_total, roster, initial, through)
        if joint is not None:
            return joint
        current = initial
        while True:
            result: list[DayLineup] = []
            for day in current:
                active = roster if roster is not None else self.roster(team)
                active = self.projected_roster(team, day.on, active)
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
        players = self.player_index.get(self.projection(on))
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

        def objectives(rows: tuple[tuple[str, ...], ...]) -> tuple[float, ...]:
            self.check_limits()
            if monotonic() - started > self.params.budgets["week"].value:
                raise CalculationTimeout("lineup: exact daily optimization exceeded time budget")
            totals = rest + ordered_row_sums(rows, draws, rest.shape, fixed_ids)
            scores = sample_scores(
                totals,
                opponent_total,
                self.league.categories,
                self.axes,
                self.league.scoring,
                self.league.category_ties,
                0.0,
            ).mean(axis=-1)
            return tuple(self.calibrated_score(float(v)).result for v in scores)

        assigned, value = best_lineup(
            slots,
            free,
            movable_objective,
            self.params.tolerance.value,
            required,
            batch_objective=objectives,
            batch_size=self.params.lineup_batch.value,
            subset_solver=cached_subsets,
            maximum=lambda achieved: assignment_ceiling(
                self, draws, fixed_ids, tuple(free), rest, opponent_total, achieved=achieved
            ),
        )
        return {**fixed, **assigned}, value

    def locked(self, pid: str, on: date) -> bool:
        if self.league.lineup_lock == "daily":
            return self.as_of >= datetime.combine(on, self.league.lineup_lock_time, self.zone)
        player = self.player_index.get(self.projection(on))[pid].player
        return any(g.tipoff <= self.as_of for g in self.games_on(player.team_id, on))

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
            tuple(sorted(changed.get(home, self.roster(home)))),
            tuple(sorted(changed.get(away, self.roster(away)))),
        )
        if key not in self.matchup_cache:
            other, opponent_days = self.total(away, week, changed.get(away))
            own, days = self.optimize_total(home, week, other, changed.get(home))
            self.matchup_cache[key] = (own, other, (*days, *opponent_days))
            if len(self.matchup_cache) > self.params.scenario_cache_entries.value:
                self.matchup_cache.popitem(last=False)
        self.matchup_cache.move_to_end(key)
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
            c=self.params.week_calibration.value
            if self.league.scoring == "h2h_one_win"
            else self.params.calibration.value,
        )

    def week(
        self, home: str, away: str, week_id: str, rosters: dict[str, tuple[str, ...]] | None = None
    ) -> WeekForecast:
        return cached_forecast(self, home, away, week_id, rosters)
