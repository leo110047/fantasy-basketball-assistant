"""Historical corrections must not rewrite information at an earlier decision."""

from datetime import timedelta

import pytest
from inseason_support import parameters
from test_inseason_backtest import study_season

from fba.contracts.base import DataError
from fba.inseason.backtest import checkpoint_errors, component_rows
from fba.inseason.parameter_fit import production_trial


def late_correction(season):
    old = season.players.boxes[0]
    correction = old.model_copy(
        update={
            "known_at": season.players.as_of - timedelta(hours=1),
            "minutes": old.minutes * 3,
            "stats": {**old.stats, "AST": old.stats["AST"] * 100, "FGM": 0.0},
        }
    )
    return season.model_copy(
        update={
            "players": season.players.model_copy(
                update={"boxes": (*season.players.boxes, correction)}
            )
        }
    )


@pytest.mark.parametrize("field", ("rates", "minutes", "FG%", "production"))
def test_late_correction_keeps_original_checkpoint_prediction(field):
    season = study_season(2025)
    changed = late_correction(season)

    def measure(data):
        if field == "rates":
            return checkpoint_errors(data, dict.fromkeys(data.league.base_stats, 100.0), (5,))
        if field == "production":
            return production_trial(data, parameters(), (5,))
        return component_rows(data, (5,), field, 100.0, 5.0)

    assert measure(changed) == measure(season)


@pytest.mark.parametrize("field", ("rates", "minutes", "FG%"))
def test_checkpoint_rejects_prior_published_after_decision(field):
    season = study_season(2025)
    season = season.model_copy(
        update={"priors": season.priors.model_copy(update={"known_at": season.players.as_of})}
    )
    with pytest.raises(DataError, match="prior.*decision"):
        if field == "rates":
            checkpoint_errors(season, dict.fromkeys(season.league.base_stats, 100.0), (5,))
        else:
            component_rows(season, (5,), field, 100.0, 5.0)


def test_checkpoint_uses_publication_order_and_preserves_corrected_outcomes():
    from fba.inseason.checkpoint_history import checkpoint_history

    season = study_season(2025)
    original = checkpoint_history(season, 5)[0]
    changed = checkpoint_history(late_correction(season), 5)[0]
    assert changed.as_of == original.as_of
    assert changed.past == original.past
    assert changed.future == original.future
    delayed = season.players.boxes[0].model_copy(update={"known_at": season.players.as_of})
    missing_first = season.model_copy(
        update={
            "players": season.players.model_copy(
                update={"boxes": (delayed, *season.players.boxes[1:])}
            )
        }
    )
    later = checkpoint_history(missing_first, 5)[0]
    assert later.as_of > original.as_of
    assert len(later.past) == 5
    assert delayed.game_id not in {b.game_id for b in later.past}


def test_future_results_affect_targets_without_changing_checkpoint_inputs():
    from fba.inseason.checkpoint_history import checkpoint_history

    season = study_season(2025)
    before = checkpoint_history(season, 5)[0]
    target = before.future[0]
    corrected = target.model_copy(
        update={
            "known_at": season.players.as_of - timedelta(hours=1),
            "stats": {**target.stats, "AST": 0.0},
        }
    )
    changed = season.model_copy(
        update={
            "players": season.players.model_copy(
                update={"boxes": (*season.players.boxes, corrected)}
            )
        }
    )
    after = checkpoint_history(changed, 5)[0]
    assert (after.as_of, after.past) == (before.as_of, before.past)
    assert after.future != before.future
    assert any(b.game_id == corrected.game_id and b.stats["AST"] == 0 for b in after.future)


def test_undated_postseason_capture_does_not_manufacture_earlier_decisions():
    from fba.inseason.checkpoint_history import checkpoint_history

    season = study_season(2025)
    season = season.model_copy(
        update={
            "players": season.players.model_copy(
                update={
                    "boxes": tuple(
                        b.model_copy(update={"known_at": season.players.as_of})
                        for b in season.players.boxes
                    )
                }
            )
        }
    )
    assert checkpoint_history(season, 5) == ()
    with pytest.raises(DataError, match="no holdout observations"):
        checkpoint_errors(season, dict.fromkeys(season.league.base_stats, 100.0), (5,))
