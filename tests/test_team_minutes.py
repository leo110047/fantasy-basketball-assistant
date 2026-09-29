import json

import pytest
from test_calculation import legacy_projection_bundle as legacy_projection_bundle
from test_calculation import projection_bundle as projection_bundle

from fba.adapters.calculation import load_calculation_input
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import ModelDocument, TeamBudgetModel
from fba.contracts.projection import (
    BudgetedInput,
    CalibratedInput,
    MinuteEstimate,
    PreparedPlayer,
    TeamMember,
)
from fba.core.calculation import calculate
from fba.core.team_minutes import minute_allocations, validate_minutes


@pytest.fixture
def budget_case(projection_bundle):
    original, _ = load_calculation_input(projection_bundle, CalibratedInput)
    model = original.config.model.model_dump(mode="json")
    model.update(
        format_version=7,
        team_minutes={
            "regulation_minutes": 48,
            "players_on_court": 5,
            "overtime_minutes_per_game": 0,
            "unmodeled_reserve_minutes": 12,
            "evidence": model["projection"]["evidence"],
        },
    )
    parameters = ModelDocument.model_validate_json(json.dumps(model)).root
    assert isinstance(parameters, TeamBudgetModel)
    players = tuple(
        PreparedPlayer(**p.model_dump(), expected_games_override=None, history_pool_id=None)
        for p in original.players
    )
    members = tuple(
        TeamMember(id=p.id, catalog_id=p.id, team_id=p.team_id, estimates=()) for p in players
    )
    return BudgetedInput(
        format_version=5,
        config=original.config.model_copy(update={"model": parameters}),
        artifacts=original.artifacts,
        calibration=original.calibration,
        calibration_snapshot=original.calibration_snapshot,
        players=players,
        teams=original.teams,
        notes=(),
        history_pools=(),
        team_members=members,
    )


def test_complete_roster_caps_expected_minutes_not_sum_of_mpg(budget_case):
    i = budget_case
    # Six players at 45 MPG, expected GP 3 * calibration 0.5 over four games = 101.25, not 270.
    players = tuple(
        p.model_copy(
            update={"priors": tuple(q.model_copy(update={"minutes": 45.0}) for q in p.priors)}
        )
        for p in i.players
    )
    i = i.model_copy(update={"players": players})
    a = minute_allocations(i)
    assert all(x.before == x.after for x in a)
    outside = tuple(
        TeamMember(
            id=f"outside-{n}",
            catalog_id=None,
            team_id="A",
            estimates=(
                MinuteEstimate(
                    prior_id=i.config.model.preparation.forecast_prior_id,
                    expected_games=4,
                    minutes=45,
                    source_ids=("forecast",),
                ),
            ),
        )
        for n in range(7)
    )
    unknown = TeamMember(id="unknown", catalog_id=None, team_id="A", estimates=())
    constrained = i.model_copy(update={"team_members": (*i.team_members, *outside, unknown)})
    a = minute_allocations(constrained)
    assert a[0].before > a[0].budget and a[0].after < a[0].before
    assert a[0].after == pytest.approx(228) and a[0].reserve == pytest.approx(12)
    assert a[0].unmodeled_ids == ("unknown",)
    assert a[1].before == a[1].after
    before = calculate(i, "0" * 64)
    after = calculate(constrained, "1" * 64)
    old = {p.id: p for p in before.projections}
    for p in after.projections:
        allocation = next(q for team in a for q in team.allocations if q.catalog_id == p.id)
        assert p.expected_games == pytest.approx(allocation.expected_games_after, abs=1e-8)
        assert p.unconstrained_games == old[p.id].expected_games
        assert p.minutes == old[p.id].minutes
        assert p.stats == old[p.id].stats
        assert p.covariance == old[p.id].covariance
        assert p.stats[4] == pytest.approx(2 * p.stats[0] + p.stats[2] + p.stats[5], abs=1e-7)
    shuffled = constrained.model_copy(
        update={
            "players": tuple(reversed(constrained.players)),
            "team_members": tuple(reversed(constrained.team_members)),
        }
    )
    assert calculate(shuffled, "1" * 64) == after
    with pytest.raises(DataError, match="allocated minutes"):
        validate_minutes(
            tuple(p.model_copy(update={"minutes": 1000.0}) for p in after.projections),
            constrained,
            a,
        )


@pytest.mark.parametrize("failure", ["missing", "duplicate", "wrong_team", "unprepared"])
def test_team_coverage_is_required_not_inferred_from_yahoo_subset(budget_case, failure):
    i = budget_case
    if failure == "missing":
        i = i.model_copy(update={"team_members": i.team_members[1:]})
    if failure == "duplicate":
        i = i.model_copy(update={"team_members": (*i.team_members, i.team_members[0])})
    if failure == "wrong_team":
        i = i.model_copy(
            update={
                "team_members": (
                    i.team_members[0].model_copy(update={"team_id": "B"}),
                    *i.team_members[1:],
                )
            }
        )
    if failure == "unprepared":
        i = CalibratedInput(
            format_version=3,
            config=i.config,
            artifacts=i.artifacts,
            calibration=i.calibration,
            calibration_snapshot=i.calibration_snapshot,
            players=i.players,
            teams=i.teams,
        )
    with pytest.raises((DataError, ConfigError), match="team_minutes"):
        calculate(i, "0" * 64)


def test_budget_model_schema_requires_all_settings(budget_case):
    model = budget_case.config.model.model_dump(mode="json")
    del model["team_minutes"]["unmodeled_reserve_minutes"]
    with pytest.raises(ValueError):
        ModelDocument.model_validate_json(json.dumps(model))


def test_rotation_losses_do_not_create_il_credit(budget_case):
    from test_auction import player

    from fba.adapters.auction import prepare_management
    from fba.contracts.season import RoleManagedPlayer

    i = budget_case
    adjusted = i.config.model.model_copy(
        update={
            "team_minutes": i.config.model.team_minutes.model_copy(
                update={"unmodeled_reserve_minutes": 230.0}
            )
        }
    )
    i = i.model_copy(update={"config": i.config.model_copy(update={"model": adjusted})})
    result = calculate(i, "0" * 64)
    catalog = tuple(player(n).model_copy(update={"id": p.id}) for n, p in enumerate(i.players))
    managed = prepare_management(i, result, catalog)
    assert any(p.expected_games < p.unconstrained_games for p in managed.players)
    assert all(
        isinstance(p, RoleManagedPlayer) and p.healthy_games == p.season_games
        for p in managed.players
    )


def test_complete_population_migration_requires_source_rebuild(legacy_projection_bundle, tmp_path):
    from pathlib import Path

    from fba.adapters.migration import migrate_projection

    model = tmp_path / "budget-model.json"
    model.write_bytes((Path(__file__).parents[1] / "examples/2026-27/model.json").read_bytes())
    output = tmp_path / "converted-budget"
    with pytest.raises(ConfigError, match="require project"):
        migrate_projection(legacy_projection_bundle, model, tmp_path / "calibration", output)
    assert not output.exists()


def test_fractional_prior_games_share_projection_rounding_order(budget_case):
    i = budget_case
    first = i.players[0]
    first = first.model_copy(
        update={
            "priors": tuple(p.model_copy(update={"expected_games": 1.504}) for p in first.priors)
        }
    )
    model = i.config.model.model_copy(
        update={"valuation": i.config.model.valuation.model_copy(update={"result_decimals": 2})}
    )
    i = i.model_copy(
        update={
            "players": (first, *i.players[1:]),
            "config": i.config.model_copy(update={"model": model}),
            "calibration": i.calibration.model_copy(update={"intercept": 0.0, "slope": 2.0}),
        }
    )
    result = calculate(i, "0" * 64)
    player = next(p for p in result.projections if p.id == first.id)
    assert player.expected_games == player.unconstrained_games == 3.0
    allocation = next(
        p for t in minute_allocations(i) for p in t.allocations if p.catalog_id == first.id
    )
    assert allocation.expected_games_before == player.unconstrained_games


def test_manual_games_are_reserved_before_automatic_team_allocations(budget_case):
    i = budget_case
    players = tuple(
        p.model_copy(
            update={
                "priors": tuple(q.model_copy(update={"minutes": 45.0}) for q in p.priors),
                "expected_games_override": 3.0 if p.id == i.players[0].id else None,
            }
        )
        for p in i.players
    )
    model = i.config.model.model_copy(
        update={
            "team_minutes": i.config.model.team_minutes.model_copy(
                update={"unmodeled_reserve_minutes": 150.0}
            )
        }
    )
    i = i.model_copy(
        update={"players": players, "config": i.config.model_copy(update={"model": model})}
    )
    result = calculate(i, "0" * 64)
    fixed = next(p for p in result.projections if p.id == players[0].id)
    assert fixed.expected_games == fixed.unconstrained_games == 3.0
    allocation = next(t for t in minute_allocations(i) if t.team_id == players[0].team_id)
    assert allocation.after == pytest.approx(90.0)
    assert any(p.expected_games_after < p.expected_games_before for p in allocation.allocations)
    impossible = tuple(p.model_copy(update={"expected_games_override": 4.0}) for p in players)
    with pytest.raises(DataError, match="manual expected games exceed team budget"):
        calculate(i.model_copy(update={"players": impossible}), "0" * 64)
