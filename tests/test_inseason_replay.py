from datetime import timedelta

import pytest
from inseason_support import DEFAULTS, fixture

from fba.contracts.base import DataError
from fba.contracts.inseason import InseasonPreferences
from fba.contracts.inseason_replay import PolicyReplayStudy, ReplayCase
from fba.inseason.replay import run_policy_replay


def study():
    league, params, players, priors, ledger, snapshot, now = fixture()
    league = league.model_copy(update={"adds_per_week": 1})
    end = now + timedelta(days=3)
    final_games = tuple(
        g.model_copy(update={"status": "completed" if g.tipoff < end else "scheduled"})
        for g in players.games
    )
    boxes = list(players.boxes)
    for game in final_games:
        if game.status == "completed":
            player = next(p for p in players.players if p.team_id == game.home)
            sample = next(b for b in boxes if b.player_id == player.id)
            boxes.append(
                sample.model_copy(
                    update={
                        "game_id": game.id,
                        "played_at": game.tipoff,
                        "known_at": game.tipoff + timedelta(hours=3),
                    }
                )
            )
    final_players = players.model_copy(
        update={"as_of": end, "games": final_games, "boxes": tuple(boxes)}
    )
    final_snapshot = snapshot.model_copy(
        update={
            "as_of": end,
            "actual": tuple(
                s.model_copy(update={"final": True, "through": end}) for s in snapshot.actual
            ),
        }
    )
    case = ReplayCase(
        league=league,
        players=players,
        priors=priors,
        ledger=ledger,
        snapshot=snapshot,
        as_of=now,
        week_id="2",
        final_players=final_players,
        final_snapshot=final_snapshot,
    )
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    return PolicyReplayStudy(
        cases=(case,),
        preferences=prefs.model_copy(update={"reserve_adds": 0}),
        recall_target=0.95,
        minimum_cases=2,
        oracle_max_plans=1000,
        evidence=params.group_evidence,
    ), params


def test_replay_reports_three_policies_and_sparse_cases_cannot_pass():
    data, params = study()
    report = run_policy_replay(data, params)
    assert set(report.mean_scores) == {"unchanged", "ranking", "recommended"}
    assert report.rows[0].full_candidates > 0
    assert report.recall is not None
    assert report.beam_recall is not None
    assert not report.sufficient_cases
    assert not report.recall_passed and not report.policy_passed
    assert report.rows[0].outcomes[0].moves == ()


def test_future_outcomes_change_actual_but_never_choices_or_forecasts():
    data, params = study()
    original = run_policy_replay(data, params)
    case = data.cases[0]
    changed_boxes = tuple(
        b.model_copy(update={"stats": {k: 0.0 for k in b.stats}}) if b.played_at > case.as_of else b
        for b in case.final_players.boxes
    )
    changed = case.model_copy(
        update={"final_players": case.final_players.model_copy(update={"boxes": changed_boxes})}
    )
    result = run_policy_replay(data.model_copy(update={"cases": (changed,)}), params)
    for before, after in zip(original.rows[0].outcomes, result.rows[0].outcomes, strict=True):
        assert before.forecast == after.forecast
        assert before.moves == after.moves
    assert original.mean_scores != result.mean_scores


def test_duplicate_weeks_and_future_decision_snapshot_are_rejected():
    data, params = study()
    with pytest.raises(DataError, match="duplicate"):
        run_policy_replay(data.model_copy(update={"cases": data.cases * 2}), params)
    case = data.cases[0]
    case = case.model_copy(
        update={
            "players": case.players.model_copy(update={"as_of": case.as_of + timedelta(days=1)})
        }
    )
    with pytest.raises(DataError, match="decision time"):
        run_policy_replay(data.model_copy(update={"cases": (case,)}), params)


def test_exhaustive_replay_measures_two_add_plans_and_fails_when_oracle_is_truncated():
    from fba.contracts.inseason import CalculationTimeout

    data, params = study()
    case = data.cases[0]
    case = case.model_copy(update={"league": case.league.model_copy(update={"adds_per_week": 2})})
    data = data.model_copy(update={"cases": (case,)})
    report = run_policy_replay(data, params)
    assert report.rows[0].full_plan_count > report.rows[0].full_candidates
    assert report.rows[0].search_retained_best is not None
    with pytest.raises(CalculationTimeout, match="oracle_max_plans"):
        run_policy_replay(data.model_copy(update={"oracle_max_plans": 1}), params)
