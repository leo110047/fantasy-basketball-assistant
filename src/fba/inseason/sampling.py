"""Sampling from frozen player evidence; RNG streams preserve shared sample prefixes."""

from hashlib import sha256

import numpy as np
from numpy.typing import NDArray

from fba.contracts.base import DataError
from fba.contracts.inseason import (
    BoxScore,
    EffectivePlayer,
    FrozenPriors,
    InseasonParameters,
    ProjectionRules,
    SeasonGame,
)
from fba.formulas.arrays import evaluate_array
from fba.formulas.categories import derive_games
from fba.formulas.registry import evaluate
from fba.formulas.simulation import array_product, mean_array, nested_count_limit

type SamplingProfile = tuple[str, float, float, tuple[tuple[str, float], ...]]


def sampling_profile(player: EffectivePlayer) -> SamplingProfile:
    """Distribution identity within one Simulation's fixed history/model context.

    This deliberately compares the original inputs, not rounded target means.
    Game identity only selects independent random streams in sample_game.
    """
    return (
        player.player.id,
        player.minutes,
        player.probability,
        tuple(sorted(player.rates.items())),
    )


def sample_game(
    player: EffectivePlayer,
    history: tuple[BoxScore, ...],
    league: ProjectionRules,
    params: InseasonParameters,
    priors: FrozenPriors,
    samples: int,
    game: SeasonGame,
) -> NDArray[np.float64]:
    player_id = player.player.id
    # Production is conditional on playing; probability owns every DNP draw.
    history = tuple(b for b in history if b.minutes > 0)

    def random(stream: str) -> np.random.Generator:
        seed = int.from_bytes(
            sha256(f"{params.seed.value}:{player_id}:{game.id}:{stream}".encode()).digest()[:8],
            "little",
        )
        return np.random.default_rng(seed)

    target = np.array(
        [
            evaluate("product", gain=player.rates.get(s, 0.0), probability=player.minutes).result
            for s in league.base_stats
        ]
    )
    if not history:
        sampled = np.stack(
            [random(s).poisson(target[i], size=samples) for i, s in enumerate(league.base_stats)],
            axis=-1,
        ).astype(np.float64)
        distribution = priors.distribution
        if distribution is None:
            raise DataError(
                f"simulation.{player_id}: prior predictive requires frozen preseason "
                "nested-count rules; reload the preseason source"
            )
        pending = {
            row.child: row.parent
            for row in distribution.nested_counts
            if row.child in league.base_stats
        }
        while pending:
            ready = tuple(child for child, parent in pending.items() if parent not in pending)
            if not ready:
                raise DataError("prior.distribution.nested_counts: cyclic constraints")
            for child in ready:
                parent = pending.pop(child)
                if parent not in league.base_stats:
                    raise DataError(f"prior.distribution.{child}: missing parent {parent}")
                i, j = (league.base_stats.index(s) for s in (child, parent))
                if target[i] > target[j]:
                    raise DataError(f"prior.distribution.{child}: mean exceeds parent {parent}")
                probability = evaluate(
                    "ratio",
                    numerator=float(target[i]),
                    denominator=float(target[j]),
                    zero_value=0.0,
                ).result
                sampled[:, i] = random(child + ":nested").binomial(
                    sampled[:, j].astype(np.int64), probability
                )
        sampled = array_product(
            {
                "values": sampled,
                "multiplier": (random("availability").random(samples) < player.probability)[
                    :, None
                ],
            }
        )
        result = derive_games(sampled, league.base_stats, league.derived)
        return result
    source = np.array([[b.stats.get(s, 0.0) for s in league.base_stats] for b in history])
    sampled = source[random("history").integers(len(source), size=samples)].copy()
    mean = mean_array(source, axis=0)
    # A zero-observation statistic cannot be rescaled. The explicit prior
    # predictive Poisson component preserves its positive model mean.
    zero_mean = np.zeros_like(sampled)
    for i in np.flatnonzero(mean == 0):
        zero_mean[:, i] = random(league.base_stats[int(i)]).poisson(target[i], size=samples)
    sampled = evaluate_array(
        "bootstrap_scale",
        history=source,
        samples=sampled,
        target=target,
        zero_mean_draws=zero_mean,
    ).result.copy()
    for shot in league.shots:
        i, j = (
            league.base_stats.index(shot.made),
            league.base_stats.index(shot.attempted),
        )
        sampled[:, i] = nested_count_limit({"child": sampled[:, i], "parent": sampled[:, j]})
    sampled = array_product(
        {
            "values": sampled,
            "multiplier": (random("availability").random(samples) < player.probability)[:, None],
        }
    )
    result = derive_games(sampled, league.base_stats, league.derived)
    return result
