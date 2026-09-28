from math import log2
from typing import Protocol

import numpy as np
from scipy.special import expit
from scipy.stats import norm, qmc

from fba.contracts.auction import (
    FitStep,
    FitSummary,
    Infeasible,
    MarginalRequest,
    MarketResult,
    Plan,
    SolverError,
)
from fba.contracts.base import DataError
from fba.contracts.config import FitParameters
from fba.contracts.season import ManagementInput, MarginalFeature, MarginalTask, SeasonKernel
from fba.core.managed import FloatArray, ManagedSeason
from fba.core.portfolio import Portfolio
from fba.core.roster import capacity, completable
from fba.core.scoring import categories


class FeatureRunner(Protocol):
    def __call__(self, request: MarginalRequest) -> tuple[MarginalFeature, ...]: ...


def marginal_batch(
    manager: ManagedSeason, tasks: tuple[MarginalTask, ...]
) -> tuple[MarginalFeature, ...]:
    features: list[MarginalFeature] = []
    for task in tasks:
        difference = (
            manager.project((*task.rest, task.player)).boxes - manager.project(task.rest).boxes
        )
        features.append(
            MarginalFeature(
                index=task.index, values=tuple(float(v) for v in difference.mean(axis=(0, 1)))
            )
        )
    return tuple(features)


def matrix_root(covariance: FloatArray) -> FloatArray:
    values, vectors = np.linalg.eigh((covariance + covariance.T) / 2)
    return (vectors * np.sqrt(np.maximum(values, 0))) @ vectors.T


def opponent_rosters(
    portfolio: Portfolio, market: MarketResult, own_id: str, complete: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    by_id = {p.id: p for p in portfolio.players}
    prices = {p.player_id: p for p in market.prices}
    ordered = sorted(
        (
            p
            for p in portfolio.players
            if p.active
            and p.utility is not None
            and prices[p.id].anchor is not None
            and p.id not in complete
        ),
        key=lambda p: (-(prices[p.id].anchor or 0), -(p.fair or 0), p.id),
    )
    rosters = [list(t.owned) for t in market.room if t.id != own_id]
    used = set(complete) | {p for r in rosters for p in r}
    for turn in range(capacity(portfolio.league)):
        for roster in rosters if not turn % 2 else reversed(rosters):
            if len(roster) >= capacity(portfolio.league):
                continue
            candidate = next(
                (
                    p
                    for p in ordered
                    if p.id not in used
                    and completable(portfolio.league, tuple(by_id[i] for i in roster) + (p,))
                ),
                None,
            )
            if candidate is not None:
                roster.append(candidate.id)
                used.add(candidate.id)
    if any(len(r) != capacity(portfolio.league) for r in rosters):
        raise DataError("fit: cannot construct complete disjoint opponent benchmark")
    return tuple(tuple(r) for r in rosters)


class FittedUtility:
    def __init__(
        self,
        portfolio: Portfolio,
        market: MarketResult,
        own_id: str,
        parameters: FitParameters,
        management: ManagementInput,
        kernel: SeasonKernel,
        base: Plan,
    ) -> None:
        self.portfolio, self.market, self.own_id = portfolio, market, own_id
        self.parameters = parameters
        self.management = management
        self.manager = ManagedSeason(
            portfolio.league, parameters, management, portfolio.players, kernel
        )
        self.league = portfolio.league
        samples = int(log2(parameters.samples))
        if 2**samples != parameters.samples:
            raise DataError("fit.samples: must be a power of two")
        # Pair coordinates from one joint design; independently scrambled Sobol
        # sequences are not independent when their rows are paired.
        draws = norm.ppf(
            np.clip(
                qmc.Sobol(
                    self.manager.k * 2,
                    scramble=True,
                    seed=np.random.default_rng([parameters.seed, parameters.opponent_seed]),
                ).random_base2(samples),
                1e-12,
                1 - 1e-12,
            )
        )
        self.draws = draws[:, : self.manager.k]
        self.opponent_draws = draws[:, self.manager.k :]
        self.opponent = np.empty((0, 0))
        self.opponent_blocks = np.empty((0, 0, 0))
        self.scale = np.empty(0)
        self.initial_pool = self.waiver_pool(base)
        self.anchor = tuple(self.manager.index[i] for i in base.players)
        self.reference(base.players)

    def waiver_pool(self, base: Plan) -> tuple[int, ...]:
        opponents = opponent_rosters(self.portfolio, self.market, self.own_id, base.players)
        held = (
            set(base.players)
            | {i for r in opponents for i in r}
            | {i for t in self.market.room for i in t.owned}
        )
        prices = {p.player_id: p for p in self.market.prices}
        market_order = sorted(
            (p for p in self.portfolio.players if prices[p.id].expected is not None),
            key=lambda p: (-(prices[p.id].anchor or 0), -(p.fair or 0), p.id),
        )
        held.update(p.id for p in market_order[: sum(t.slots for t in self.market.room)])
        fair_order = sorted(
            (p for p in self.portfolio.players if p.active and p.utility is not None),
            key=lambda p: (-(p.fair or 0), p.id),
        )
        held.update(p.id for p in fair_order[: self.league.teams * capacity(self.league)])
        return tuple(
            self.manager.index[p.id]
            for p in self.portfolio.players
            if p.active and p.utility is not None and p.id not in held
        )

    def reference(self, complete: tuple[str, ...]) -> None:
        rosters = opponent_rosters(self.portfolio, self.market, self.own_id, complete)
        held = {self.manager.index[i] for r in rosters for i in r}
        self.manager.set_pool(tuple(i for i in self.initial_pool if i not in held))
        projected = self.manager.project_many(
            tuple(tuple(self.manager.index[i] for i in r) for r in rosters)
        )
        means = projected.mean.reshape(-1, self.manager.k)
        covariances = projected.covariance.reshape(-1, self.manager.k, self.manager.k)
        index = np.arange(len(self.draws)) % len(means)
        noise = np.empty_like(self.draws)
        for j, covariance in enumerate(covariances):
            selected = index == j
            noise[selected] = self.opponent_draws[selected] @ matrix_root(covariance).T
        self.opponent = categories(means[index] + noise, self.league, self.manager.stat_ids)
        blocks = np.array(
            [
                part.mean(axis=0).reshape(-1, self.manager.k)
                for part in np.array_split(projected.boxes, self.parameters.health_blocks)
            ]
        )
        self.opponent_blocks = categories(
            blocks[:, index] + noise[None], self.league, self.manager.stat_ids
        )
        floors = {p.id: p.value for p in self.parameters.category_floors}
        self.scale = np.maximum(
            self.opponent.std(axis=0), [floors[c.id] for c in self.league.categories]
        )

    def context(self, roster: tuple[int, ...]) -> tuple[FloatArray, FloatArray]:
        result = self.manager.project(roster)
        mean = result.mean.mean(axis=0)
        covariance = result.covariance.mean(axis=0) + np.cov(result.mean, rowvar=False, bias=True)
        return mean, self.draws @ matrix_root(covariance).T

    def score(self, mean: FloatArray, noise: FloatArray) -> float:
        difference = (
            categories(mean + noise, self.league, self.manager.stat_ids) - self.opponent
        ) / self.scale
        pivot = (len(self.league.categories) - 1) // 2
        return float(
            expit(
                np.partition(difference, pivot, axis=-1)[..., pivot] / self.parameters.bandwidth
            ).mean()
        )

    def block_scores(self, roster: tuple[int, ...], noise: FloatArray) -> FloatArray:
        boxes = self.manager.project(roster).boxes
        means = np.array(
            [
                part.mean(axis=(0, 1))
                for part in np.array_split(boxes, self.parameters.health_blocks)
            ]
        )
        difference = (
            categories(means[:, None] + noise[None], self.league, self.manager.stat_ids)
            - self.opponent_blocks
        ) / self.scale
        pivot = (len(self.league.categories) - 1) // 2
        return expit(
            np.partition(difference, pivot, axis=-1)[..., pivot] / self.parameters.bandwidth
        ).mean(axis=1)

    def gradient(self, mean: FloatArray, noise: FloatArray) -> FloatArray:
        step = np.maximum(
            np.std(mean + noise, axis=0) * self.parameters.gradient_fraction,
            self.parameters.gradient_floor,
        )
        gradient = np.zeros(self.manager.k)
        for k, h in enumerate(step):
            delta = np.zeros(self.manager.k)
            delta[k] = h
            gradient[k] = (self.score(mean + delta, noise) - self.score(mean - delta, noise)) / (
                2 * h
            )
        return gradient

    def marginals(
        self, base: Plan, gradient: FloatArray, mean: FloatArray, runner: FeatureRunner | None
    ) -> FloatArray:
        losses: list[tuple[int, float]] = []
        for pid in base.purchases:
            p = self.manager.index[pid]
            rest = self.manager.project(tuple(i for i in self.anchor if i != p))
            losses.append((p, float((mean - rest.mean.mean(axis=0)) @ gradient)))
        preferred = tuple(p for p, _ in sorted(losses, key=lambda pair: (pair[1], pair[0])))
        feature = np.zeros((len(self.portfolio.players), self.manager.k))
        tasks: list[MarginalTask] = []
        for i, player in enumerate(self.portfolio.players):
            if not player.active or player.utility is None or player.id in self.portfolio.owned:
                continue
            p = self.manager.index[player.id]
            q = (
                p
                if p in preferred
                else next(
                    (
                        q
                        for q in preferred
                        if len(self.manager.lineup(tuple(i for i in self.anchor if i != q) + (p,)))
                        == len(self.league.starter_slots)
                    ),
                    preferred[0],
                )
            )
            rest = tuple(i for i in self.anchor if i != q)
            tasks.append(MarginalTask(index=i, player=p, rest=rest))
        values = (
            marginal_batch(self.manager, tuple(tasks))
            if runner is None
            else runner(
                MarginalRequest(
                    league=self.league,
                    parameters=self.parameters,
                    management=self.management,
                    players=self.portfolio.players,
                    pool=self.manager.pool,
                    tasks=tuple(tasks),
                )
            )
        )
        for value in values:
            feature[value.index] = value.values
        return feature

    def solve(
        self, base: Plan, runner: FeatureRunner | None = None
    ) -> tuple[Portfolio, Plan, FitSummary]:
        mean, noise = self.context(self.anchor)
        gradient = self.gradient(mean, noise)
        score = self.score(mean, noise)
        block_scores = self.block_scores(self.anchor, noise)
        feature = self.marginals(base, gradient, mean, runner)
        utility = feature @ gradient
        scale = float(np.std(utility))
        if scale < 1e-12:
            raise DataError("fit: managed marginal utility has no variation")
        managed = utility / scale * np.std(self.portfolio.values)
        calls = self.portfolio.calls
        best = base
        selected = self.portfolio
        alpha = 0.0
        steps = [
            FitStep(step=0.0, score=score, accepted=True, block_minimum=0.0, block_maximum=0.0)
        ]
        for step in self.parameters.steps:
            vector = (1 - step) * self.portfolio.values + step * managed
            players = tuple(
                p.model_copy(update={"utility": float(v) if p.utility is not None else None})
                for p, v in zip(self.portfolio.players, vector, strict=True)
            )
            candidate = Portfolio(
                self.league,
                self.portfolio.parameters,
                players,
                self.portfolio.costs,
                self.portfolio.available,
                self.portfolio.owned,
                self.portfolio.budget,
                prune=self.portfolio.prune,
            )
            proposal = candidate.solve(canonical=True)
            calls += candidate.calls
            if isinstance(proposal, Infeasible):
                raise SolverError("fit: utility update changed feasibility")
            self.reference(proposal.players)
            roster = tuple(self.manager.index[i] for i in proposal.players)
            proposed_mean, proposed_noise = self.context(roster)
            proposed_score = self.score(proposed_mean, proposed_noise)
            blocks = self.block_scores(roster, proposed_noise)
            delta = blocks - block_scores
            accepted = proposed_score > score + self.parameters.improvement_tolerance and bool(
                np.all(delta > self.parameters.improvement_tolerance)
            )
            steps.append(
                FitStep(
                    step=step,
                    score=proposed_score,
                    accepted=accepted,
                    block_minimum=float(delta.min()),
                    block_maximum=float(delta.max()),
                )
            )
            if accepted:
                score, block_scores, best, selected, alpha = (
                    proposed_score,
                    blocks,
                    proposal,
                    candidate,
                    step,
                )
        selected.calls = calls
        return (
            selected,
            best,
            FitSummary(
                anchor=base.players,
                selected_step=alpha,
                steps=tuple(steps),
                samples=len(self.draws),
                health_samples=self.parameters.health_samples,
            ),
        )
