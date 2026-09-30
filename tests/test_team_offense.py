import json
from pathlib import Path

import numpy as np
import pytest
from test_calculation import projection_bundle as projection_bundle
from test_team_minutes import budget_case as budget_case

from fba.adapters.sources import SourceData
from fba.adapters.team_offense import offense_baselines, outside_prior
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import ModelDocument
from fba.contracts.data import PlayerGame, ProviderPlayer, StatValue
from fba.contracts.projection import (
    MinuteEstimate,
    OffenseInput,
    Prior,
    TeamBoxPrior,
    TeamMember,
    TeamOffenseBaseline,
)
from fba.core.config import validate_team_offense
from fba.formulas.team_offense import validate_offense, validate_offense_input
from fba.projection.calculation import calculate, calculate_with_offense


@pytest.fixture
def offense_case(budget_case):
    i = budget_case
    model = i.config.model.model_dump(mode="json")
    example = json.loads((Path(__file__).parents[1] / "examples/2026-27/model.json").read_text())
    for key in ("health", "pricing"):
        model[key] = example[key]
    model.update(
        format_version=10,
        team_offense={
            "used_terms": [
                {"stat_id": "FGA", "coefficient": 1},
                {"stat_id": "FTA", "coefficient": 0.44},
                {"stat_id": "TO", "coefficient": 1},
            ],
            "second_chance_stat": "OREB",
            "scaled_stats": ["FGM", "FGA", "FTM", "FTA", "PTS", "3PM", "AST", "TO"],
            "assist_stat": "AST",
            "made_stat": "FGM",
            "historical_minimum_games": 1,
            "historical_overtime_minutes": 5,
            "historical_minute_tolerance": 5,
            "evidence": model["projection"]["evidence"],
        },
    )
    model = ModelDocument.model_validate_json(json.dumps(model)).root
    # Team budgets deliberately below the observed model's shooting density.
    baseline = (30.0, 50.0, 10.0, 15.0, 80.0, 10.0, 40.0, 10.0, 20.0, 10.0, 5.0, 5.0)
    baselines = tuple(
        TeamOffenseBaseline(
            team_id=t.id,
            stats=baseline,
            game_ids=("old-game",),
            excluded_game_ids=(),
            source_ids=("historical-boxes",),
        )
        for t in i.teams
    )
    return OffenseInput.model_validate(
        {
            **i.model_dump(),
            "format_version": 6,
            "config": i.config.model_copy(update={"model": model}),
            "outside_priors": (),
            "offense_baselines": baselines,
        }
    )


def test_full_projection_conserves_usage_and_recomputes_distribution(offense_case):
    i = offense_case
    result, audit = calculate_with_offense(i, "1" * 64)
    assert all(a.after + a.reserved_possessions <= a.possession_budget + 1e-9 for a in audit)
    assert any(p.usage_factor < 1 for a in audit for p in a.allocations)
    for p in result.projections:
        row = next(r for a in audit for r in a.allocations if r.catalog_id == p.id)
        assert p.stats[1] == pytest.approx(row.after[1], abs=1e-7)
        assert p.stats[0] / p.stats[1] == pytest.approx(row.before[0] / row.before[1])
        assert p.stats[2] / p.stats[3] == pytest.approx(row.before[2] / row.before[3])
        assert p.stats[4] == pytest.approx(2 * p.stats[0] + p.stats[2] + p.stats[5], abs=1e-7)
        assert np.linalg.eigvalsh(np.array(p.covariance)).min() >= -1e-7
        assert p.expected_games == p.unconstrained_games == 1.5
    shuffled = i.model_copy(
        update={
            "players": tuple(reversed(i.players)),
            "team_members": tuple(reversed(i.team_members)),
            "offense_baselines": tuple(reversed(i.offense_baselines)),
        }
    )
    assert calculate(shuffled, "1" * 64) == result
    tampered = tuple(
        p.model_copy(update={"stats": tuple(v * 10 for v in p.stats)}) for p in result.projections
    )
    with pytest.raises(DataError, match="usage exceeds"):
        validate_offense(tampered, i, audit)


def test_outside_players_and_unknown_minutes_consume_team_resources(offense_case):
    i = offense_case
    prior = Prior(id="a", expected_games=3, minutes=15, stats=i.players[0].priors[0].stats)
    member = TeamMember(
        id="outside",
        team_id="A",
        catalog_id=None,
        estimates=(
            MinuteEstimate(prior_id="a", expected_games=3, minutes=15, source_ids=("forecast",)),
        ),
    )
    missing = TeamMember(id="unknown", team_id="A", catalog_id=None, estimates=())
    outside = (
        TeamBoxPrior(member_id="outside", priors=(prior,), source_ids=("forecast",), notes=()),
        TeamBoxPrior(member_id="unknown", priors=(), source_ids=(), notes=()),
    )
    i = i.model_copy(
        update={"team_members": (*i.team_members, member, missing), "outside_priors": outside}
    )
    _, audits = calculate_with_offense(i, "0" * 64)
    a = audits[0]
    assert "unknown" in a.unmodeled_ids
    assert any(p.member_id == "outside" for p in a.allocations)
    assert a.reserved_possessions == pytest.approx(a.possession_budget * a.residual_minutes / 240)
    assert a.after + a.reserved_possessions <= a.possession_budget + 1e-8
    incomplete = i.model_copy(update={"outside_priors": outside[:1]})
    with pytest.raises(DataError, match="roster coverage"):
        calculate(incomplete, "0" * 64)
    forged = prior.model_copy(update={"minutes": 16.0})
    with pytest.raises(DataError, match="inconsistent participation"):
        validate_offense_input(
            i.model_copy(
                update={
                    "outside_priors": (
                        outside[0].model_copy(update={"priors": (forged,)}),
                        outside[1],
                    )
                }
            ),
            i.config.model,
        )


def test_invalid_offense_settings_fail_at_startup(offense_case):
    model = offense_case.config.model
    validate_team_offense(model, offense_case.config.season)
    for update in (
        {"used_terms": ()},
        {"scaled_stats": ("FGM",)},
        {"historical_minute_tolerance": 13.0},
        {"second_chance_stat": "FGA"},
    ):
        bad = model.model_copy(
            update={"team_offense": model.team_offense.model_copy(update=update)}
        )
        with pytest.raises(ConfigError, match="team_offense"):
            validate_team_offense(bad, offense_case.config.season)
    with pytest.raises(DataError, match="team coverage"):
        validate_offense_input(offense_case.model_copy(update={"offense_baselines": ()}), model)


def test_previous_team_baseline_screens_incomplete_games_and_normalizes_overtime(offense_case):
    model = offense_case.config.model
    axes = (*model.projection.stat_ids, model.preparation.minutes_stat)
    games = []
    for game, minutes in (("regulation", 240), ("overtime", 265), ("incomplete", 100)):
        stats = [10.0] * len(model.projection.stat_ids) + [float(minutes)]
        for pid in range(5):
            games.append(
                PlayerGame(
                    player_id=str(pid),
                    team_id="A",
                    game_id=game,
                    stats=tuple(
                        StatValue(id=s, value=v / 5, missing_reason=None)
                        for s, v in zip(axes, stats, strict=True)
                    ),
                    source_id="history",
                )
            )
    data = SourceData(
        current=(),
        previous=(),
        history=tuple(games),
        games=(),
        players=(ProviderPlayer(provider="espn", id="0", name="Outside", team_id="A"),),
        counts=(),
        actual=(),
    )
    (baseline,) = offense_baselines(data, model)
    assert baseline.game_ids == ("overtime", "regulation")
    assert baseline.excluded_game_ids == ("incomplete",)
    assert baseline.stats[0] == pytest.approx((10 + 10 * 240 / 265) / 2)
    bad = model.model_copy(
        update={
            "team_offense": model.team_offense.model_copy(update={"historical_minimum_games": 3})
        }
    )
    with pytest.raises(DataError, match="insufficient"):
        offense_baselines(data, bad)


def test_unobserved_outside_member_is_not_a_zero_forecast(offense_case):
    member = TeamMember(id="espn:new", team_id="A", catalog_id=None, estimates=())
    box = outside_prior(member, None, (), (), offense_case.config.model, ())
    assert box.priors == ()
    assert box.notes[0].kind == "unavailable"


def test_double_double_is_recomputed_when_usage_crosses_threshold(offense_case, budget_case):
    players = []
    for player in budget_case.players:
        priors = []
        for prior in player.priors:
            stats = list(prior.stats)
            stats[0] += 5
            stats[1] += 10
            stats[4] = 2 * stats[0] + stats[2] + stats[5]
            stats[6] = 12.0
            priors.append(prior.model_copy(update={"stats": tuple(stats)}))
        players.append(
            player.model_copy(update={"priors": tuple(priors), "history": (tuple(stats),)})
        )
    baseline = calculate(budget_case.model_copy(update={"players": tuple(players)}), "0" * 64)
    result = calculate(offense_case.model_copy(update={"players": tuple(players)}), "0" * 64)
    assert all(p.stats[-1] > 0.9 for p in baseline.projections)
    assert all(p.stats[-1] < 0.1 for p in result.projections)
    assert any(
        a.covariance != b.covariance
        for a, b in zip(baseline.projections, result.projections, strict=True)
    )


@pytest.mark.parametrize("budget", [9.9, 10.0, 10.1, 15.0, 20.0])
def test_positive_usage_is_shared_continuously_across_source_coverage(offense_case, budget):
    from fba.contracts.projection import PlayerOffenseAllocation
    from fba.formulas.team_offense import allocate_usage

    stats = (4.0, 10.0, 0.0, 0.0, 8.0, 0.0, 3.0, 1.0, 8.0, 0.0, 0.0, 0.0)
    rows = tuple(
        PlayerOffenseAllocation(
            member_id=str(i),
            catalog_id=None,
            expected_games=1,
            minutes=10,
            prior_coverage=coverage,
            before=stats,
            after=stats,
            usage_factor=1,
            assist_factor=1,
        )
        for i, coverage in enumerate((1.0, 0.0))
    )
    high, low = allocate_usage(rows, budget, 1, offense_case.config.model)
    assert high.usage_factor == low.usage_factor == pytest.approx(budget / 20)
    assert high.after[1] > 0 and low.after[1] > 0
    assert high.assist_factor == low.assist_factor == 0.5
    assert high.after[8] + low.after[8] == high.after[0] + low.after[0]
    assert high.after[4] == low.after[4] == pytest.approx(8 * budget / 20)
    assert allocate_usage(tuple(reversed(rows)), budget, 1, offense_case.config.model) == (
        high,
        low,
    )


def test_outside_forecast_imputation_has_explicit_donor_evidence(offense_case):
    from fba.contracts.data import Forecast

    model = offense_case.config.model
    stats = offense_case.players[0].priors[0].stats
    member = TeamMember(
        id="espn:new",
        team_id="A",
        catalog_id=None,
        estimates=(
            MinuteEstimate(
                prior_id=model.preparation.forecast_prior_id,
                expected_games=3,
                minutes=15,
                source_ids=("forecast",),
            ),
        ),
    )
    totals = tuple(
        StatValue(
            id=s,
            value=None if s == "OREB" else v * 3,
            missing_reason="not supplied" if s == "OREB" else None,
        )
        for s, v in zip(model.projection.stat_ids, stats, strict=True)
    )
    forecast = Forecast(
        player_id="new",
        expected_games=3,
        totals=(*totals, StatValue(id="MIN", value=45.0, missing_reason=None)),
        source_id="forecast",
    )
    box = outside_prior(member, forecast, (), (stats,), model, ("historical-source",))
    assert box.priors[0].stats[7] == pytest.approx(stats[7])
    assert "historical-source" in box.source_ids
    assert any(n.kind == "Assumption" and "all-player" in n.detail for n in box.notes)


def test_input_and_model_versions_must_match(offense_case, budget_case):
    from fba.adapters.team_offense import offense_sources

    bad = offense_case.model_copy(update={"config": budget_case.config})
    with pytest.raises(ConfigError, match="matching offense model"):
        calculate(bad, "0" * 64)
    # Adapter rejects before reading any frozen sources, with the same typed failure.
    with pytest.raises(ConfigError, match="matching offense model"):
        offense_sources(None, None, budget_case.config, ())
    bad = budget_case.model_copy(update={"config": offense_case.config})
    with pytest.raises(ConfigError, match="rebuild an input"):
        calculate(bad, "0" * 64)
