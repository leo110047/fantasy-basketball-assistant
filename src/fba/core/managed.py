from datetime import date, timedelta
from typing import NamedTuple

import numpy as np
from numpy.typing import NDArray

from fba.contracts.auction import AuctionPlayer
from fba.contracts.base import DataError
from fba.contracts.config import FitParameters, LeagueRules
from fba.contracts.season import ManagementInput, SeasonArrays, SeasonKernel
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
    ) -> None:
        self.league, self.parameters, self.kernel = league, parameters, kernel
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
        self.health = self.health_paths()
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
        self.lineups: dict[tuple[int, ...], tuple[int, ...]] = {}
        self.controls: dict[tuple[int, ...], tuple[FloatArray, FloatArray]] = {}

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

    def forecast(self, day: int, status: bool) -> FloatArray:
        end = min(self.d, day + self.parameters.forecast_days)
        scheduled = self.games[day:end]
        counts = np.cumsum(scheduled, axis=0) - scheduled
        back, hurt = self.rates()
        probabilities = self.availability + (float(status) - self.availability) * np.power(
            1 - back - hurt, counts
        )
        probabilities = np.clip(probabilities, 0, 1)
        for j, d in enumerate(range(day, end)):
            probabilities[j, d < self.returns] = 0
            after = (self.returns > day) & (self.returns <= d)
            probabilities[j, after] = self.availability[after]
        return (scheduled * probabilities).sum(axis=0)

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

    def control(self, roster: tuple[int, ...]) -> tuple[FloatArray, FloatArray]:
        if roster in self.controls:
            return self.controls[roster]
        realised = np.zeros((self.parameters.health_samples, len(self.weeks), self.k))
        expected = np.zeros((len(self.weeks), self.k))
        correction = np.zeros((len(self.weeks), self.k, self.k))
        dates: dict[tuple[int, int], list[int]] = {}
        for d in range(self.d):
            use = self.lineup(tuple(p for p in roster if self.games[d, p] and d >= self.returns[p]))
            if not use:
                continue
            w = int(self.week[d])
            ids = list(use)
            health = self.health[:, d][:, ids]
            realised[:, w] += health @ self.raw[ids]
            expected[w] += (self.raw[ids] * self.availability[ids, None]).sum(axis=0)
            correction[w] += np.einsum(
                "n,nij->ij", self.availability[ids] - health.mean(axis=0), self.cov[ids]
            )
            for p in ids:
                dates.setdefault((w, p), []).append(d)
        back, hurt = self.rates()
        clock = self.games.cumsum(axis=0)
        for (w, p), days in dates.items():
            ticks = clock[days, p]
            lags = np.abs(ticks[:, None] - ticks[None, :])
            variance = (
                self.availability[p]
                * (1 - self.availability[p])
                * np.power(1 - back[p] - hurt[p], lags).sum()
            )
            correction[w] += variance * np.outer(self.raw[p], self.raw[p])
        delta = realised - realised.mean(axis=0)
        correction -= np.einsum("swi,swj->wij", delta, delta) / max(
            1, self.parameters.health_samples - 1
        )
        result = (realised - expected, correction)
        self.controls[roster] = result
        return result

    def arrays(self) -> SeasonArrays:
        eligibility = [
            self.parameters.unavailable_status in group.eligible_statuses
            for group in self.league.injury_slots
            for _ in range(group.count)
        ]
        locks = np.zeros(self.d, dtype=np.uint8)
        for week in range(len(self.weeks)):
            days = np.flatnonzero(self.week == week)
            if self.league.lineup.lock_at == "first_game":
                days = days[self.games[days].any(axis=1)]
            if len(days):
                locks[days[0]] = 1
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
            il_eligible=np.tile(np.array(eligibility, dtype=np.uint8), (self.n, 1)),
            lock_days=locks,
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
        counts = self.kernel(self.arrays(), rosters, self.pool)
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
        result = ManagedMoments(mean, covariance, boxes)
        self.cache[rosters] = result
        return result

    def set_pool(self, pool: tuple[int, ...]) -> None:
        if pool != self.pool:
            self.pool = pool
            self.cache.clear()
