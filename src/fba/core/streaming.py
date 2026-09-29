import numpy as np

from fba.contracts.auction import AuctionInput, AuctionResult, DraftState, Plan
from fba.contracts.base import DataError
from fba.contracts.config import FitParameters, StreamingComparisonModel
from fba.contracts.season import ManagementPolicy, SeasonArrays, SeasonKernel, TacticalArrays
from fba.contracts.streaming import (
    FlexPlayer,
    StreamingComparison,
    StreamingScenario,
    StreamingSummary,
)
from fba.core.auction import portfolio_for
from fba.core.fit import FittedUtility
from fba.core.managed import FloatArray
from fba.core.roster import effective_players


def select_streaming(
    slots: tuple[int, ...], values: tuple[FloatArray, ...], parameters: FitParameters
) -> tuple[int, tuple[StreamingComparison, ...]]:
    selected = 0
    comparisons: list[StreamingComparison] = []
    for index in range(1, len(slots)):
        delta = values[index] - values[selected]
        blocks = np.array([part.mean() for part in np.array_split(delta, parameters.health_blocks)])
        accepted = bool(np.all(blocks > parameters.improvement_tolerance))
        comparisons.append(
            StreamingComparison(
                slots=slots[index],
                versus=slots[selected],
                paired_gain=float(delta.mean()),
                block_minimum=float(blocks.min()),
                block_maximum=float(blocks.max()),
                accepted=accepted,
            )
        )
        if accepted:
            selected = index
    return slots[selected], tuple(comparisons)


def flex_candidates(
    fitted: FittedUtility,
    gradient: FloatArray,
    count: int,
    arrays: SeasonArrays,
    tactics: TacticalArrays,
    mean: FloatArray,
) -> tuple[FlexPlayer, ...]:
    if not count:
        return ()
    manager, roster = fitted.manager, fitted.anchor
    losses: list[FlexPlayer] = []
    for player in roster:
        rest = tuple(p for p in roster if p != player)
        if not any(
            len(manager.lineup((*rest, p))) == len(fitted.league.starter_slots)
            for p in manager.pool
        ):
            continue
        policy = tactics.policy.model_copy(
            update={"streaming_slots": (min(count, len(rest)), *tactics.policy.streaming_slots[1:])}
        )
        run = manager.kernel.run(
            arrays,
            (rest, *fitted.opponent_rosters),
            manager.pool,
            tactics.with_policy(policy),
            False,
            primary_only=True,
        )
        realized, expected = manager.control_mean(rest)
        removed = (run.counts[:, 0] @ manager.raw - (realized - expected)).mean(axis=(0, 1))
        loss = float((mean - removed) @ gradient)
        losses.append(FlexPlayer(player_id=manager.ids[player], removal_loss=loss))
    return tuple(sorted(losses, key=lambda p: (p.removal_loss, p.player_id))[:count])


def analyze_streaming(
    inputs: AuctionInput, state: DraftState, current: AuctionResult, kernel: SeasonKernel
) -> StreamingSummary:
    model = inputs.config.model
    if not isinstance(model, StreamingComparisonModel) or inputs.management is None:
        raise DataError(
            "streaming: rebuild with configured streaming comparisons (model format 13)"
        )
    if not isinstance(current.plan, Plan):
        raise DataError("streaming: a complete legal roster plan is required")
    players = effective_players(inputs.config.league, inputs.players, state)
    portfolio = portfolio_for(inputs, players, current.market, state)
    fitted = FittedUtility(
        portfolio,
        current.market,
        state.mine,
        model.fit,
        inputs.management,
        kernel,
        current.plan,
        model.pricing,
        model.management,
    )
    manager = fitted.manager
    mean, noise = fitted.context(fitted.anchor)
    gradient = fitted.gradient(mean, noise)
    rosters = (fitted.anchor, *fitted.opponent_rosters)
    rows: list[StreamingScenario] = []
    values: list[FloatArray] = []
    means: dict[int, FloatArray] = {}
    policy = ManagementPolicy(
        streaming_slots=(model.pricing.streaming_slots,) * len(rosters),
        reserve_adds=model.management.reserve_adds,
        upgrades=model.pricing.upgrades,
    )
    tactics, arrays = manager.tactics(policy, model.management), manager.arrays()
    for slots in model.streaming_comparison.slots:
        selected = policy.model_copy(
            update={"streaming_slots": (slots, *policy.streaming_slots[1:])}
        )
        run = kernel.run(
            arrays, rosters, manager.pool, tactics.with_policy(selected), False, primary_only=True
        )
        realized, expected = manager.control_mean(fitted.anchor)
        boxes = run.counts[:, 0] @ manager.raw - (realized - expected)
        means[slots] = boxes.mean(axis=(0, 1))
        values.append(boxes.mean(axis=1) @ gradient)
        injury, upgrade, stream = run.adds[:, 0].mean(axis=(0, 1))
        rows.append(
            StreamingScenario(
                slots=slots,
                started=float(run.counts[:, 0].sum(axis=-1).mean()),
                injury_adds=float(injury),
                upgrade_adds=float(upgrade),
                stream_adds=float(stream),
                gain=float((values[-1] - values[0]).mean()),
            )
        )
    recommended, comparisons = select_streaming(
        model.streaming_comparison.slots, tuple(values), model.fit
    )
    return StreamingSummary(
        roster=current.plan.players,
        recommended_slots=recommended,
        configured_slots=model.pricing.streaming_slots,
        scenarios=tuple(rows),
        comparisons=comparisons,
        flex=flex_candidates(fitted, gradient, recommended, arrays, tactics, means[recommended]),
        shared_limit=inputs.config.league.transactions.adds_per_period,
        reserve_adds=model.management.reserve_adds,
        upgrades=model.pricing.upgrades,
        health_samples=model.fit.health_samples,
        health_blocks=model.fit.health_blocks,
        matchup_count=len(manager.weeks),
        pool_size=len(manager.pool),
    )
