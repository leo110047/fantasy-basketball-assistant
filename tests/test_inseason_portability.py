"""A shared synthetic reference for cross-platform ordering and numeric parity."""

import json
import os
from pathlib import Path

import pytest
from inseason_support import DEFAULTS, simulation
from test_inseason_review import final_scores, prediction

from fba.contracts.inseason import InseasonPreferences
from fba.data.codec import canonical, digest
from fba.inseason.recommendations import search_adds
from fba.inseason.review import weekly_review
from fba.inseason.today import today
from fba.inseason.trades import evaluate_trade


def portable_results():
    preferences = InseasonPreferences.model_validate_json(
        (DEFAULTS / "preferences.json").read_bytes()
    )
    results = {}
    for year, teams, mode in ((2025, 2, "h2h_one_win"), (2026, 10, "h2h_each_category")):
        sim = simulation(year=year, teams=teams, mode=mode)
        projection = sim.projection(sim.as_of.astimezone(sim.zone).date())
        week = sim.week("team0", "team1", "2")
        plans = search_adds(sim, preferences, "2", lambda value: None)
        trade = evaluate_trade(sim, "team0", "team1", ("p0",), ("p3",))
        daily = today(sim, projection.on, "Asia/Taipei")
        recorded = prediction(sim)
        review = weekly_review(sim.league, sim.params, final_scores(sim), (recorded,), "2", {})
        results[str(year)] = {
            "fixture_sha256": digest(canonical(sim.snapshot)),
            "parameters_sha256": digest(canonical(sim.params)),
            "players": [
                {"id": p.player.id, "minutes": p.minutes, "rates": p.rates, "expected": p.expected}
                for p in projection.players
            ],
            "week": {
                "score": week.score,
                "categories": [c.probability for c in week.categories],
                "lineups": [d.model_dump(mode="json") for d in week.lineups],
            },
            "plans": [
                {
                    "moves": [m.model_dump(mode="json") for m in p.moves],
                    "score": p.score,
                    "id": p.id,
                }
                for p in plans
            ],
            "trade": {
                "mine": trade.mine_delta,
                "opponent": trade.opponent_delta,
                "acceptance": trade.acceptance,
                "playoffs": trade.playoff_delta,
            },
            "today": {
                "lineup": daily.lineup.model_dump(mode="json"),
                "actions": [
                    {"kind": a.kind, "player": a.player_id, "slot": a.slot} for a in daily.actions
                ],
            },
            "review": {"category_brier": review.category_brier, "week_brier": review.week_brier},
        }
    return results


def assert_same(actual, reference):
    if isinstance(reference, dict):
        assert actual.keys() == reference.keys()
        for key in reference:
            assert_same(actual[key], reference[key])
    elif isinstance(reference, list):
        assert len(actual) == len(reference)
        for left, right in zip(actual, reference, strict=True):
            assert_same(left, right)
    elif isinstance(reference, float):
        # Each platform within half the allowed pairwise tolerance ensures
        # every macOS/Windows/Linux pair differs by at most 1e-9.
        assert actual == pytest.approx(reference, abs=5e-10, rel=0)
    else:
        assert actual == reference


def test_shared_two_season_reference_preserves_ordering_and_numeric_results():
    actual = portable_results()
    reference = json.loads(
        Path(__file__).with_name("fixtures").joinpath("inseason-portable.json").read_bytes()
    )
    assert_same(actual, reference)
    if output := os.environ.get("FBA_PORTABILITY_REPORT"):
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "scope": "synthetic two-season portability; not live Yahoo validation",
                    "results": actual,
                },
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
