from math import log2
from typing import Protocol

import numpy as np
from scipy.stats import qmc

from fba.auction.managed import FloatArray, ManagedMoments, ManagedSeason
from fba.auction.portfolio import Portfolio
from fba.contracts.auction import (
    AuctionPlayer,
    FitDiagnostics,
    FitStep,
    FitSummary,
    FittedPlayer,
    Infeasible,
    ManagedFitSummary,
    MarginalRequest,
    MarketResult,
    Plan,
    SolverError,
)
from fba.contracts.base import DataError
from fba.contracts.config import FitParameters, ManagementParameters, PricingParameters
from fba.contracts.season import ManagementInput, MarginalFeature, MarginalTask, SeasonKernel
from fba.core.roster import capacity, completable
from fba.formulas.arrays import evaluate_array
from fba.formulas.auction_fit import (
    category_diagnostics,
    diagnostic_traces,
    margin_score,
    matrix_root,
)
from fba.formulas.registry import evaluate
from fba.formulas.scoring import categories
from fba.formulas.simulation import deviation_array, mean_array, normal_quantile

type CompletionCache = dict[tuple[tuple[str, ...], ...], bool]


def benchmark_completable(
    portfolio: Portfolio, players: tuple[AuctionPlayer, ...], cache: CompletionCache
) -> bool:
    positions = tuple(sorted(tuple(sorted(p.positions)) for p in players))
    if positions not in cache:
        cache[positions] = completable(portfolio.league, players)
    return cache[positions]


class FeatureRunner(Protocol):
    def __call__(self, request: MarginalRequest) -> tuple[MarginalFeature, ...]: ...


def marginal_batch(
    manager: ManagedSeason,
    tasks: tuple[MarginalTask, ...],
) -> tuple[MarginalFeature, ...]:
    features: list[MarginalFeature] = []
    for task in tasks:
        difference = manager.project_primary_boxes(
            ((*task.rest, task.player), *task.opponents)
        ) - manager.project_primary_boxes((task.rest, *task.opponents))
        features.append(
            MarginalFeature(
                index=task.index,
                values=tuple(float(v) for v in mean_array(difference, axis=(0, 1))),
                blocks=tuple(
                    tuple(float(v) for v in mean_array(part, axis=(0, 1)))
                    for part in np.array_split(difference, manager.parameters.health_blocks)
                ),
            )
        )
    return tuple(features)


def paired_project(
    manager: ManagedSeason, roster: tuple[int, ...], opponents: tuple[tuple[int, ...], ...]
) -> ManagedMoments:
    if not opponents:
        return manager.project(roster)
    return manager.project_primary((roster, *opponents))


def opponent_rosters(
    portfolio: Portfolio,
    market: MarketResult,
    own_id: str,
    complete: tuple[str, ...],
    completion_cache: CompletionCache | None = None,
) -> tuple[tuple[str, ...], ...]:
    cache = {} if completion_cache is None else completion_cache
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
                    and benchmark_completable(
                        portfolio, tuple(by_id[i] for i in roster) + (p,), cache
                    )
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
        pricing: PricingParameters | None = None,
        tactics: ManagementParameters | None = None,
    ) -> None:
        self.portfolio, self.market, self.own_id = portfolio, market, own_id
        self.parameters = parameters
        self.management = management
        self.pricing, self.tactic_parameters = pricing, tactics
        self.opponent_rosters: tuple[tuple[int, ...], ...] = ()
        self.rival_cache: dict[tuple[int, ...], tuple[tuple[int, ...], ...]] = {}
        self.completion_cache: CompletionCache = {}
        self.manager = ManagedSeason(
            portfolio.league,
            parameters,
            management,
            portfolio.players,
            kernel,
            pricing=pricing,
            tactics=tactics,
        )
        self.league = portfolio.league
        samples = int(log2(parameters.samples))
        if 2**samples != parameters.samples:
            raise DataError("fit.samples: must be a power of two")
        # Pair coordinates from one joint design; independently scrambled Sobol
        # sequences are not independent when their rows are paired.
        draws = normal_quantile(
            {
                "uniform": qmc.Sobol(
                    self.manager.k * 2,
                    scramble=True,
                    seed=np.random.default_rng([parameters.seed, parameters.opponent_seed]),
                ).random_base2(samples),
                "epsilon": np.asarray(1e-12),
            }
        )
        self.draws = draws[:, : self.manager.k]
        self.opponent_draws = draws[:, self.manager.k :]
        self.opponent = np.empty((0, 0))
        self.opponent_blocks = np.empty((0, 0, 0))
        self.block_features: FloatArray | None = None
        self.scale = np.empty(0)
        self.initial_pool = self.waiver_pool(base)
        self.anchor = tuple(self.manager.index[i] for i in base.players)
        self.reference(base.players)

    def waiver_pool(self, base: Plan) -> tuple[int, ...]:
        opponents = opponent_rosters(
            self.portfolio, self.market, self.own_id, base.players, self.completion_cache
        )
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
        rosters = opponent_rosters(
            self.portfolio, self.market, self.own_id, complete, self.completion_cache
        )
        held = {self.manager.index[i] for r in rosters for i in r}
        self.manager.set_pool(tuple(i for i in self.initial_pool if i not in held))
        self.opponent_rosters = tuple(tuple(self.manager.index[i] for i in r) for r in rosters)
        if self.pricing is None:
            projected = self.manager.project_many(self.opponent_rosters)
        else:
            own = tuple(self.manager.index[i] for i in complete)
            joint = self.manager.project_many((own, *self.opponent_rosters))
            projected = ManagedMoments(joint.mean[1:], joint.covariance[1:], joint.boxes[:, 1:])
        means = projected.mean.reshape(-1, self.manager.k)
        covariances = projected.covariance.reshape(-1, self.manager.k, self.manager.k)
        index = np.arange(len(self.draws)) % len(means)
        noise = np.empty_like(self.draws)
        for j, covariance in enumerate(covariances):
            selected = index == j
            noise[selected] = evaluate_array(
                "matrix_product",
                left=self.opponent_draws[selected],
                right=matrix_root(covariance).T,
            ).result
        self.opponent = categories(means[index] + noise, self.league, self.manager.stat_ids)
        blocks = np.array(
            [
                mean_array(part, axis=0).reshape(-1, self.manager.k)
                for part in np.array_split(projected.boxes, self.parameters.health_blocks)
            ]
        )
        self.opponent_blocks = categories(
            blocks[:, index] + noise[None], self.league, self.manager.stat_ids
        )
        floors = {p.id: p.value for p in self.parameters.category_floors}
        self.scale = evaluate_array(
            "standard_deviation_floor",
            values=self.opponent,
            fraction=1.0,
            floor=np.array([floors[c.id] for c in self.league.categories]),
        ).result

    def rivals(self, roster: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
        roster = tuple(sorted(roster))
        if roster not in self.rival_cache:
            complete = tuple(self.manager.ids[i] for i in roster)
            rosters = opponent_rosters(
                self.portfolio, self.market, self.own_id, complete, self.completion_cache
            )
            self.rival_cache[roster] = tuple(
                tuple(self.manager.index[i] for i in r) for r in rosters
            )
        return self.rival_cache[roster]

    def candidate_rivals(self, rest: tuple[int, ...], player: int) -> tuple[tuple[int, ...], ...]:
        rivals = self.rivals(rest)
        return self.rivals((*rest, player)) if any(player in r for r in rivals) else rivals

    def project(self, roster: tuple[int, ...]) -> ManagedMoments:
        rivals = self.opponent_rosters if self.pricing else ()
        return paired_project(self.manager, roster, rivals)

    def context(self, roster: tuple[int, ...]) -> tuple[FloatArray, FloatArray]:
        result = self.project(roster)
        mean = mean_array(result.mean, axis=0)
        covariance = evaluate_array(
            "sampling_covariance", means=result.mean, covariances=result.covariance
        ).result
        return mean, evaluate_array(
            "matrix_product", left=self.draws, right=matrix_root(covariance).T
        ).result

    def difference(self, mean: FloatArray, noise: FloatArray) -> FloatArray:
        return evaluate_array(
            "standardized_margins",
            values=categories(mean + noise, self.league, self.manager.stat_ids),
            opponent=self.opponent,
            scale=self.scale,
        ).result

    def score(self, mean: FloatArray, noise: FloatArray) -> float:
        return evaluate(
            "mean",
            values=tuple(
                float(v)
                for v in margin_score(self.difference(mean, noise), self.parameters.bandwidth)
            ),
        ).result

    def block_scores(self, roster: tuple[int, ...], noise: FloatArray) -> FloatArray:
        boxes = self.project(roster).boxes
        means = np.array(
            [
                mean_array(part, axis=(0, 1))
                for part in np.array_split(boxes, self.parameters.health_blocks)
            ]
        )
        difference = evaluate_array(
            "standardized_margins",
            values=categories(means[:, None] + noise[None], self.league, self.manager.stat_ids),
            opponent=self.opponent_blocks,
            scale=self.scale,
        ).result
        return mean_array(margin_score(difference, self.parameters.bandwidth), axis=1)

    def gradient(self, mean: FloatArray, noise: FloatArray) -> FloatArray:
        step = evaluate_array(
            "standard_deviation_floor",
            values=mean + noise,
            fraction=self.parameters.gradient_fraction,
            floor=self.parameters.gradient_floor,
        ).result
        gradient = np.zeros(self.manager.k)
        for k, h in enumerate(step):
            delta = np.zeros(self.manager.k)
            delta[k] = h
            gradient[k] = evaluate_array(
                "central_difference",
                plus=self.score(mean + delta, noise),
                minus=self.score(mean - delta, noise),
                step=h,
            ).result
        return gradient

    def marginals(
        self, base: Plan, gradient: FloatArray, mean: FloatArray, runner: FeatureRunner | None
    ) -> FloatArray:
        losses: list[tuple[int, float]] = []
        for pid in base.purchases:
            p = self.manager.index[pid]
            rest = self.project(tuple(i for i in self.anchor if i != p))
            losses.append(
                (
                    p,
                    float(
                        evaluate_array(
                            "matrix_product",
                            left=mean - mean_array(rest.mean, axis=0),
                            right=gradient,
                        ).result
                    ),
                )
            )
        preferred = tuple(p for p, _ in sorted(losses, key=lambda pair: (pair[1], pair[0])))
        feature = np.zeros((len(self.portfolio.players), self.manager.k))
        tasks: list[MarginalTask] = []
        sold = {pid for team in self.market.room for pid in team.owned}
        for i, player in enumerate(self.portfolio.players):
            if not player.active or player.utility is None or player.id in self.portfolio.owned:
                continue
            if self.pricing is not None and player.id in sold:
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
            rivals = self.candidate_rivals(rest, p) if self.pricing else ()
            tasks.append(MarginalTask(index=i, player=p, rest=rest, opponents=rivals))
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
                    pricing=self.pricing,
                    tactics=self.tactic_parameters,
                )
            )
        )
        self.block_features = np.zeros((self.parameters.health_blocks, *feature.shape))
        for value in values:
            feature[value.index] = value.values
            self.block_features[:, value.index] = value.blocks
        return feature

    def sensitivity(
        self, gradient: FloatArray, scale: float, alpha: float
    ) -> tuple[tuple[FittedPlayer, ...], ...]:
        if not alpha:
            return ()
        if self.block_features is None:
            raise DataError("fit: paired health-group marginal features are unavailable")
        managed = evaluate_array(
            "utility_rescale",
            values=evaluate_array(
                "matrix_product", left=self.block_features, right=gradient
            ).result,
            reference=self.portfolio.values,
            scale=scale,
        ).result
        vectors = evaluate_array(
            "utility_blend", baseline=self.portfolio.values, managed=managed, step=alpha
        ).result
        vectors = np.round(vectors, self.portfolio.parameters.result_decimals)
        return tuple(
            tuple(
                FittedPlayer(id=p.id, utility=float(v) if p.utility is not None else None)
                for p, v in zip(self.portfolio.players, vector, strict=True)
            )
            for vector in vectors
        )

    def solve(
        self, base: Plan, runner: FeatureRunner | None = None
    ) -> tuple[Portfolio, Plan, FitSummary]:
        mean, noise = self.context(self.anchor)
        gradient = self.gradient(mean, noise)
        score = self.score(mean, noise)
        baseline_score = score
        selected_difference = self.difference(mean, noise)
        baseline_traces = diagnostic_traces(selected_difference, self.parameters)
        selected_opponents = self.opponent_rosters
        block_scores = self.block_scores(self.anchor, noise)
        feature = self.marginals(base, gradient, mean, runner)
        utility = evaluate_array("matrix_product", left=feature, right=gradient).result
        scale = float(deviation_array(utility))
        if scale < 1e-12:
            raise DataError("fit: managed marginal utility has no variation")
        managed = evaluate_array(
            "utility_rescale", values=utility, reference=self.portfolio.values, scale=scale
        ).result
        calls = self.portfolio.calls
        best = base
        selected = self.portfolio
        alpha = 0.0
        steps = [
            FitStep(step=0.0, score=score, accepted=True, block_minimum=0.0, block_maximum=0.0)
        ]
        for step in self.parameters.steps:
            vector = evaluate_array(
                "utility_blend", baseline=self.portfolio.values, managed=managed, step=step
            ).result
            if self.pricing is not None:
                vector = np.round(vector, self.portfolio.parameters.result_decimals)
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
            if self.pricing is not None and proposal.players == best.players:
                # An unchanged roster has unchanged managed outcomes; retain its new marginal
                # prices instead of silently discarding management value when no swap is needed.
                accepted = proposed_score >= score - self.parameters.improvement_tolerance and bool(
                    np.all(delta >= -self.parameters.improvement_tolerance)
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
                selected_difference = self.difference(proposed_mean, proposed_noise)
                selected_opponents = self.opponent_rosters
                score, block_scores, best, selected, alpha = (
                    proposed_score,
                    blocks,
                    proposal,
                    candidate,
                    step,
                )
        selected.calls = calls
        summary = FitSummary(
            anchor=base.players,
            selected_step=alpha,
            steps=tuple(steps),
            samples=len(self.draws),
            health_samples=self.parameters.health_samples,
            diagnostics=FitDiagnostics(
                categories=category_diagnostics(
                    selected_difference,
                    tuple(c.id for c in self.league.categories),
                    self.parameters,
                ),
                traces=(*baseline_traces, *diagnostic_traces(selected_difference, self.parameters)),
                baseline_score=baseline_score,
                selected_score=score,
                compared_roster=best.players,
                opponents=tuple(
                    tuple(self.manager.ids[i] for i in roster) for roster in selected_opponents
                ),
            ),
        )
        if self.pricing is not None:
            summary = ManagedFitSummary(
                **summary.model_dump(),
                method="paired_managed_marginal",
                players=tuple(FittedPlayer(id=p.id, utility=p.utility) for p in selected.players),
                policy=self.pricing,
                sensitivity=self.sensitivity(gradient, scale, alpha),
            )
        return selected, best, summary
