import json
from pathlib import Path

import pytest

from fba.contracts.config import Category, LeagueRules, Linear, ModelDocument, Term
from fba.contracts.projection import Projected
from fba.core.valuation import fit_ruler, value


@pytest.fixture
def frozen():
    root = Path(__file__).parents[1]
    data = json.loads((root / "tests/fixtures/valuation-reference.json").read_text())
    rows = tuple(
        Projected(
            id=r["id"],
            expected_games=r["expected_games"],
            minutes=r["minutes"],
            stats=tuple(r["stats"]),
            covariance=(),
        )
        for r in data["rows"]
        if r["fair"] is not None
    )
    league = LeagueRules.model_validate_json((root / "examples/2026-27/league.json").read_bytes())
    model = ModelDocument.model_validate_json(
        (root / "examples/2026-27/model.json").read_bytes()
    ).root
    return data, rows, league, model


def test_all_locked_reference_fair_values_and_shuffle(frozen):
    data, players, league, model = frozen
    ids = tuple(r["id"] for r in data["rows"])
    axes = (*model.projection.stat_ids, model.projection.threshold_stat)
    ruler = fit_ruler(players, axes, league, model.valuation)
    actual = value(players, ids, axes, league, model.valuation, 82, ruler)
    expected = {r["id"]: r["fair"] for r in data["rows"]}
    assert len(actual.players) == 439
    for row in actual.players:
        if expected[row.id] is None:
            assert row.fair is None and row.unavailable_reason
        else:
            assert abs(row.fair - expected[row.id]) <= 0.01
    assert (
        value(
            tuple(reversed(players)), tuple(reversed(ids)), axes, league, model.valuation, 82, ruler
        )
        == actual
    )
    total = sum(sorted((p.fair or 0 for p in actual.players), reverse=True)[:140])
    assert total == pytest.approx(2800, abs=1e-6)


@pytest.mark.parametrize("teams", [12, 16])
def test_team_and_category_changes_use_configuration(frozen, teams):
    data, players, league, model = frozen
    categories = tuple(c for c in league.categories if c.id not in ("OREB", "DD", "A/T"))
    categories += (
        Category(
            id="TO",
            label="TO",
            formula=Linear(kind="linear", terms=(Term(stat_id="TO", coefficient=1.0),)),
            direction="lower",
            comparison_decimals=6,
            tie_value=0.5,
        ),
    )
    rules = league.model_copy(update={"teams": teams, "categories": categories})
    result = value(
        players,
        tuple(r["id"] for r in data["rows"]),
        (*model.projection.stat_ids, model.projection.threshold_stat),
        rules,
        model.valuation,
        82,
        fit_ruler(
            players,
            (*model.projection.stat_ids, model.projection.threshold_stat),
            rules,
            model.valuation,
        ),
    )
    count = teams * (len(rules.starter_slots) + rules.bench_slots)
    total = sum(sorted((r.fair or 0 for r in result.players), reverse=True)[:count])
    assert total == pytest.approx(teams * rules.budget, abs=1e-6)
    assert all(len(r.categories) == 9 for r in result.players if r.fair is not None)
