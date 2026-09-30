import json

import pytest
from test_auction_io import frozen_auction as frozen_auction
from test_calculation import projection_bundle as projection_bundle
from test_preparation import annual_case as annual_case

from fba.adapters.auction import auction_details, load_auction
from fba.adapters.auction_metadata import auction_teams, freeze_team_sources, label_sources
from fba.adapters.calculation import load_projection
from fba.adapters.espn import team_labels
from fba.contracts.auction import AnnotatedAuctionDetail, AuctionDetail
from fba.contracts.base import DataError
from fba.contracts.projection import CalculationResult
from fba.data.codec import canonical, digest


def test_labels_and_annotations_are_source_bound(frozen_auction):
    path, _, _ = frozen_auction
    original = path.read_bytes()
    inputs, _ = load_auction(path)
    assert inputs.teams[0].name == "City A Alphas"
    assert inputs.teams[0].abbreviation == "A"
    rows = {p.player_id: p for p in inputs.details}
    assert rows["0"].history_games == 0 and rows["1"].history_games == 3
    assert rows["1"].history_minimum == 2
    assert rows["1"].original_expected_games == 3
    assert rows["1"].expected_games == 1.5
    assert rows["1"].healthy_games_threshold == 3
    changes = (
        {"teams": (inputs.teams[0].model_copy(update={"name": "Invented"}), *inputs.teams[1:])},
        {
            "details": (
                inputs.details[0].model_copy(update={"history_games": 999}),
                *inputs.details[1:],
            )
        },
        {"artifacts": tuple(a for a in inputs.artifacts if not a.path.startswith("source/teams/"))},
    )
    for change in changes:
        path.write_bytes(canonical(inputs.model_copy(update=change)))
        with pytest.raises(DataError, match="auction.(teams|details)"):
            load_auction(path)
    path.write_bytes(original)
    raw = path.parent / next(a.path for a in inputs.artifacts if a.path.startswith("source/teams/"))
    raw.write_bytes(raw.read_bytes() + b" ")
    with pytest.raises(DataError, match="SHA-256"):
        load_auction(path)


def test_version_two_remains_readable_without_claiming_new_metadata(frozen_auction):
    path, _, _ = frozen_auction
    current = json.loads(path.read_bytes())
    current["format_version"] = 2
    current.pop("teams")
    current.pop("scenarios")
    for row in current["details"]:
        for key in AnnotatedAuctionDetail.model_fields.keys() - AuctionDetail.model_fields.keys():
            del row[key]
    path.write_text(json.dumps(current))
    legacy, sha = load_auction(path)
    assert sha == digest(path.read_bytes()) and legacy.teams is None
    assert all(type(row) is AuctionDetail for row in legacy.details)


def test_team_wire_missing_name_and_duplicate_ids_are_explicit():
    def raw(teams):
        return json.dumps({"settings": {"proTeams": teams}}).encode()

    team = {"id": 1, "abbrev": "ABC"}
    assert team_labels(raw([team]), "fixture")[0].name is None
    with pytest.raises(DataError, match="duplicate"):
        team_labels(raw([team, team]), "fixture")
    with pytest.raises(DataError, match="team labels changed"):
        team_labels(raw([{**team, "abbrev": ""}]), "fixture")


def test_history_annotation_excludes_zero_minute_rows(frozen_auction, annual_case):
    _, _, projection = frozen_auction
    _, _, _, snapshot = annual_case
    inputs, _ = load_projection(projection / "projection-input.json")
    result = CalculationResult.model_validate_json(
        next((projection / "results").glob("calculation-*.json")).read_bytes()
    )
    zero = snapshot.history[0].model_copy(
        update={
            "game_id": "zero",
            "stats": tuple(s.model_copy(update={"value": 0}) for s in snapshot.history[0].stats),
        }
    )
    changed = snapshot.model_copy(update={"history": (*snapshot.history, zero)})
    before = auction_details(snapshot, result, inputs.config, annotated=True)
    after = auction_details(changed, result, inputs.config, annotated=True)
    assert after == before


def test_team_metadata_rejects_missing_sources_and_player_labels(annual_case):
    root, _, config, snapshot = annual_case
    with pytest.raises(DataError, match="missing frozen"):
        label_sources(snapshot.model_copy(update={"artifacts": ()}), config)
    files = freeze_team_sources(snapshot, config, root)
    unknown = snapshot.players[0].model_copy(update={"team_id": "unknown"})
    changed = snapshot.model_copy(update={"players": (unknown, *snapshot.players[1:])})
    with pytest.raises(DataError, match="missing labels"):
        auction_teams(changed, config, files)
