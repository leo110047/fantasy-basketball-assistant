import json

import pytest
from test_auction import player
from test_calculation import projection_bundle as projection_bundle
from test_team_minutes import budget_case as budget_case
from test_team_offense import offense_case as offense_case

from fba.adapters.auction import prepare_auction, prepare_management
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import ModelDocument, TeamConstraintModel
from fba.data.codec import canonical, digest
from fba.formulas.team_minutes import minute_allocations
from fba.projection.calculation import calculate_with_offense


def controlled(inputs, minutes="enforce", offense="enforce"):
    data = inputs.config.model.model_dump(mode="json")
    data.update(
        format_version=12,
        availability_tail={
            "lower_anchor_games": 3,
            "evidence": data["projection"]["evidence"],
        },
        team_constraints={"minutes": minutes, "offense": offense},
    )
    model = ModelDocument.model_validate_json(json.dumps(data)).root
    assert isinstance(model, TeamConstraintModel)
    return inputs.model_copy(update={"config": inputs.config.model_copy(update={"model": model})})


def test_default_policies_preserve_previous_numeric_results(offense_case):
    inputs = controlled(offense_case)
    current, audit = calculate_with_offense(inputs, "0" * 64)
    legacy = inputs.config.model.model_dump(mode="json", exclude={"team_constraints"})
    legacy["format_version"] = 11
    model = ModelDocument.model_validate_json(json.dumps(legacy)).root
    before, prior_audit = calculate_with_offense(
        inputs.model_copy(update={"config": inputs.config.model_copy(update={"model": model})}),
        "0" * 64,
    )
    assert current.projections == before.projections
    assert current.valuation == before.valuation
    assert audit == prior_audit
    assert current.algorithm == "configurable-team-projection-v1"
    assert before.algorithm == "bounded-availability-projection-v1"


@pytest.mark.parametrize("minutes", ["audit", "enforce"])
@pytest.mark.parametrize("offense", ["audit", "enforce"])
def test_policies_are_independent_and_keep_audits_and_health(offense_case, minutes, offense):
    source = controlled(offense_case, minutes, offense)
    model = source.config.model
    source = source.model_copy(
        update={
            "config": source.config.model_copy(
                update={
                    "model": model.model_copy(
                        update={
                            "team_minutes": model.team_minutes.model_copy(
                                update={"unmodeled_reserve_minutes": 230.0}
                            )
                        }
                    )
                }
            )
        }
    )
    result, offense_audit = calculate_with_offense(source, "0" * 64)
    minute_audit = minute_allocations(source)
    assert minute_audit and offense_audit
    for team in minute_audit:
        if minutes == "enforce":
            assert team.after < team.before
            assert team.after <= team.budget - 230
        else:
            assert team.after == team.before
        assert any(a.expected_games_before > 0 for a in team.allocations)
    if offense == "enforce":
        assert any(r.usage_factor < 1 for t in offense_audit for r in t.allocations)
    else:
        assert all(r.before == r.after for t in offense_audit for r in t.allocations)
    for projection in result.projections:
        assert projection.unconstrained_games == 1.5
        if minutes == "audit":
            assert projection.expected_games == projection.unconstrained_games
        else:
            assert projection.expected_games < projection.unconstrained_games
    assert source.config.model.health == model.health
    assert source.config.model.pricing == model.pricing
    assert source.config.model.management == model.management
    catalog = tuple(player(n).model_copy(update={"id": p.id}) for n, p in enumerate(source.players))
    managed = prepare_management(source, result, catalog, model.health)
    for row in managed.players:
        assert row.unconstrained_games == 1.5
        assert row.healthy_games == pytest.approx(
            row.season_games - (row.season_games - 1.5) * model.health.injury_share
        )
        if minutes == "enforce":
            assert row.expected_games < row.unconstrained_games < row.healthy_games
        else:
            assert row.expected_games == row.unconstrained_games < row.healthy_games


@pytest.mark.parametrize("minutes", ["audit", "enforce"])
def test_manual_overflow_is_reported_in_audit_and_rejected_in_enforce(offense_case, minutes):
    source = controlled(offense_case, minutes, "audit")
    source = source.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"expected_games_override": 4.0}) for p in source.players
            ),
            "config": source.config.model_copy(
                update={
                    "model": source.config.model.model_copy(
                        update={
                            "team_minutes": source.config.model.team_minutes.model_copy(
                                update={"unmodeled_reserve_minutes": 230.0}
                            )
                        }
                    )
                }
            ),
        }
    )
    if minutes == "enforce":
        with pytest.raises(DataError, match="manual expected games exceed team budget"):
            calculate_with_offense(source, "0" * 64)
    else:
        result, _ = calculate_with_offense(source, "0" * 64)
        assert all(p.expected_games == 4 for p in result.projections)
        assert all(t.after == t.before > t.budget - 230 for t in minute_allocations(source))


def test_audit_still_rejects_missing_roster_and_offense_sources(offense_case):
    inputs = controlled(offense_case, "audit", "audit")
    for change, message in (
        ({"team_members": inputs.team_members[1:]}, "coverage mismatch"),
        ({"offense_baselines": ()}, "team coverage"),
    ):
        with pytest.raises(DataError, match=message):
            calculate_with_offense(inputs.model_copy(update=change), "0" * 64)


@pytest.mark.parametrize("policy", ["minutes", "offense"])
def test_auction_policy_change_requires_a_new_projection(
    offense_case, tmp_path, monkeypatch, policy
):
    inputs = controlled(offense_case)
    result, _ = calculate_with_offense(inputs, "0" * 64)
    (tmp_path / "results").mkdir()
    payload = canonical(result)
    (tmp_path / "results" / f"calculation-{digest(payload)}.json").write_bytes(payload)
    # Isolate the frozen-input read; the real adapter must reject before publishing anything.
    monkeypatch.setattr(
        "fba.adapters.forecast_scenarios.load_projection", lambda _: (inputs, "0" * 64)
    )
    data = inputs.config.model.model_dump(mode="json")
    data["team_constraints"][policy] = "audit"
    model = tmp_path / "model.json"
    model.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="team_constraints.*rebuild"):
        prepare_auction(tmp_path, model, tmp_path / "auction")
    assert not (tmp_path / "auction").exists()


@pytest.mark.parametrize(
    "change", [None, {}, {"minutes": "audit"}, {"minutes": "off", "offense": "enforce"}]
)
def test_explicit_policies_are_required_and_validated(offense_case, change):
    data = controlled(offense_case).config.model.model_dump(mode="json")
    if change is None:
        del data["team_constraints"]
    else:
        data["team_constraints"] = change
    with pytest.raises(ValueError, match="team_constraints"):
        ModelDocument.model_validate_json(json.dumps(data))
