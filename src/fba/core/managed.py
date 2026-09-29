from datetime import date, timedelta
from typing import NamedTuple

import numpy as np
from numpy.typing import NDArray

from fba.contracts.auction import AuctionPlayer
from fba.contracts.base import DataError
from fba.contracts.config import FitParameters, LeagueRules, ManagementParameters, PricingParameters
from fba.contracts.season import (
    ManagementInput,
    ManagementPolicy,
    SeasonArrays,
    SeasonKernel,
    TacticalArrays,
)
from fba.core.roster import capacity, match_slots

type FloatArray = NDArray[np.float64]


class ManagedMoments(NamedTuple):
    mean: FloatArray
    covariance: FloatArray
    boxes: FloatArray


def management_calendar(league: LeagueRules, game_days: tuple[date, ...]) -> tuple[date, ...]:
    dates = sorted(set(game_days))
    if not dates:
        raise DataError("management.schedule: no scheduled games")
    first = dates[0]
    if league.lineup.lock_mode == "weekly" and league.lineup.lock_at == "period_start":
        periods = tuple(w for w in league.matchups if w.start <= first <= w.end)
        if len(periods) != 1:
            raise DataError("management.schedule: first game must belong to one matchup period")
        first = periods[0].start
    return tuple(first + timedelta(days=i) for i in range((dates[-1] - first).days + 1))


class ManagedSeason:
    """One invocation's conditional health integration; decisions see only today's status."""

    def __init__(
        self,
        league: LeagueRules,
        parameters: FitParameters,
        inputs: ManagementInput,
        players: tuple[AuctionPlayer, ...],
        kernel: SeasonKernel,
        known_health: NDArray[np.bool_] | None = None,
        pricing: PricingParameters | None = None,
        tactics: ManagementParameters | None = None,
    ) -> None:
        self.league, self.parameters, self.kernel = league, parameters, kernel
        if (pricing is None) != (tactics is None):
            raise DataError("management.pricing: policy and tactical parameters are both required")
        self.pricing, self.tactic_parameters = pricing, tactics
        self.pricing_tactics: TacticalArrays | None = None
        by_id = {p.id: p for p in inputs.players}
        if len(by_id) != len(inputs.players) or len(set(inputs.stat_ids)) != len(inputs.stat_ids):
            raise DataError("management: duplicate player or statistic axes")
        if len(set(inputs.sampling_ids)) != len(inputs.sampling_ids) or set(
            inputs.sampling_ids
        ) != set(by_id):
            raise DataError("management.sampling_ids: must uniquely cover players")
        self.ids = inputs.sampling_ids
        self.players = tuple(by_id[i] for i in self.ids)
        self.index = {p: i for i, p in enumerate(self.ids)}
        catalog = {p.id: p for p in players}
        if set(self.ids) != set(catalog) or len(catalog) != len(players):
            raise DataError("management.players: must match auction population")
        self.n = len(self.ids)
        self.k = len(inputs.stat_ids)
        self.stat_ids = inputs.stat_ids
        self.days = management_calendar(league, tuple(d for p in self.players for d in p.game_days))
        self.d = len(self.days)
        self.weeks = tuple(
            w for w in league.matchups if w.start <= self.days[-1] and w.end >= self.days[0]
        )
        if any(
            not any(w.start <= d <= w.end for w in periods)
            for periods in (self.weeks, league.transactions.add_periods)
            for d in self.days
        ):
            raise DataError("management.schedule: dates outside matchup or transaction periods")
        self.week = np.array(
            [next(i for i, w in enumerate(self.weeks) if w.start <= d <= w.end) for d in self.days],
            dtype=np.int32,
        )
        periods = league.transactions.add_periods
        self.period = np.array(
            [next(i for i, w in enumerate(periods) if w.start <= d <= w.end) for d in self.days],
            dtype=np.int32,
        )
        self.games = np.array(
            [[d in p.game_days for p in self.players] for d in self.days], dtype=bool
        )
        self.returns = np.array(
            [
                0 if p.return_on is None else sum(d < p.return_on for d in self.days)
                for p in self.players
            ],
            dtype=np.int32,
        )
        self.validate_players()
        original = np.array([p.means for p in self.players], dtype=float)
        covariance = np.array([p.covariance for p in self.players], dtype=float)
        gp = np.array([p.expected_games for p in self.players])
        health_gp = np.array([p.healthy_games for p in self.players])
        share = np.divide(gp, health_gp, out=np.ones(self.n), where=health_gp > 0)
        self.raw = original * share[:, None]
        self.cov = covariance * share[:, None, None] + np.einsum(
            "ni,nj,n->nij", original, original, share * (1 - share)
        )
        self.availability = np.array(
            [p.healthy_games / max(1, p.season_games) for p in self.players]
        )
        for i in range(self.n):
            if self.returns[i] > 0:
                self.availability[i] = min(
                    1.0, health_gp[i] / max(1, int(self.games[self.returns[i] :, i].sum()))
                )
        self.priority = np.array([catalog[i].utility or 0.0 for i in self.ids])
        self.value = self.priority / np.maximum(self.availability, parameters.availability_floor)
        if known_health is not None and (
            parameters.health_samples != 1 or known_health.shape != (1, self.d, self.n)
        ):
            raise DataError("management.health: observations require one matching health timeline")
        self.observed_available = (
            None if known_health is None else np.logical_or.accumulate(known_health[0], axis=0)
        )
        back, hurt = self.rates()
        decay = np.broadcast_to(1 - back - hurt, (self.d + 1, self.n)).copy()
        decay[0] = 1.0
        # Integer game-clock powers use ordered products, avoiding platform libm pow rounding.
        self.decay = np.cumprod(decay, axis=0)
        self.health = self.health_paths() if known_health is None else known_health.copy()
        if len(league.positions) > np.iinfo(np.uint64).bits:
            raise DataError("league.positions: exceeds native position mask width")
        bits = {position: 1 << i for i, position in enumerate(league.positions)}
        self.masks = np.array(
            [sum(bits[p] for p in catalog[i].positions) for i in self.ids], dtype=np.uint64
        )
        self.slots = np.array(
            [sum(bits[p] for p in slot.eligible_positions) for slot in league.starter_slots],
            dtype=np.uint64,
        )
        forecasts = np.array(
            [[self.forecast(d, status) for status in (False, True)] for d in range(self.d)]
        )
        ranked = (
            np.where(self.health, forecasts[None, :, 1, :], forecasts[None, :, 0, :]) * self.value
        )
        self.orders = np.argsort(-ranked, axis=-1, kind="stable").astype(np.int32)
        self.pool: tuple[int, ...] = ()
        self.cache: dict[tuple[tuple[int, ...], ...], ManagedMoments] = {}
        self.primary_cache: dict[tuple[tuple[int, ...], ...], ManagedMoments] = {}
        self.box_cache: dict[tuple[tuple[int, ...], ...], FloatArray] = {}
        self.lineups: dict[tuple[int, ...], tuple[int, ...]] = {}
        self.controls: dict[tuple[int, ...], tuple[FloatArray, FloatArray]] = {}
        self.control_means: dict[tuple[int, ...], tuple[FloatArray, FloatArray]] = {}

    def validate_players(self) -> None:
        for p in self.players:
            if not 0 <= p.expected_games <= p.healthy_games <= p.season_games:
                raise DataError(f"management.{p.id}: invalid playing/health GP decomposition")
            if (
                len(p.means) != self.k
                or len(p.covariance) != self.k
                or any(len(row) != self.k for row in p.covariance)
            ):
                raise DataError(f"management.{p.id}: inconsistent statistic axes")
            if len(set(p.game_days)) != len(p.game_days):
                raise DataError(f"management.{p.id}: duplicate game dates")

    def rates(self) -> tuple[FloatArray, FloatArray]:
        back = np.minimum(
            1 / self.parameters.mean_missed_games,
            self.availability / np.maximum(1 - self.availability, 1e-12),
        )
        hurt = (1 - self.availability) / np.maximum(self.availability, 1e-12) * back
        return back, hurt

    def health_paths(self) -> NDArray[np.bool_]:
        samples = self.parameters.health_samples
        rng = np.random.default_rng(self.parameters.health_seed)
        u = (
            np.arange(samples)[:, None, None] + rng.random((samples, self.d + 1, self.n))
        ) / samples
        for d in range(self.d + 1):
            for p in range(self.n):
                rng.shuffle(u[:, d, p])
        state = u[:, 0, :] < self.availability
        health = np.empty((samples, self.d, self.n), dtype=bool)
        back, hurt = self.rates()
        for d in range(self.d):
            if d:
                change = self.games[d - 1]
                state[:, change] = np.where(
                    state[:, change],
                    u[:, d][:, change] >= hurt[change],
                    u[:, d][:, change] < back[change],
                )
            state[:, d < self.returns] = False
            returning = (self.returns == d) & (self.returns > 0)
            state[:, returning] = u[:, d][:, returning] < self.availability[returning]
            state[:, self.availability == 0] = False
            health[:, d] = state
        return health

    def forecast(
        self, day: int, status: bool, horizon: int | None = None, eligible_from: int = 0
    ) -> FloatArray:
        end = min(self.d, day + (self.parameters.forecast_days if horizon is None else horizon))
        scheduled = self.games[day:end]
        counts = np.cumsum(scheduled, axis=0) - scheduled
        probabilities = (
            self.availability
            + (float(status) - self.availability) * self.decay[counts, np.arange(self.n)]
        )
        probabilities = np.clip(probabilities, 0, 1)
        pending = (self.returns > day) & (not status)
        if self.observed_available is not None:
            # Only the public prefix through this decision can retire a preseason estimate.
            pending &= ~self.observed_available[day]
        for j, d in enumerate(range(day, end)):
            probabilities[j, pending & (d < self.returns)] = 0
            after = pending & (self.returns <= d)
            probabilities[j, after] = self.availability[after]
        if eligible_from > day:
            probabilities[: eligible_from - day] = 0
        return (scheduled * probabilities).sum(axis=0)

    def tactics(self, policy: ManagementPolicy, parameters: ManagementParameters) -> TacticalArrays:
        days = np.arange(self.d)
        eligible = days + int(self.league.transactions.effective == "next_day")
        if self.league.lineup.lock_mode == "weekly":
            locks = np.append(np.flatnonzero(self.lock_days()), self.d)
            eligible = locks[np.searchsorted(locks, eligible)]
        values: list[FloatArray] = []
        for horizon in (self.parameters.forecast_days, parameters.long_forecast_days):
            for starts in (days, eligible):
                forecasts = np.array(
                    [
                        [
                            self.forecast(d, status, horizon, int(starts[d]))
                            for status in (False, True)
                        ]
                        for d in range(self.d)
                    ]
                )
                values.append(
                    np.where(self.health, forecasts[None, :, 1, :], forecasts[None, :, 0, :])
                    * self.value
                )
        short_held, short_acquired, long_held, long_acquired = values
        return TacticalArrays(
            policy=policy,
            short_values=short_held,
            long_values=long_held,
            acquired_short=short_acquired,
            acquired_long=long_acquired,
            short_orders=np.argsort(-short_acquired, axis=-1, kind="stable").astype(np.int32),
            long_orders=np.argsort(-long_acquired, axis=-1, kind="stable").astype(np.int32),
            candidate_limit=parameters.candidate_limit,
            minimum_gain=parameters.minimum_gain,
            opportunity_cost=parameters.opportunity_cost,
        )

    def lineup(self, roster: tuple[int, ...]) -> tuple[int, ...]:
        if roster in self.lineups:
            return self.lineups[roster]
        eligible = tuple(tuple(bool(self.masks[p] & mask) for mask in self.slots) for p in roster)
        order = tuple(
            sorted(range(len(roster)), key=lambda i: (-self.priority[roster[i]], roster[i]))
        )
        chosen, _ = match_slots(eligible, order, len(self.slots))
        result = tuple(roster[i] for i in chosen)
        self.lineups[roster] = result
        return result

    def control_schedule(self, roster: tuple[int, ...]) -> tuple[tuple[int, tuple[int, ...]], ...]:
        scheduled: list[tuple[int, tuple[int, ...]]] = []
        for d in range(self.d):
            use = self.lineup(tuple(p for p in roster if self.games[d, p] and d >= self.returns[p]))
            if use:
                scheduled.append((d, use))
        return tuple(scheduled)

    def control_mean(self, roster: tuple[int, ...]) -> tuple[FloatArray, FloatArray]:
        if roster in self.control_means:
            return self.control_means[roster]
        realised = np.zeros((self.parameters.health_samples, len(self.weeks), self.k))
        expected = np.zeros((len(self.weeks), self.k))
        for d, use in self.control_schedule(roster):
            w = int(self.week[d])
            ids = list(use)
            health = self.health[:, d][:, ids]
            realised[:, w] += health @ self.raw[ids]
            expected[w] += (self.raw[ids] * self.availability[ids, None]).sum(axis=0)
        self.control_means[roster] = (realised, expected)
        return realised, expected

    def control(self, roster: tuple[int, ...]) -> tuple[FloatArray, FloatArray]:
        if roster in self.controls:
            return self.controls[roster]
        realised, expected = self.control_mean(roster)
        correction = np.zeros((len(self.weeks), self.k, self.k))
        dates: dict[tuple[int, int], list[int]] = {}
        for d, use in self.control_schedule(roster):
            w = int(self.week[d])
            ids = list(use)
            health = self.health[:, d][:, ids]
            correction[w] += np.einsum(
                "n,nij->ij", self.availability[ids] - health.mean(axis=0), self.cov[ids]
            )
            for p in ids:
                dates.setdefault((w, p), []).append(d)
        clock = self.games.cumsum(axis=0)
        for (w, p), days in dates.items():
            ticks = clock[days, p]
            lags = np.abs(ticks[:, None] - ticks[None, :])
            variance = self.availability[p] * (1 - self.availability[p]) * self.decay[lags, p].sum()
            correction[w] += variance * np.outer(self.raw[p], self.raw[p])
        delta = realised - realised.mean(axis=0)
        correction -= np.einsum("swi,swj->wij", delta, delta) / max(
            1, self.parameters.health_samples - 1
        )
        result = (realised - expected, correction)
        self.controls[roster] = result
        return result

    def lock_days(self) -> NDArray[np.uint8]:
        locks = np.zeros(self.d, dtype=np.uint8)
        for week in range(len(self.weeks)):
            days = np.flatnonzero(self.week == week)
            if self.league.lineup.lock_at == "first_game":
                days = days[self.games[days].any(axis=1)]
            if len(days):
                locks[days[0]] = 1
        return locks

    def arrays(self, observed_eligibility: NDArray[np.uint8] | None = None) -> SeasonArrays:
        eligibility = [
            self.parameters.unavailable_status in group.eligible_statuses
            for group in self.league.injury_slots
            for _ in range(group.count)
        ]
        return SeasonArrays(
            health=np.ascontiguousarray(self.health, dtype=np.uint8),
            games=np.ascontiguousarray(self.games, dtype=np.uint8),
            weeks=self.week,
            periods=self.period,
            masks=self.masks,
            slots=self.slots,
            priority=self.priority,
            value=self.value,
            orders=self.orders,
            il_eligible=(
                np.tile(np.array(eligibility, dtype=np.uint8), (self.d, self.n, 1))
                if observed_eligibility is None
                else observed_eligibility
            ),
            lock_days=self.lock_days(),
            waiver_days=self.league.transactions.waiver_days,
            add_limit=self.league.transactions.adds_per_period,
            next_day=self.league.transactions.effective == "next_day",
            weekly_lock=self.league.lineup.lock_mode == "weekly",
            roster_capacity=capacity(self.league),
            week_count=len(self.weeks),
        )

    def project(self, roster: tuple[int, ...]) -> ManagedMoments:
        result = self.project_many((roster,))
        return ManagedMoments(result.mean[0], result.covariance[0], result.boxes[:, 0])

    def project_many(self, rosters: tuple[tuple[int, ...], ...]) -> ManagedMoments:
        rosters = tuple(tuple(sorted(r)) for r in rosters)
        owned = tuple(p for r in rosters for p in r)
        if len(set(owned)) != len(owned) or any(len(r) > capacity(self.league) for r in rosters):
            raise DataError("management.roster: duplicate players or capacity exceeded")
        if rosters in self.cache:
            return self.cache[rosters]
        counts = self.run_counts(rosters)
        result = self.summarize(rosters, counts)
        self.cache[rosters] = result
        return result

    def project_primary(self, rosters: tuple[tuple[int, ...], ...]) -> ManagedMoments:
        rosters = tuple(tuple(sorted(r)) for r in rosters)
        if rosters in self.cache:
            result = self.cache[rosters]
            return ManagedMoments(result.mean[0], result.covariance[0], result.boxes[:, 0])
        if rosters not in self.primary_cache:
            counts = self.run_counts(rosters, primary_only=True)
            result = self.summarize(rosters[:1], counts[:, :1])
            self.primary_cache[rosters] = ManagedMoments(
                result.mean[0], result.covariance[0], result.boxes[:, 0]
            )
        return self.primary_cache[rosters]

    def project_primary_boxes(self, rosters: tuple[tuple[int, ...], ...]) -> FloatArray:
        rosters = tuple(tuple(sorted(r)) for r in rosters)
        if rosters in self.cache:
            return self.cache[rosters].boxes[:, 0]
        if rosters in self.primary_cache:
            return self.primary_cache[rosters].boxes
        if rosters not in self.box_cache:
            counts = self.run_counts(rosters, primary_only=True)
            realised, expected = self.control_mean(rosters[0])
            self.box_cache[rosters] = counts[:, 0] @ self.raw - (realised - expected)
        return self.box_cache[rosters]

    def run_counts(
        self, rosters: tuple[tuple[int, ...], ...], *, primary_only: bool = False
    ) -> FloatArray:
        if self.pricing is not None and self.tactic_parameters is not None:
            policy = ManagementPolicy(
                streaming_slots=tuple(min(self.pricing.streaming_slots, len(r)) for r in rosters),
                reserve_adds=self.tactic_parameters.reserve_adds,
                upgrades=self.pricing.upgrades,
            )
            if self.pricing_tactics is None:
                self.pricing_tactics = self.tactics(policy, self.tactic_parameters)
            tactics = self.pricing_tactics.with_policy(policy)
            counts = self.kernel.run(
                self.arrays(), rosters, self.pool, tactics, False, primary_only=primary_only
            ).counts
        else:
            counts = self.kernel(self.arrays(), rosters, self.pool)
        return counts

    def summarize(self, rosters: tuple[tuple[int, ...], ...], counts: FloatArray) -> ManagedMoments:
        physical = counts @ self.raw
        controls = [self.control(r) for r in rosters]
        boxes = physical - np.stack([c[0] for c in controls], axis=1)
        mean = boxes.mean(axis=0)
        within = np.einsum("twn,nij->twij", counts.mean(axis=0), self.cov)
        delta = physical - physical.mean(axis=0)
        covariance = (
            within
            + np.einsum("stwi,stwj->twij", delta, delta)
            / max(1, self.parameters.health_samples - 1)
            + np.stack([c[1] for c in controls])
        )
        return ManagedMoments(mean, covariance, boxes)

    def set_pool(self, pool: tuple[int, ...]) -> None:
        if pool != self.pool:
            self.pool = pool
            self.cache.clear()
            self.primary_cache.clear()
            self.box_cache.clear()
