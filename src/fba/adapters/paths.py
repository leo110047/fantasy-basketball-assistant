from pathlib import Path
from time import perf_counter
from typing import Literal

from fba.adapters.auction import load_auction
from fba.adapters.calculation import publish_result
from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.contracts.auction import DraftState
from fba.contracts.base import DataError
from fba.contracts.config import AuctionModel
from fba.contracts.paths import PathPair, StressResult, StressSettings
from fba.contracts.season import SeasonKernel
from fba.core.auction import CapRunner, calculate_auction, run_caps
from fba.core.fit import FeatureRunner
from fba.core.paths import AuctionPaths


def verdict(
    pairs: tuple[PathPair, ...], tolerance: float
) -> Literal["incomplete", "no_difference", "supported", "skip", "mixed"]:
    deltas = [p.delta for p in pairs if p.delta is not None]
    if len(deltas) != len(pairs):
        return "incomplete"
    if all(abs(d) <= tolerance for d in deltas):
        return "no_difference"
    if min(deltas) >= -tolerance:
        return "supported"
    if max(deltas) <= tolerance:
        return "skip"
    return "mixed"


def paths_file(
    path: Path,
    draft: Path,
    settings_path: Path,
    output: Path,
    player_id: str,
    ceiling: int,
    mode: Literal["equal", "fit"],
    runner: CapRunner = run_caps,
    feature_runner: FeatureRunner | None = None,
    kernel: SeasonKernel | None = None,
) -> Path:
    inputs, input_hash = load_auction(path)
    state = decode(DraftState, read_bytes(draft), str(draft))
    settings = decode(StressSettings, read_bytes(settings_path), str(settings_path))
    if settings.evidence.as_of > inputs.config.season.snapshot_as_of:
        raise DataError("auction_stress.evidence.as_of: after snapshot cutoff")
    state_hash = digest(canonical(state))
    started = perf_counter()
    current = calculate_auction(
        inputs,
        state,
        input_hash,
        state_hash,
        mode=mode,
        runner=runner,
        feature_runner=feature_runner,
        kernel=kernel,
    )
    calculated = perf_counter()
    pairs = AuctionPaths(inputs, state, current, settings).compare(player_id, ceiling)
    ended = perf_counter()
    model = inputs.config.model
    assert isinstance(model, AuctionModel)
    branches = tuple(branch for pair in pairs for branch in (pair.participate, pair.skip))
    result = StressResult(
        format_version=1,
        config=inputs.config.refs,
        input_sha256=input_hash,
        state_sha256=state_hash,
        settings_sha256=digest(canonical(settings)),
        settings=settings,
        state=state,
        initial=current,
        mode=mode,
        player_id=player_id,
        ceiling=ceiling,
        verdict=verdict(pairs, model.solver.value_tolerance),
        complete_pairs=sum(p.delta is not None for p in pairs),
        pairs=pairs,
        policy_calls=sum(branch.policy_calls for branch in branches),
        solver_calls=current.solver_calls + sum(branch.solver_calls for branch in branches),
        calculation_seconds=calculated - started,
        paths_seconds=ended - calculated,
    )
    return publish_result(result, output, "auction-paths")
