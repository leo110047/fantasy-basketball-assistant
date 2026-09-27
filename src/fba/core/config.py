from datetime import date, timedelta
from math import fsum

from fba.contracts.base import ConfigError
from fba.contracts.config import (
    CalculationModel,
    ConfigBundle,
    LeagueRules,
    Linear,
    ModelConfig,
    Period,
    PreparationModel,
    ResourceModel,
    SeasonConfig,
    ThresholdCount,
    ValidatedConfig,
)


def unique(values: tuple[str, ...], path: str) -> None:
    if len(values) != len(set(values)):
        raise ConfigError(f"{path}: duplicate values")


def require_members(values: tuple[str, ...], allowed: set[str], path: str) -> None:
    missing = sorted(set(values) - allowed)
    if missing:
        raise ConfigError(f"{path}: unknown references {missing}")


def validate_periods(periods: tuple[Period, ...], start: date, end: date, path: str) -> None:
    unique(tuple(p.id for p in periods), path)
    next_day = start
    for period in sorted(periods, key=lambda p: (p.start, p.id)):
        if period.start != next_day or period.end < period.start:
            raise ConfigError(f"{path}.{period.id}: overlap, gap, or reversed dates")
        next_day = period.end + timedelta(days=1)
    if next_day != end + timedelta(days=1):
        raise ConfigError(f"{path}: must cover {start} through {end}")


def validate_stats(league: LeagueRules, season: SeasonConfig) -> None:
    definitions = {s.id: s for s in season.stat_definitions}
    unique(tuple(s.id for s in season.stat_definitions), "season.stat_definitions")
    graph: dict[str, tuple[str, ...]] = {}
    for stat in season.stat_definitions:
        definition = stat.definition
        dependencies: tuple[str, ...] = ()
        if isinstance(definition, Linear):
            dependencies = tuple(t.stat_id for t in definition.terms)
        elif isinstance(definition, ThresholdCount):
            dependencies = definition.stat_ids
            if definition.minimum_hits > len(dependencies):
                raise ConfigError(f"season.stat_definitions.{stat.id}: impossible minimum_hits")
        require_members(dependencies, set(definitions), f"season.stat_definitions.{stat.id}")
        unique(dependencies, f"season.stat_definitions.{stat.id}")
        if any(definitions[d].unit != stat.unit for d in dependencies):
            raise ConfigError(f"season.stat_definitions.{stat.id}: incompatible units")
        graph[stat.id] = dependencies
    remaining = set(graph)
    while remaining:
        ready = {s for s in remaining if not (set(graph[s]) & remaining)}
        if not ready:
            raise ConfigError(f"season.stat_definitions: dependency cycle {sorted(remaining)}")
        remaining -= ready
    for category in league.categories:
        formula = category.formula
        terms = (
            formula.terms
            if isinstance(formula, Linear)
            else (formula.numerator + formula.denominator)
        )
        require_members(
            tuple(t.stat_id for t in terms),
            set(definitions),
            f"league.categories.{category.id}.formula",
        )


def validate_league(league: LeagueRules, season: SeasonConfig) -> None:
    unique(league.positions, "league.positions")
    unique(tuple(s.id for s in league.starter_slots + league.injury_slots), "league.slots")
    unique(tuple(c.id for c in league.categories), "league.categories")
    for slot in league.starter_slots:
        unique(slot.eligible_positions, f"league.starter_slots.{slot.id}")
        require_members(
            slot.eligible_positions,
            set(league.positions),
            f"league.starter_slots.{slot.id}",
        )
    for injury in league.injury_slots:
        unique(injury.eligible_statuses, f"league.injury_slots.{injury.id}")
    capacity = len(league.starter_slots) + league.bench_slots
    if league.budget < capacity * league.minimum_bid:
        raise ConfigError("league.budget: cannot pay minimum_bid for every roster slot")
    if any(n % league.bid_increment for n in (league.budget, league.minimum_bid)):
        raise ConfigError("league.bid_increment: budget and minimum_bid must be multiples")
    if league.lineup.lock_mode == "weekly" and league.lineup.lock_at == "player_game":
        raise ConfigError("league.lineup.lock_at: player_game conflicts with weekly lock")
    if any(
        t.tzinfo is not None
        for t in (
            league.lineup.lock_local_time,
            league.transactions.cutoff_local_time,
        )
    ):
        raise ConfigError("league: local times must use league.timezone, without an offset")
    validate_periods(league.matchups, season.starts_on, season.ends_on, "league.matchups")
    validate_periods(
        league.transactions.add_periods,
        season.starts_on,
        season.ends_on,
        "league.transactions.add_periods",
    )
    validate_playoffs(league)


def validate_playoffs(league: LeagueRules) -> None:
    playoffs = league.playoffs
    unique(playoffs.week_ids, "league.playoffs.week_ids")
    unique(playoffs.seeding, "league.playoffs.seeding")
    playoff_weeks = tuple(w.id for w in league.matchups if w.phase == "playoff")
    if playoff_weeks != playoffs.week_ids:
        raise ConfigError("league.playoffs.week_ids: must equal playoff matchups in date order")
    if playoffs.team_count > league.teams or playoffs.byes >= playoffs.team_count:
        raise ConfigError("league.playoffs: invalid team_count or byes")
    first_round = playoffs.team_count - playoffs.byes
    if first_round % 2:
        raise ConfigError("league.playoffs.byes: first round must pair all non-bye teams")
    remaining = first_round // 2 + playoffs.byes
    for _ in playoffs.week_ids[1:]:
        if remaining < 2 or remaining % 2:
            raise ConfigError("league.playoffs.week_ids: rounds do not form a bracket")
        remaining //= 2
    if remaining != 1:
        raise ConfigError("league.playoffs.week_ids: bracket must produce one winner")


def validate_sources(season: SeasonConfig) -> None:
    unique(tuple(s.id for s in season.sources), "season.sources")
    required_roles = {
        "projections",
        "game_logs",
        "schedule",
        "rosters",
        "official_schedule_counts",
        "historical_projections",
    }
    roles = {s.role for s in season.sources}
    if required_roles - roles:
        raise ConfigError(f"season.sources: missing roles {sorted(required_roles - roles)}")
    for role in required_roles - {"rosters"}:
        if sum(s.role == role for s in season.sources) != 1:
            raise ConfigError(f"season.sources: this data format requires one {role} source")
    for source in season.sources:
        expected_season = (
            season.previous_season_id
            if source.role
            in (
                "historical_projections",
                "game_logs",
            )
            else season.season_id
        )
        if source.season_id != expected_season:
            raise ConfigError(f"season.sources.{source.id}.season_id: expected {expected_season}")
        if not source.url.startswith("https://"):
            raise ConfigError(f"season.sources.{source.id}.url: requires https")
        if (source.delivery == "manual") != (source.manual_file is not None):
            raise ConfigError(f"season.sources.{source.id}.manual_file: conflicts with delivery")
        if (source.delivery == "manual") != (source.manual_capture is not None):
            raise ConfigError(f"season.sources.{source.id}.manual_capture: conflicts with delivery")
        if (
            source.manual_capture is not None
            and source.manual_capture.retrieved_at > season.snapshot_as_of
        ):
            raise ConfigError(f"season.sources.{source.id}.manual_capture: after snapshot cutoff")
        if source.available_as_of > season.snapshot_as_of:
            raise ConfigError(f"season.sources.{source.id}.available_as_of: after snapshot cutoff")


def validate_config(
    league: LeagueRules,
    season: SeasonConfig,
    model: ModelConfig | ResourceModel | CalculationModel | PreparationModel,
    refs: ConfigBundle,
) -> ValidatedConfig:
    if season.starts_on > season.ends_on:
        raise ConfigError("season.ends_on: before starts_on")
    if season.previous_season_id == season.season_id:
        raise ConfigError("season.previous_season_id: must differ from season_id")
    validate_league(league, season)
    validate_stats(league, season)
    validate_sources(season)
    if model.calibration.evidence.as_of > season.snapshot_as_of:
        raise ConfigError("model.calibration.evidence.as_of: after snapshot cutoff")
    if isinstance(model, (ResourceModel, CalculationModel)):
        validate_calculation_model(league, season, model)
    if isinstance(model, PreparationModel):
        validate_preparation_model(league, season, model)
    return ValidatedConfig(league=league, season=season, model=model, refs=refs)


def validate_preparation_model(
    league: LeagueRules, season: SeasonConfig, model: PreparationModel
) -> None:
    p = model.preparation
    path = "model.preparation"
    ids = (p.forecast_prior_id, p.historical_prior_id)
    unique(ids, f"{path}.prior_ids")
    if set(ids) != {w.id for w in model.projection.prior_weights}:
        raise ConfigError(f"{path}.prior_ids: must match configured forecast and historical priors")
    if (
        p.donor_minutes_lower > p.donor_minutes_upper
        or p.historical_games_lower > p.historical_games_upper
        or p.minimum_player_history > p.donor_minimum_history
    ):
        raise ConfigError(f"{path}: reversed bounds or donor sample smaller than player minimum")
    definitions = {s.id: s for s in season.stat_definitions}
    if p.minutes_stat not in definitions or definitions[p.minutes_stat].unit != "minutes":
        raise ConfigError(f"{path}.minutes_stat: requires a minutes statistic")
    unique(tuple(g.id for g in p.position_pools), f"{path}.position_pools")
    covered: set[str] = set()
    for group in p.position_pools:
        positions = (*group.any_positions, *group.exact_positions)
        if not positions:
            raise ConfigError(f"{path}.position_pools.{group.id}: empty positions")
        require_members(positions, set(league.positions), f"{path}.position_pools.{group.id}")
        covered.update(positions)
    if covered != set(league.positions):
        raise ConfigError(f"{path}.position_pools: must cover league positions")
    unique(tuple(r.stat_id for r in p.history_shares), f"{path}.history_shares")
    pairs = {(r.child, r.parent) for r in model.projection.nested_counts}
    if any((r.stat_id, r.parent_stat) not in pairs for r in p.history_shares):
        raise ConfigError(f"{path}.history_shares: must refer to nested count relationships")
    if p.evidence.as_of > season.snapshot_as_of:
        raise ConfigError(f"{path}.evidence.as_of: after snapshot cutoff")


def validate_calculation_model(
    league: LeagueRules, season: SeasonConfig, model: ResourceModel | CalculationModel
) -> None:
    p = model.projection
    fields = set(p.stat_ids)
    if not fields or len(fields) != len(p.stat_ids):
        raise ConfigError("model.projection.stat_ids: empty or duplicate axes")
    groups = tuple(field for group in p.rounding_groups for field in group)
    if (
        any(not group for group in p.rounding_groups)
        or len(set(groups)) != len(groups)
        or set(groups) != fields - {p.scoring_stat}
    ):
        raise ConfigError("model.projection.rounding_groups: must partition primitive axes")
    if not p.scoring_terms or any(t.stat_id == p.scoring_stat for t in p.scoring_terms):
        raise ConfigError("model.projection.scoring_terms: missing or cyclic formula")
    used = {p.scoring_stat, *(t.stat_id for t in p.scoring_terms)}
    if isinstance(model, ResourceModel):
        used.update(
            (
                model.projection.second_chance_stat,
                model.projection.assist_stat,
                model.projection.made_stat,
                *model.projection.offense_stats,
            )
        )
        used.update(t.stat_id for t in model.projection.possession_terms)
    if not used <= fields or p.threshold_stat in fields:
        raise ConfigError("model.projection: unknown or duplicate statistic reference")
    definitions = {s.id: s.definition for s in season.stat_definitions}
    if not fields | {p.threshold_stat} <= set(definitions):
        raise ConfigError("model.projection.stat_ids: undefined season statistic")
    threshold = definitions[p.threshold_stat]
    if not isinstance(threshold, ThresholdCount) or not set(threshold.stat_ids) <= fields:
        raise ConfigError("model.projection.threshold_stat: requires projected threshold inputs")
    if (
        min(
            p.count_pseudocount,
            p.attempt_pseudocount,
            p.feasibility_tolerance,
        )
        <= 0
    ):
        raise ConfigError("model.projection: numerical scales and tolerances must be positive")
    validate_nested_counts(model)
    if isinstance(model, ResourceModel):
        validate_offense(model)
    else:
        unique(
            tuple(w.id for w in model.projection.prior_weights), "model.projection.prior_weights"
        )
        if (
            not model.projection.prior_weights
            or abs(fsum(w.weight for w in model.projection.prior_weights) - 1)
            > p.feasibility_tolerance
        ):
            raise ConfigError("model.projection.prior_weights: weights must sum to one")
    for category in league.categories:
        terms = (
            category.formula.terms
            if isinstance(category.formula, Linear)
            else (*category.formula.numerator, *category.formula.denominator)
        )
        if any(t.stat_id not in fields | {p.threshold_stat} for t in terms):
            raise ConfigError(f"league.categories.{category.id}: no projected statistic")
    for name, evidence in (("projection", p.evidence), ("valuation", model.valuation.evidence)):
        if evidence.as_of > season.snapshot_as_of:
            raise ConfigError(f"model.{name}.evidence.as_of: after snapshot cutoff")


def validate_nested_counts(model: ResourceModel | CalculationModel) -> None:
    p = model.projection
    children = tuple(pair.child for pair in p.nested_counts)
    if len(children) != len(set(children)) or not set(children) <= set(p.stat_ids):
        raise ConfigError("model.projection.nested_counts: duplicate or unknown child")
    available = set(p.stat_ids) - set(children) - {p.scoring_stat}
    for pair in p.nested_counts:
        if pair.parent not in available or not any(
            pair.parent in group and pair.child in group for group in p.rounding_groups
        ):
            raise ConfigError(
                "model.projection.nested_counts: cyclic order or inconsistent rounding group"
            )
        available.add(pair.child)


def validate_offense(model: ResourceModel) -> None:
    p = model.projection
    offense = set(p.offense_stats)
    if (
        len(offense) != len(p.offense_stats)
        or min(p.minimum_cost_scale, p.minimum_usage_scale) <= 0
        or not p.possession_terms
        or any(t.coefficient <= 0 or t.stat_id not in offense for t in p.possession_terms)
        or p.second_chance_stat in offense
        or any((pair.child in offense) != (pair.parent in offense) for pair in p.nested_counts)
        or (p.scoring_stat in offense and any(t.stat_id not in offense for t in p.scoring_terms))
    ):
        raise ConfigError("model.projection.offense_stats: inconsistent resource dependencies")
