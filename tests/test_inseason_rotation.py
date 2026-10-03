from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from inseason_support import fixture
from test_inseason_projection import entry, project

from fba.adapters.config import load_config
from fba.adapters.inseason_priors import read_priors
from fba.contracts.archive import ForecastArchive
from fba.contracts.base import DataError
from fba.contracts.inseason import AdjustmentLedger
from fba.contracts.projection import CalculationResult, Projected, Valuation
from fba.data.codec import canonical, digest
from fba.formulas.registry import evaluate
from fba.inseason.sampling import sample_game


def rotation_case(minutes, probabilities=None, known=None):
    inputs = list(fixture())
    count = len(minutes)
    source = inputs[2]
    players = tuple(
        p.model_copy(update={"team_id": "NBA0", "positions": ("PG",)})
        for p in source.players[:count]
    )
    inputs[2] = source.model_copy(update={"players": players, "boxes": ()})
    probabilities = probabilities if probabilities is not None else [1.0] * count
    known = set(range(count)) if known is None else set(known)
    priors = tuple(
        p.model_copy(update={"minutes": float(m), "appearance_probability": float(q)})
        for i, (p, m, q) in enumerate(
            zip(inputs[3].players[:count], minutes, probabilities, strict=True)
        )
        if i in known
    )
    inputs[3] = inputs[3].model_copy(update={"players": priors})
    return inputs


def forecast_file(tmp_path, games=41.0, minutes=30.0):
    config = load_config(
        *[Path(f"examples/2026-27/{name}.json") for name in ("league", "season", "model")]
    )
    axes = (*config.model.projection.stat_ids, config.model.projection.threshold_stat)
    league, _, players, _, _, _, now = fixture()
    stats = players.boxes[0].stats
    values = tuple(stats.get(s, 0.0) if minutes else 0.0 for s in axes)
    archive = ForecastArchive(
        format_version=1,
        config=config,
        season_games=82,
        calculation=CalculationResult(
            format_version=1,
            algorithm="synthetic participation import",
            input_sha256="a" * 64,
            config=config.refs,
            projections=(
                Projected(
                    id="p0", expected_games=games, minutes=minutes, stats=values, covariance=()
                ),
            ),
            valuation=Valuation(replacement_score=0.0, players=()),
        ),
        identities=(),
    )
    path = tmp_path / "forecast.json"
    path.write_bytes(canonical(archive))
    rules = league.model_copy(update={"season_id": config.season.season_id})
    return path, digest(path.read_bytes()), rules, now


def test_import_preserves_playing_opportunities_and_per_appearance_rates(tmp_path):
    priors = read_priors(*forecast_file(tmp_path))
    player = priors.players[0]
    assert player.appearance_probability == 0.5
    assert player.minutes == 30.0
    assert player.rates["FGA"] == pytest.approx(12 / 30)


def test_import_rejects_impossible_season_games_and_preserves_explicit_zero(tmp_path):
    with pytest.raises(DataError, match="expected games exceed"):
        read_priors(*forecast_file(tmp_path, games=83.0))
    player = read_priors(*forecast_file(tmp_path, games=0.0, minutes=0.0)).players[0]
    assert player.appearance_probability == player.minutes == 0.0
    assert set(player.rates.values()) == {0.0}


def test_no_history_preserves_half_season_exposure_and_caps_current_injury_risk():
    inputs = rotation_case([30], [0.5])
    p = project(inputs).players[0]
    assert p.minutes == 30 and p.probability == 0.5
    assert p.expected["FGA"] == pytest.approx(5)
    assert p.traces["expected:minutes"].result == 15
    source = inputs[2]
    inputs[2] = source.model_copy(
        update={"players": (source.players[0].model_copy(update={"status": "GTD"}),)}
    )
    # The season forecast already contains absence risk. Do not multiply the
    # same unknown injury/rotation decomposition by another 0.5.
    assert project(inputs).players[0].probability == 0.5


def test_old_import_cannot_silently_restore_full_participation():
    inputs = rotation_case([30])
    priors = inputs[3]
    inputs[3] = priors.model_copy(
        update={"players": (priors.players[0].model_copy(update={"appearance_probability": None}),)}
    )
    with pytest.raises(DataError, match="重新載入"):
        project(inputs)


def test_overfull_known_rotation_reduces_opportunities_without_cutting_conditional_minutes():
    result = project(rotation_case([35] * 8))
    assert all(p.minutes == 35 and p.probability == pytest.approx(6 / 7) for p in result.players)
    assert sum(p.traces["expected:minutes"].result for p in result.players) == pytest.approx(240)
    assert [p.expected["FGA"] for p in result.players] == pytest.approx(
        [10, 10.5, 11, 11.5, 12, 12.5, 13, 13.5]
    )


def test_peer_roles_share_only_residual_and_cannot_dilute_known_players():
    result = project(rotation_case([40] * 8, known=range(4)))
    assert all(p.probability == 1 and p.minutes == 40 for p in result.players[:4])
    assert all(p.probability == 0.5 and p.minutes == 40 for p in result.players[4:])
    assert sum(p.traces["expected:minutes"].result for p in result.players) == 240
    assert all(p.flags[0].kind == "missing_role" for p in result.players[4:])
    assert all(p.flags[0].observed is None for p in result.players[4:])


def test_no_residual_is_not_a_twenty_minute_role_and_unused_capacity_is_not_upside():
    full = project(rotation_case([40] * 7, known=range(6)))
    assert full.players[-1].probability == full.players[-1].expected["FGA"] == 0
    partial = project(rotation_case([30], [0.5]))
    assert partial.players[0].probability == 0.5
    assert partial.players[0].traces["expected:minutes"].result == 15


def test_manual_minutes_are_preserved_and_overfull_manual_requests_fail():
    inputs = rotation_case([40] * 7)
    change = entry(inputs, 48.0)
    result = project(inputs, ledger=AdjustmentLedger(format_version=1, entries=(change,)))
    assert result.players[0].minutes == 48 and result.players[0].probability == 1
    assert all(p.probability == 0.8 for p in result.players[1:])
    changes = tuple(
        change.model_copy(update={"id": f"manual:{i}", "player_id": p.id, "value": 40.0})
        for i, p in enumerate(inputs[2].players)
    )
    with pytest.raises(DataError, match="手調預期分鐘超過全隊"):
        project(inputs, ledger=AdjustmentLedger(format_version=1, entries=changes))


def test_explicit_dnps_reduce_probability_without_double_counting_zero_minutes():
    inputs = rotation_case([30], [0.5])
    source = inputs[2]
    original = fixture()[2].boxes[0]
    boxes = (
        original.model_copy(update={"minutes": 40.0}),
        original.model_copy(
            update={
                "game_id": "explicit-dnp",
                "played_at": original.played_at + timedelta(days=1),
                "minutes": 0.0,
                "stats": dict.fromkeys(original.stats, 0.0),
            }
        ),
    )
    inputs[2] = source.model_copy(update={"boxes": boxes})
    p = project(inputs).players[0]
    assert p.minutes == pytest.approx(32.5)  # (3 * 30 + 40) / (3 + 1)
    assert p.probability == 0.5  # (3 * 0.5 + 1) / (3 + 2)
    assert p.traces["expected:minutes"].result == 16.25
    for trace in p.traces.values():
        assert evaluate(trace.formula_id, **trace.inputs) == trace


def test_rotation_uses_current_team_and_keeps_order_and_future_data_boundaries():
    inputs = rotation_case([40] * 7, known=range(6))
    baseline = project(inputs)
    source = inputs[2]
    inputs[2] = source.model_copy(update={"players": tuple(reversed(source.players))})
    inputs[3] = inputs[3].model_copy(update={"players": tuple(reversed(inputs[3].players))})
    assert canonical(project(inputs)) == canonical(baseline)
    future = source.players[0].model_copy(
        update={"known_at": inputs[-1] + timedelta(days=1), "team_id": "FUTURE"}
    )
    inputs[2] = source.model_copy(update={"players": (*source.players, future)})
    assert canonical(project(inputs)) == canonical(baseline)
    moved = source.players[-1].model_copy(update={"team_id": "NEW"})
    inputs[2] = source.model_copy(update={"players": (*source.players[:-1], moved)})
    changed = project(inputs).players[-1]
    assert changed.player.team_id == "NEW" and changed.probability == 1


def test_an_explicit_zero_role_does_not_get_peer_minutes():
    inputs = rotation_case([0], [0])
    priors = inputs[3]
    inputs[3] = priors.model_copy(
        update={
            "players": (
                priors.players[0].model_copy(
                    update={
                        "rates": dict.fromkeys(priors.players[0].rates, 0.0),
                        "probabilities": {},
                    }
                ),
            )
        }
    )
    p = project(inputs).players[0]
    assert p.minutes == p.probability == p.expected["FGA"] == 0


@pytest.mark.parametrize("include_dnp", [False, True])
def test_conditional_sampling_counts_nonappearance_once_and_preserves_double_double(include_dnp):
    league, params, snapshot, priors, *_ = data = fixture()
    p = project(data).players[0]
    p = p.model_copy(update={"probability": 0.5, "rates": {**p.rates, "REB": 0.4, "AST": 0.4}})
    box = next(b for b in snapshot.boxes if b.player_id == p.player.id)
    played = box.model_copy(update={"stats": {**box.stats, "REB": 12.0, "AST": 12.0}})
    dnp = box.model_copy(update={"minutes": 0.0, "stats": dict.fromkeys(box.stats, 0.0)})
    history = (played, dnp) if include_dnp else (played,)
    game = next(g for g in snapshot.games if p.player.team_id in (g.home, g.away))
    sampled = sample_game(p, history, league, params, priors, 20000, game)
    axes = (*league.base_stats, *(d.id for d in league.derived))
    present = sampled[:, axes.index("FGA")] > 0
    assert np.mean(present) == pytest.approx(0.5, abs=0.01)
    assert np.mean(sampled[:, axes.index("FGA")]) == pytest.approx(5.0, abs=0.1)
    assert np.mean(sampled[present, axes.index("FGA")]) == pytest.approx(10.0)
    assert np.mean(sampled[:, axes.index("DD")]) == pytest.approx(0.5, abs=0.01)


def test_all_dnp_history_uses_frozen_prior_distribution_and_requires_its_rules():
    league, params, snapshot, priors, *_ = data = fixture()
    p = project(data).players[0].model_copy(update={"probability": 0.5})
    box = next(b for b in snapshot.boxes if b.player_id == p.player.id)
    dnp = box.model_copy(update={"minutes": 0.0, "stats": dict.fromkeys(box.stats, 0.0)})
    game = next(g for g in snapshot.games if p.player.team_id in (g.home, g.away))
    with pytest.raises(DataError, match="prior predictive requires frozen preseason"):
        sample_game(p, (dnp,), league, params, priors, 1000, game)
    config = load_config(
        *[Path(f"examples/2026-27/{name}.json") for name in ("league", "season", "model")]
    )
    frozen = priors.model_copy(update={"distribution": config.model.projection})
    actual = sample_game(p, (dnp,), league, params, frozen, 1000, game)
    expected = sample_game(p, (), league, params, frozen, 1000, game)
    assert np.array_equal(actual, expected)
    assert np.any(actual > 0)
