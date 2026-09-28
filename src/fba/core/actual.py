from collections import defaultdict
from math import fsum

from fba.contracts.base import DataError
from fba.contracts.config import PreparationModel, ThresholdCount, ValidatedConfig
from fba.contracts.data import ActualSeason, PlayerGame
from fba.contracts.projection import Projected


def observed_players(
    history: tuple[PlayerGame, ...], totals: tuple[ActualSeason, ...], config: ValidatedConfig
) -> tuple[Projected, ...]:
    model = config.model
    if not isinstance(model, PreparationModel):
        raise DataError("annual.evaluation: preparation model required")
    axes = (*model.projection.stat_ids, model.projection.threshold_stat)
    threshold = next(s.definition for s in config.season.stat_definitions if s.id == axes[-1])
    if not isinstance(threshold, ThresholdCount):
        raise DataError("annual.evaluation: invalid threshold statistic")
    counts = {t.player_id: t for t in totals}
    if len(counts) != len(totals):
        raise DataError("annual.actual: duplicate season totals")
    grouped: dict[str, list[tuple[float, ...]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for game in sorted(history, key=lambda g: (g.player_id, g.game_id)):
        key = (game.player_id, game.game_id)
        stats = {s.id: s.value for s in game.stats}
        if key in seen or len(stats) != len(game.stats) or game.player_id not in counts:
            raise DataError(f"annual.actual.{key}: duplicate observation or absent season total")
        seen.add(key)
        required = (*axes[:-1], model.preparation.minutes_stat)
        if any(stats.get(s) is None for s in required):
            raise DataError(f"annual.actual.{key}: missing observed statistic")
        values = tuple(float(value) for s in required if (value := stats[s]) is not None)
        hits = sum(values[required.index(s)] >= threshold.threshold for s in threshold.stat_ids)
        grouped[game.player_id].append(
            (*values[:-1], float(hits >= threshold.minimum_hits), values[-1])
        )
    result: list[Projected] = []
    for pid, total in sorted(counts.items()):
        rows = grouped[pid]
        means = observed_means(total, rows, axes, model, threshold)
        result.append(
            Projected(
                id=pid,
                expected_games=float(total.games),
                minutes=means[-1],
                stats=means[:-1],
                covariance=tuple((0.0,) * len(axes) for _ in axes),
            )
        )
    return tuple(result)


def observed_means(
    total: ActualSeason,
    rows: list[tuple[float, ...]],
    axes: tuple[str, ...],
    model: PreparationModel,
    threshold: ThresholdCount,
) -> tuple[float, ...]:
    path = f"annual.actual.{total.player_id}"
    values = {s.id: s.value for s in total.totals}
    required = (*axes[:-1], model.preparation.minutes_stat)
    if len(values) != len(total.totals) or any(values.get(s) is None for s in required):
        raise DataError(f"{path}: missing or duplicate season statistic")
    observed = tuple(float(value) for s in required if (value := values[s]) is not None)
    if len(rows) > total.games or (total.games == 0 and any(observed)):
        raise DataError(f"{path}: inconsistent reported games")
    residual = tuple(observed[i] - fsum(r[i] for r in rows) for i in range(len(axes) - 1))
    tolerance = model.projection.feasibility_tolerance
    if any(r < -tolerance for r in residual) or (
        len(rows) == total.games and any(abs(r) > tolerance for r in residual)
    ):
        raise DataError(f"{path}: game statistics disagree with season totals")
    # Missing dates cannot affect a threshold count when the remaining season totals
    # cannot supply enough qualifying categories even in a single game. This is an
    # exact bound, never an estimated game or a missing-data fallback.
    possible_hits = sum(residual[axes.index(s)] >= threshold.threshold for s in threshold.stat_ids)
    if len(rows) < total.games and possible_hits >= threshold.minimum_hits:
        raise DataError(f"{path}: incomplete game logs leave threshold count undetermined")
    totals = (*observed[:-1], fsum(r[-2] for r in rows), observed[-1])
    return tuple(v / total.games if total.games else 0.0 for v in totals)
