from datetime import timedelta
from math import nextafter
from pathlib import Path

import pytest
from inseason_support import DEFAULTS, fixture
from test_inseason_projection import project

from fba.adapters.config import load_config
from fba.adapters.inseason_priors import direct_prior, read_priors
from fba.contracts.base import DataError
from fba.contracts.data import Forecast, StatValue
from fba.contracts.inseason import InseasonForecast
from fba.contracts.yahoo import YahooCatalog
from fba.data.codec import canonical, digest
from fba.data.inseason_sources import projection_rules
from fba.inseason.sampling import sample_game
from fba.inseason.team_view import player_categories, team_views


def provider_forecast():
    # Captured ESPN 2026-27 Ryan Rollins totals, not our adjusted research.
    values = {
        "MIN": 1932.0,
        "FGM": 391.0,
        "FGA": 828.0,
        "FTM": 99.0,
        "FTA": 124.0,
        "3PM": 148.0,
        "PTS": 1029.0,
        "REB": 276.0,
        "AST": 331.0,
        "TO": 159.0,
        "STL": 90.0,
        "BLK": 28.0,
    }
    return Forecast(
        player_id="4591725",
        expected_games=69.0,
        source_id="ESPN 2026-27 season projection",
        totals=tuple(
            StatValue(id=key, value=value, missing_reason=None) for key, value in values.items()
        ),
    )


def test_preseason_preserves_source_means_and_total_contribution():
    inputs = list(fixture())
    league, params, source, priors, _, _, _ = inputs
    forecast = provider_forecast()
    direct = direct_prior(forecast, "p0", league, 82)
    inputs[2] = source.model_copy(update={"boxes": ()})
    inputs[3] = priors.model_copy(update={"players": (direct, *priors.players[1:])})
    projection = project(inputs)
    player = projection.players[0]
    assert player.minutes == 28.0
    assert player.rates["FGM"] * player.minutes == pytest.approx(391 / 69)
    assert player.probability == pytest.approx(69 / 82)
    assert player.expected["AST"] == pytest.approx(331 / 82)
    # The provider totals / projected games are the displayed per-appearance means.
    means = player_categories(projection, league, params, inputs[3], inputs[2], source.games)
    assert means["p0"]["PTS"] == pytest.approx(1029 / 69)
    assert means["p0"]["AST"] == pytest.approx(331 / 69)
    assert player.expected["PTS"] == pytest.approx(1029 / 82)
    assert player.prior.source == forecast.source_id


def test_inseason_observations_update_rates_minutes_and_shooting_separately():
    inputs = list(fixture())
    league, params, source, priors, _, _, _ = inputs
    direct = direct_prior(provider_forecast(), "p0", league, 82)
    box = source.boxes[0].model_copy(
        update={
            "minutes": 40.0,
            "stats": {**source.boxes[0].stats, "AST": 12.0, "FGM": 9.0, "FGA": 12.0},
        }
    )
    inputs[2] = source.model_copy(update={"boxes": (box,)})
    inputs[3] = priors.model_copy(update={"players": (direct, *priors.players[1:])})
    player = project(inputs).players[0]
    k = params.rate_k["AST"].value
    assert player.rates["AST"] == pytest.approx((k * (331 / 1932) + 12) / (k + 40))
    assert player.minutes == pytest.approx(
        (params.minute_k.value * 28 + 40) / (params.minute_k.value + 1)
    )
    shot_k = params.shot_k["FG%"].value
    assert player.probabilities["FG%"] == pytest.approx((shot_k * (391 / 828) + 9) / (shot_k + 12))
    assert player.weights["AST"] > 0


def test_missing_source_statistic_requires_explicit_fallback_and_preserves_known_values():
    league = fixture()[0]
    forecast = provider_forecast()
    without = forecast.model_copy(
        update={"totals": tuple(s for s in forecast.totals if s.id != "BLK")}
    )
    with pytest.raises(DataError, match="unavailable forecast statistics"):
        direct_prior(without, "p0", league, 82)
    prior = direct_prior(without, "p0", league, 82, {"BLK": 20.0, "AST": 9999.0})
    assert prior.fallback_statistics == ("BLK",)
    assert prior.rates["BLK"] == pytest.approx(20 / 1932)
    assert prior.rates["AST"] == pytest.approx(331 / 1932)


def test_missing_scoring_component_is_recovered_from_exact_published_totals():
    forecast = provider_forecast()
    without = forecast.model_copy(
        update={"totals": tuple(s for s in forecast.totals if s.id != "3PM")}
    )
    prior = direct_prior(without, "p0", fixture()[0], 82)
    assert prior.rates["3PM"] == pytest.approx(148 / 1932)
    assert prior.fallback_statistics == ()
    inconsistent = without.model_copy(
        update={
            "totals": tuple(
                s.model_copy(update={"value": 1.0}) if s.id == "PTS" else s for s in without.totals
            )
        }
    )
    with pytest.raises(DataError, match="negative derived scoring component"):
        direct_prior(inconsistent, "p0", fixture()[0], 82)


def test_direct_document_uses_existing_source_hash_time_and_rule_validation(tmp_path):
    config = load_config(
        *[Path(f"examples/2026-27/{name}.json") for name in ("league", "season", "model")]
    )
    league, _, _, priors, _, _, now = fixture()
    league = league.model_copy(update={"season_id": config.season.season_id})
    direct = direct_prior(provider_forecast(), "p0", league, 82)
    priors = priors.model_copy(
        update={"players": (direct,), "season_id": league.season_id, "known_at": now}
    )
    document = InseasonForecast(format_version=1, config=config, priors=priors)
    path = tmp_path / "forecast.json"
    path.write_bytes(canonical(document))
    sha = digest(path.read_bytes())
    loaded = read_priors(path, sha, league, now)
    assert loaded.players == priors.players
    assert loaded.source_sha256 == sha
    with pytest.raises(DataError, match="publication time differs"):
        read_priors(path, sha, league, now + timedelta(seconds=1))
    with pytest.raises(DataError, match="SHA-256 differs"):
        read_priors(path, "0" * 64, league, now)
    catalog = YahooCatalog.model_validate_json((DEFAULTS / "yahoo-catalog.json").read_bytes())
    assert projection_rules(path, catalog).season_id == league.season_id


@pytest.mark.parametrize("probability", (0.1, None))
def test_direct_document_rejects_inconsistent_or_missing_shooting_probability(
    tmp_path, probability
):
    config = load_config(
        *[Path(f"examples/2026-27/{name}.json") for name in ("league", "season", "model")]
    )
    league, _, _, priors, _, _, now = fixture()
    league = league.model_copy(update={"season_id": config.season.season_id})
    direct = direct_prior(provider_forecast(), "p0", league, 82)
    probabilities = dict(direct.probabilities)
    if probability is None:
        probabilities.pop("FG%")
    else:
        probabilities["FG%"] = probability
    direct = direct.model_copy(update={"probabilities": probabilities})
    priors = priors.model_copy(
        update={"players": (direct,), "season_id": league.season_id, "known_at": now}
    )
    path = tmp_path / "forecast.json"
    path.write_bytes(canonical(InseasonForecast(format_version=1, config=config, priors=priors)))
    with pytest.raises(DataError, match=r"FG%.*missing or inconsistent"):
        read_priors(path, digest(path.read_bytes()), league, now)


def test_team_minute_overage_is_visible_without_revising_provider_forecasts():
    from test_inseason_rotation import rotation_case

    inputs = rotation_case([35] * 8)
    league, params, source, _, ledger, _, _ = inputs
    projection = project(inputs)
    rows = team_views(projection, league, params, source.games, league, ledger)
    row = rows[0]
    assert row.minutes.result == 280.0
    assert row.budget.result == 240.0
    assert row.difference.result == 40.0
    assert all(p.probability == 1.0 for p in projection.players)


def test_normalized_equal_nested_counts_tolerate_roundoff_but_reject_invalid_forecasts():
    league, params, source, priors, _, _, _ = fixture()
    config = load_config(
        *[Path(f"examples/2026-27/{name}.json") for name in ("league", "season", "model")]
    )
    priors = priors.model_copy(update={"distribution": config.model.projection})
    player = project(fixture()).players[0]
    rates = {**player.rates, "FGA": 1.0, "FGM": 0.5, "3PM": nextafter(0.5, 1.0)}
    player = player.model_copy(update={"rates": rates})
    draws = sample_game(player, (), league, params, priors, 100, source.games[0])
    assert (
        draws[:, league.base_stats.index("3PM")] <= draws[:, league.base_stats.index("FGM")]
    ).all()
    invalid = player.model_copy(update={"rates": {**rates, "3PM": 1.0}})
    with pytest.raises(DataError, match="mean exceeds parent FGM"):
        sample_game(invalid, (), league, params, priors, 100, source.games[0])
