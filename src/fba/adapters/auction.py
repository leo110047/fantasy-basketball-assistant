from pathlib import Path
from time import perf_counter_ns

from fba.adapters.auction_metadata import auction_teams, freeze_team_sources, label_sources
from fba.adapters.calculation import load_calculation_input, publish_result
from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.adapters.config import load_parameters
from fba.adapters.forecast_scenarios import freeze_scenarios, valued_projection, verify_scenarios
from fba.adapters.preparation import prepare_snapshot
from fba.adapters.snapshots import artifact, load_snapshot, publish_bundle
from fba.contracts.auction import (
    AnnotatedAuctionDetail,
    AuctionDetail,
    AuctionInput,
    AuctionPlayer,
    DraftState,
    DraftTeam,
    MarketUpdate,
)
from fba.contracts.base import ConfigError, DataError, FormatVersion, Natural, Record, Text
from fba.contracts.config import (
    AuctionModel,
    CalculationModel,
    HealthModel,
    HealthParameters,
    ValidatedConfig,
)
from fba.contracts.data import Digest, Snapshot, StatValue
from fba.contracts.projection import CalculationResult, ProductionInput, RoleProjected
from fba.contracts.season import ManagedPlayer, ManagementInput, RoleManagedPlayer, SeasonKernel
from fba.core.auction import CapRunner, calculate_auction, market_context, run_caps
from fba.core.config import validate_config
from fba.core.fit import FeatureRunner
from fba.core.health import healthy_games
from fba.core.market import require_distribution
from fba.core.preparation import history_samples
from fba.core.projection import prior


class AuctionExecution(Record):
    format_version: FormatVersion
    input_sha256: Digest
    state_sha256: Digest
    result_sha256: Digest
    stage: Text
    elapsed_ns: Natural


def auction_players(
    snapshot: Snapshot, calculation: CalculationResult
) -> tuple[AuctionPlayer, ...]:
    values = {p.id: p for p in calculation.valuation.players}
    ids = tuple(p.roster.id for p in snapshot.players)
    if (
        len(values) != len(calculation.valuation.players)
        or len(set(ids)) != len(ids)
        or set(ids) != set(values)
    ):
        raise DataError("auction.players: snapshot and valuation populations disagree")
    return tuple(
        AuctionPlayer(
            id=p.roster.id,
            name=p.roster.name,
            positions=p.roster.positions,
            positions_confirmed=True,
            active=p.team_id is not None,
            projected_price=p.roster.projected_price,
            fair=values[p.roster.id].fair,
            utility=values[p.roster.id].utility,
        )
        for p in sorted(snapshot.players, key=lambda p: p.roster.id)
    )


def auction_details(
    snapshot: Snapshot,
    calculation: CalculationResult,
    config: ValidatedConfig,
    *,
    annotated: bool = False,
) -> tuple[AuctionDetail, ...]:
    model = config.model
    assert isinstance(model, AuctionModel)
    prepared = prepare_snapshot(snapshot, config)
    priors = {p.id: p for p in prepared.players}
    history = (
        history_samples(snapshot, (*model.projection.stat_ids, model.preparation.minutes_stat))[0]
        if annotated
        else {}
    )
    usable = {
        p.id
        for p in prepared.players
        if any(s.id == model.preparation.forecast_prior_id for s in p.priors)
    }
    projected = {p.id: p for p in calculation.projections}
    values = {p.id: p for p in calculation.valuation.players}
    axes = (*model.projection.stat_ids, model.projection.threshold_stat)
    provenance = tuple(a.provenance for a in snapshot.artifacts if a.provenance is not None)
    records: list[AuctionDetail] = []
    for player in sorted(snapshot.players, key=lambda p: p.roster.id):
        pid = player.roster.id
        projection = projected.get(pid)
        sources = tuple(sorted({f.source_id for f in snapshot.forecasts if f.player_id == pid}))
        if projection is not None and len(projection.stats) != len(axes):
            raise DataError(f"auction.details.{pid}: projection statistic axes disagree")
        records.append(
            AuctionDetail(
                player_id=pid,
                team_id=player.team_id,
                expected_games=projection.expected_games if projection is not None else None,
                minutes=projection.minutes if projection is not None else None,
                stats=tuple(
                    StatValue(id=axis, value=value, missing_reason=None)
                    for axis, value in zip(axes, projection.stats, strict=True)
                )
                if projection is not None
                else (),
                average_price=player.roster.average_price,
                forecast_sources=sources,
                forecast_usable=pid in usable,
                preparation_warnings=tuple(
                    n.detail
                    for n in prepared.notes
                    if n.player_id == pid and n.kind == "unavailable"
                ),
                forecast_provenance=tuple(
                    sorted(
                        (p for p in provenance if p.source_id in sources),
                        key=lambda p: (
                            p.source_id,
                            p.url,
                            p.raw_sha256,
                            p.available_as_of,
                            p.retrieved_at,
                            p.delivery,
                        ),
                    )
                ),
                adjustments=tuple(
                    sorted(
                        (a for a in snapshot.adjustments.adjustments if a.player_id == pid),
                        key=lambda a: (a.published_at, a.id),
                    )
                ),
                categories=values[pid].categories,
            )
        )
        if annotated:
            source = priors[pid]
            records[-1] = AnnotatedAuctionDetail(
                **records[-1].model_dump(),
                history_games=len(history.get(pid, ())),
                history_minimum=model.preparation.minimum_player_history,
                original_expected_games=prior(source, model.projection)[0]
                if source.priors
                else None,
                healthy_games_threshold=model.valuation.healthy_games,
            )
    return tuple(records)


def load_auction(path: Path) -> tuple[AuctionInput, str]:
    inputs, input_hash = load_calculation_input(path, AuctionInput)
    artifacts = {a.path: a.sha256 for a in inputs.artifacts}
    if (
        artifacts.get("source/snapshot.json") != inputs.snapshot_sha256
        or artifacts.get("source/calculation.json") != inputs.calculation_sha256
    ):
        raise DataError("auction.sources: source artifacts and lineage hashes disagree")
    snapshot = decode(
        Snapshot, read_bytes(path.parent / "source/snapshot.json"), "auction.snapshot"
    )
    result = decode(
        CalculationResult,
        read_bytes(path.parent / "source/calculation.json"),
        "auction.calculation",
    )
    if (
        snapshot.as_of > inputs.config.season.snapshot_as_of
        or snapshot.season_id != inputs.config.season.season_id
        or result.config.league != inputs.config.refs.league
        or result.config.season != inputs.config.refs.season
        or inputs.players != auction_players(snapshot, result)
    ):
        raise DataError("auction.sources: season, configuration or derived players disagree")
    model = inputs.config.model
    if not isinstance(model, AuctionModel):
        raise ConfigError("model: auction requires format_version 5")
    require_distribution(model.market)
    if inputs.details is not None and inputs.details != auction_details(
        snapshot, result, inputs.config, annotated=inputs.format_version >= 3
    ):
        raise DataError("auction.details: differs from frozen sources or calculation")
    if inputs.format_version >= 3:
        if any(
            artifacts.get(f"source/teams/{a.path}") != a.sha256
            for a in label_sources(snapshot, inputs.config)
        ):
            raise DataError("auction.teams: source artifact manifest disagrees with snapshot")
        files = {
            f"source/teams/{a.path}": read_bytes(path.parent / f"source/teams/{a.path}")
            for a in label_sources(snapshot, inputs.config)
        }
        if inputs.teams != auction_teams(snapshot, inputs.config, files):
            raise DataError("auction.teams: differs from frozen sources")
    verify_scenarios(inputs, path.parent)
    validate_management(inputs, result, model)
    return inputs, input_hash


def validate_management(
    inputs: AuctionInput, result: CalculationResult, model: AuctionModel
) -> None:
    management = inputs.management
    if management is None:
        raise DataError("auction.management: frozen managed projections are required")
    if management.stat_ids != (*model.projection.stat_ids, model.projection.threshold_stat):
        raise DataError("auction.management.stat_ids: differs from frozen projection axes")
    ids = tuple(p.id for p in management.players)
    if len(ids) != len(set(ids)) or set(ids) != {p.id for p in inputs.players}:
        raise DataError("auction.management: player population disagrees with catalogue")
    projected = {p.id: p for p in result.projections}
    for player in management.players:
        p = projected.get(player.id)
        if p is not None and (
            player.expected_games != p.expected_games
            or player.means != p.stats
            or player.covariance != p.covariance
        ):
            raise DataError(f"auction.management.{player.id}: differs from frozen projection")
    for player in management.players:
        p = projected.get(player.id)
        if isinstance(p, RoleProjected) and (
            not isinstance(player, RoleManagedPlayer)
            or player.unconstrained_games != p.unconstrained_games
        ):
            raise DataError(
                f"auction.management.{player.id}: original participation differs from projection"
            )
    if isinstance(model, HealthModel):
        for player in management.players:
            p = projected.get(player.id)
            if not isinstance(player, RoleManagedPlayer) or (
                p is not None
                and (
                    not isinstance(p, RoleProjected)
                    or player.unconstrained_games != p.unconstrained_games
                )
            ):
                raise DataError(f"auction.management.{player.id}: missing original participation")
            expected_health = healthy_games(
                player.unconstrained_games,
                player.season_games,
                player.return_on,
                player.game_days,
                model.health,
            )
            if player.healthy_games != expected_health:
                raise DataError(
                    f"auction.management.{player.id}: health decomposition differs from model"
                )


def prepare_auction(
    projection: Path,
    model_path: Path,
    output: Path,
    scenario_sources: tuple[tuple[str, Path], ...] = (),
) -> Path:
    inputs, _, calculation, payload = valued_projection(projection)
    model, ref = load_parameters(model_path)
    if not isinstance(model, AuctionModel):
        raise ConfigError("model: auction requires format_version 5")
    require_distribution(model.market)
    # Auction settings cannot silently change the projection represented by these values.
    for name in (
        "calibration",
        "projection",
        "valuation",
        "preparation",
        "team_minutes",
        "team_offense",
        "availability_tail",
        "team_constraints",
    ):
        if getattr(model, name, None) != getattr(inputs.config.model, name, None):
            raise ConfigError(f"model.{name}: differs from frozen projection; rebuild it first")
    config = validate_config(
        inputs.config.league,
        inputs.config.season,
        model,
        inputs.config.refs.model_copy(update={"model": ref}),
    )
    snapshot_root = projection / inputs.calibration_snapshot
    snapshot = load_snapshot(snapshot_root)
    players = auction_players(snapshot, calculation)
    files = {
        f"config/{n}.json": read_bytes(projection / f"config/{n}.json")
        for n in ("league", "season")
    }
    files["config/model.json"] = read_bytes(model_path)
    if digest(files["config/model.json"]) != ref.input_sha256:
        raise ConfigError("model: changed during auction preparation")
    files["config/effective.json"] = canonical(config)
    files["source/calculation.json"] = payload
    files["source/snapshot.json"] = read_bytes(snapshot_root / "snapshot.json")
    files.update(freeze_team_sources(snapshot, config, snapshot_root))
    scenarios, scenario_files = freeze_scenarios(
        scenario_sources, config, digest(files["source/snapshot.json"]), players
    )
    files.update(scenario_files)
    auction = AuctionInput(
        format_version=4,
        config=config,
        artifacts=tuple(artifact(p, data) for p, data in sorted(files.items())),
        snapshot_sha256=digest(files["source/snapshot.json"]),
        calculation_sha256=digest(payload),
        players=players,
        details=auction_details(snapshot, calculation, config, annotated=True),
        teams=auction_teams(snapshot, config, files),
        scenarios=scenarios,
        management=prepare_management(
            inputs, calculation, players, model.health if isinstance(model, HealthModel) else None
        ),
    )
    return publish_bundle(
        auction, auction.artifacts, config.season.snapshot_as_of, files, output, "auction-input"
    )


def prepare_management(
    inputs: ProductionInput,
    calculation: CalculationResult,
    players: tuple[AuctionPlayer, ...],
    health: HealthParameters | None = None,
) -> ManagementInput:
    model = inputs.config.model
    if not isinstance(model, CalculationModel):
        raise ConfigError("projection: missing statistic axes")
    axes = (*model.projection.stat_ids, model.projection.threshold_stat)
    results = {p.id: p for p in calculation.projections}
    source = {p.id: p for p in inputs.players}
    teams = {t.id: t for t in inputs.teams}
    managed: list[ManagedPlayer] = []
    for player in players:
        p = source[player.id]
        team = teams.get(p.team_id or "")
        projected = results.get(player.id)
        full = team.full_season_games if team is not None else 0
        expected = projected.expected_games if projected is not None else 0.0
        unconstrained = (
            projected.unconstrained_games if isinstance(projected, RoleProjected) else expected
        )
        record = ManagedPlayer(
            id=player.id,
            expected_games=expected,
            healthy_games=(
                healthy_games(unconstrained, full, p.return_on, team.dates if team else (), health)
                if health is not None
                else unconstrained
                if p.return_on is not None
                else float(full)
            ),
            season_games=float(full),
            return_on=p.return_on,
            game_days=team.dates if team else (),
            means=projected.stats if projected else (0.0,) * len(axes),
            covariance=projected.covariance
            if projected
            else tuple((0.0,) * len(axes) for _ in axes),
        )
        if isinstance(projected, RoleProjected) or health is not None:
            record = RoleManagedPlayer(**record.model_dump(), unconstrained_games=unconstrained)
        managed.append(record)
    return ManagementInput(
        stat_ids=axes, sampling_ids=tuple(p.id for p in players), players=tuple(managed)
    )


def auction_file(
    path: Path,
    draft: Path,
    output: Path,
    stage: str,
    runner: CapRunner = run_caps,
    feature_runner: FeatureRunner | None = None,
    kernel: SeasonKernel | None = None,
) -> Path:
    if stage not in ("market", "equal", "fit"):
        raise DataError("auction.stage: requires market, equal or fit")
    inputs, input_hash = load_auction(path)
    data = read_bytes(draft)
    state = decode(DraftState, data, str(draft))
    state_hash = digest(canonical(state))
    start = perf_counter_ns()
    if stage == "fit":
        record = calculate_auction(
            inputs,
            state,
            input_hash,
            state_hash,
            runner=runner,
            mode="fit",
            kernel=kernel,
            feature_runner=feature_runner,
        )
    else:
        record = (
            MarketUpdate(
                format_version=1,
                config=inputs.config.refs,
                input_sha256=input_hash,
                state_sha256=state_hash,
                market=market_context(inputs, state, input_hash)[1],
            )
            if stage == "market"
            else calculate_auction(inputs, state, input_hash, state_hash, runner=runner)
        )
    elapsed = perf_counter_ns() - start
    result = publish_result(record, output, stage)
    publish_result(
        AuctionExecution(
            format_version=1,
            input_sha256=input_hash,
            state_sha256=state_hash,
            result_sha256=digest(canonical(record)),
            stage=stage,
            elapsed_ns=elapsed,
        ),
        output,
        "execution",
    )
    return result


def draft_template(path: Path, mine: int, output: Path) -> Path:
    inputs, input_hash = load_auction(path)
    if not 1 <= mine <= inputs.config.league.teams:
        raise DataError("draft.mine: requires a team number within the configured league")
    teams = tuple(
        DraftTeam(id=str(i), name=f"Team {i}") for i in range(1, inputs.config.league.teams + 1)
    )
    state = DraftState(
        format_version=1,
        draft_id=output.stem,
        revision=0,
        config=inputs.config.refs,
        input_sha256=input_hash,
        mine=str(mine),
        teams=teams,
        sales=(),
        overrides=(),
    )
    try:
        with output.open("xb") as stream:
            stream.write(canonical(state))
    except OSError as exc:
        raise DataError(f"{output}: cannot create draft: {exc}") from exc
    return output
