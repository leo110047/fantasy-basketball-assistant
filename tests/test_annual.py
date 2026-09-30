import json
import re

import pytest
from test_calculation import projection_bundle as projection_bundle
from test_preparation import annual_case as annual_case

from fba.adapters.acquisition import Acquired
from fba.adapters.annual import previous_evaluation, provider_predictions, validate_archive
from fba.adapters.config import load_config
from fba.adapters.preparation import project
from fba.adapters.snapshots import artifact, load_snapshot, publish
from fba.apps.annual import finish_annual
from fba.contracts.archive import ForecastArchive
from fba.contracts.base import ConfigError, DataError
from fba.contracts.data import Identity, Provenance
from fba.data.codec import canonical, decode, digest


def previous_year(value):
    if isinstance(value, str):
        if re.fullmatch(r"202[5-7]-[0-9]{2}", value):
            year = int(value[:4]) - 1
            return f"{year}-{(year + 1) % 100:02}"
        return re.sub(r"202[5-7]", lambda m: str(int(m[0]) - 1), value)
    if isinstance(value, list):
        return [previous_year(v) for v in value]
    if isinstance(value, dict):
        return {k: previous_year(v) for k, v in value.items()}
    return value


@pytest.fixture
def evaluated_snapshot(annual_case, tmp_path):
    root, model, config, snapshot = annual_case
    valued = project(root, model, tmp_path / "valued", None)
    archive = decode(ForecastArchive, (valued / "forecast.json").read_bytes(), "test")
    prior = tmp_path / "prior"
    prior.mkdir()
    for name in ("league", "season", "model"):
        (prior / f"{name}.json").write_text(
            json.dumps(previous_year(getattr(config, name).model_dump(mode="json")))
        )
    old_config = load_config(*(prior / f"{n}.json" for n in ("league", "season", "model")))
    archive = archive.model_copy(
        update={
            "config": old_config,
            "calculation": archive.calculation.model_copy(update={"config": old_config.refs}),
            "identities": tuple(
                Identity(
                    player_id=p.id,
                    provider="espn",
                    provider_player_id=str(1000 + int(p.id)),
                    method="explicit_mapping",
                    source="synthetic fixture",
                    confirmed_at=old_config.season.snapshot_as_of,
                )
                for p in archive.calculation.projections
            ),
        }
    )
    payload = canonical(archive)
    wire = []
    # Independent observed season, deliberately unlike the frozen forecast.
    for i in range(12):
        stats = {
            "13": 2.0 + i / 10,
            "14": 15.0,
            "15": 1.0,
            "16": 2.0,
            "0": 6.0 + i / 5,
            "17": 1.0,
            "6": 3.0,
            "4": 1.0,
            "3": 2.0,
            "11": 1.0,
            "2": 0.5,
            "1": 0.5,
            "40": 15.0,
        }
        rows = [
            dict(
                seasonId=2026,
                statSourceId=0,
                statSplitTypeId=5,
                externalId=str(g),
                proTeamId=1,
                stats=stats,
            )
            for g in range(3)
        ]
        rows.append(
            dict(
                seasonId=2026,
                statSourceId=0,
                statSplitTypeId=0,
                externalId="season",
                proTeamId=1,
                stats={**{k: v * 3 for k, v in stats.items()}, "42": 3.0},
            )
        )
        wire.append(dict(id=1000 + i, fullName=f"Observed {i}", proTeamId=1, stats=rows))
    actual = json.dumps(wire).encode()
    files = {a.path: (root / a.path).read_bytes() for a in snapshot.artifacts}
    year = config.season.model_dump(mode="json")
    year["sources"].append(
        dict(
            id="prior",
            role="forecast_archive",
            adapter="fba_forecast",
            url="file:///prior.json",
            season_id=old_config.season.season_id,
            season_code="2026",
            available_as_of=year["snapshot_as_of"],
            delivery="manual",
            manual_file="prior.json",
            manual_capture=dict(sha256=digest(payload), retrieved_at=year["snapshot_as_of"]),
        )
    )
    current = tmp_path / "current"
    current.mkdir()
    for name in ("league", "model"):
        (current / f"{name}.json").write_bytes(canonical(getattr(config, name)))
    (current / "season.json").write_text(json.dumps(year))
    config = load_config(*(current / f"{n}.json" for n in ("league", "season", "model")))
    files.update(
        {
            f"config/{n}.json": (current / f"{n}.json").read_bytes()
            for n in ("league", "season", "model")
        }
    )
    files["config/effective.json"] = canonical(config)
    files["raw/prior.json"] = payload
    history_id = next(s.id for s in config.season.sources if s.role == "game_logs")
    files[f"raw/{history_id}.json"] = actual
    entries = []
    for path, data in sorted(files.items()):
        source = next((s for s in config.season.sources if path == f"raw/{s.id}.json"), None)
        acquired = (
            None
            if source is None
            else Acquired(
                source=source,
                data=data,
                provenance=Provenance(
                    source_id=source.id,
                    url=source.url,
                    raw_sha256=digest(data),
                    available_as_of=config.season.snapshot_as_of,
                    retrieved_at=config.season.snapshot_as_of,
                    delivery="manual",
                ),
            )
        )
        entries.append(artifact(path, data, acquired))
    snapshot = snapshot.model_copy(
        update={
            "config": config.refs,
            "artifacts": tuple(entries),
            "players": tuple(
                p.model_copy(
                    update={
                        "roster": p.roster.model_copy(
                            update={"projected_price": 12.0 + int(p.roster.id)}
                        )
                    }
                )
                for p in snapshot.players
            ),
        }
    )
    root = publish(snapshot, files, tmp_path / "current-snapshot")
    return root, current / "model.json", config, archive


def test_annual_evaluation_is_frozen_deterministic_and_publishes_atomically(
    evaluated_snapshot, tmp_path, monkeypatch
):
    root, model, config, archive = evaluated_snapshot
    result = json.loads(previous_evaluation(root, config))
    metrics = result["evaluation"]["variants"][0]
    assert metrics["predicted_players"] == 12
    assert metrics["top_draft_hits"] == 4
    assert metrics["rank_correlation"] == 1.0
    assert metrics["games_mae"] == 1.5
    assert result["information_mode"] == "published"
    one = finish_annual(root, model, tmp_path / "one", None)
    two = finish_annual(root, model, tmp_path / "two", None)
    assert (one / "previous-evaluation.json").read_bytes() == (
        two / "previous-evaluation.json"
    ).read_bytes()
    assert (one / "forecast.json").is_file()
    assert len(list((one / "auction").glob("auction-input-*"))) == 1
    opening = json.loads(next((one / "opening").glob("equal-*.json")).read_bytes())
    assert opening["plan"]["players"]
    assert (one / "opening-draft.json").is_file()
    with pytest.raises(DataError, match="already exists"):
        finish_annual(root, model, tmp_path / "one", None)

    def broken(*args):
        raise DataError("injected auction failure")

    monkeypatch.setattr("fba.apps.annual.prepare_auction", broken)
    with pytest.raises(DataError, match="injected auction"):
        finish_annual(root, model, tmp_path / "failed", None)
    assert list((tmp_path / "failed").iterdir()) == []
    assert provider_predictions(archive)[0].id == "1000"


def test_annual_missing_archive_source_and_tampered_archive_fail(annual_case, evaluated_snapshot):
    root, _, config, _ = annual_case
    with pytest.raises(DataError, match="exactly one forecast_archive"):
        previous_evaluation(root, config)
    root, _, config, archive = evaluated_snapshot
    invalid_zone = archive.model_copy(
        update={
            "config": archive.config.model_copy(
                update={
                    "league": archive.config.league.model_copy(update={"timezone": "unknown-zone"})
                }
            )
        }
    )
    with pytest.raises(ConfigError, match="league.timezone"):
        validate_archive(invalid_zone, config)
    wrong = archive.model_copy(
        update={
            "config": archive.config.model_copy(
                update={
                    "refs": archive.config.refs.model_copy(
                        update={
                            "season": archive.config.refs.season.model_copy(
                                update={"effective_sha256": "0" * 64}
                            )
                        }
                    )
                }
            )
        }
    )
    with pytest.raises(DataError, match="effective hash mismatch"):
        validate_archive(wrong, config)
    with pytest.raises(DataError, match="missing explicit provider"):
        provider_predictions(archive.model_copy(update={"identities": ()}))
    with pytest.raises(DataError, match="ambiguous provider"):
        provider_predictions(
            archive.model_copy(update={"identities": (*archive.identities, archive.identities[0])})
        )
    (root / "raw/prior.json").write_bytes(b"{}")
    with pytest.raises(DataError, match="SHA-256 or size mismatch"):
        previous_evaluation(root, config)


def test_observed_game_coverage_and_derived_thresholds(evaluated_snapshot):
    from fba.adapters.espn import decode_players, game_logs, season_totals
    from fba.formulas.actual import observed_players

    root, _, config, archive = evaluated_snapshot
    source = next(s for s in config.season.sources if s.role == "game_logs")
    wire = decode_players((root / f"raw/{source.id}.json").read_bytes(), source.id)
    logs, totals = game_logs(wire, 2026, source.id), season_totals(wire, 2026, source.id)
    actual = observed_players(logs, totals, archive.config)
    assert actual[0].stats[-1] == 0.0
    # The missing game's remaining totals cannot make a double-double: exact, not imputed.
    assert observed_players(logs[:-1], totals, archive.config) == actual
    uncertain = totals[-1].model_copy(
        update={
            "totals": tuple(
                s.model_copy(update={"value": s.value + 10}) if s.id in ("REB", "AST") else s
                for s in totals[-1].totals
            )
        }
    )
    with pytest.raises(DataError, match="threshold count undetermined"):
        observed_players(logs[:-1], (*totals[:-1], uncertain), archive.config)
    zero = totals[0].model_copy(
        update={
            "games": 1,
            "totals": tuple(
                s.model_copy(update={"value": 3.0 if s.id == "MIN" else 0.0})
                for s in totals[0].totals
            ),
        }
    )
    bounded = observed_players((), (zero,), archive.config)[0]
    assert bounded.expected_games == 1 and bounded.minutes == 3 and not any(bounded.stats)
    with pytest.raises(DataError, match="duplicate observation"):
        observed_players((*logs, logs[0]), totals, archive.config)
    with pytest.raises(DataError, match="missing observed statistic"):
        observed_players(
            (logs[0].model_copy(update={"stats": ()}), *logs[1:]), totals, archive.config
        )


def test_annual_infeasible_opening_does_not_publish(evaluated_snapshot, tmp_path):
    root, model, _, _ = evaluated_snapshot
    snapshot = load_snapshot(root)
    files = {a.path: (root / a.path).read_bytes() for a in snapshot.artifacts}
    missing = snapshot.model_copy(
        update={
            "players": tuple(
                p.model_copy(
                    update={"roster": p.roster.model_copy(update={"projected_price": None})}
                )
                for p in snapshot.players
            )
        }
    )
    source = publish(missing, files, tmp_path / "missing-quotes")
    with pytest.raises(DataError, match="annual.opening:"):
        finish_annual(source, model, tmp_path / "not-published", None)
    assert not list((tmp_path / "not-published").iterdir())
