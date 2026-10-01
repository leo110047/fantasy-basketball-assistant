from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fba.adapters.espn import EspnStats, decode_players, stat_values
from fba.adapters.roster import import_roster
from fba.contracts.base import DataError, IdentityError
from fba.contracts.data import (
    CalibrationPair,
    Game,
    IdentityMap,
    ProviderPlayer,
    RosterRow,
    ScheduleCount,
)
from fba.core.data import resolve_players, validate_schedule
from fba.formulas.fitting import fit_availability


@given(st.permutations(["a", "b", "c"]))
def test_calibration_has_independent_known_answer_and_stable_order(order):
    inputs = {"a": (10.0, 7), "b": (20.0, 12), "c": (30.0, 17)}
    pairs = tuple(
        CalibrationPair(player_id=k, projected_games=inputs[k][0], actual_games=inputs[k][1])
        for k in order
    )
    result = fit_availability(pairs, "test-season", ("0" * 64,), "ordinary_least_squares")
    assert (result.intercept, result.slope, result.sample_size) == (2.0, 0.5, 3)


def test_calibration_cannot_invent_fit_for_constant_x():
    pairs = tuple(
        CalibrationPair(player_id=str(i), projected_games=10.0, actual_games=i) for i in range(3)
    )
    with pytest.raises(DataError, match="variance"):
        fit_availability(pairs, "year", ("0" * 64,), "ordinary_least_squares")


def test_calibration_keeps_exact_integer_observations_before_float_output():
    pairs = tuple(
        CalibrationPair(player_id=str(i), projected_games=float(i), actual_games=2**53 - 1 + 2 * i)
        for i in (1, 2, 3)
    )
    result = fit_availability(pairs, "year", ("0" * 64,), "ordinary_least_squares")
    assert (result.intercept, result.slope) == (float(2**53 - 1), 2.0)


def test_matching_never_guesses_even_unique_equal_name():
    row = RosterRow(
        id="yahoo-x",
        name="Same Name",
        positions=("P",),
        rank=1,
        projected_price=None,
        average_price=None,
    )
    provider = ProviderPlayer(provider="espn", id="espn-x", name="Same Name", team_id="t")
    with pytest.raises(IdentityError) as error:
        resolve_players(
            (row,), IdentityMap(format_version=1, entries=()), (provider,), frozenset(), frozenset()
        )
    assert error.value.unresolved == ("yahoo-x",)


def test_missing_market_quotes_stay_missing(parsed):
    data = b"player_id,name,positions,rank,projected_price,average_price\na,Name,PG,1,,-\n"
    result = import_roster(data, parsed[1].roster_import, parsed[0].positions)
    assert result[0].projected_price is None and result[0].average_price is None


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-1"])
def test_nonfinite_negative_market_quotes_rejected(parsed, price):
    data = (
        f"player_id,name,positions,rank,projected_price,average_price\na,Name,PG,1,{price},1\n"
    ).encode()
    with pytest.raises(DataError):
        import_roster(data, parsed[1].roster_import, parsed[0].positions)


def box():
    return {
        "13": 4.0,
        "14": 8.0,
        "15": 2.0,
        "16": 3.0,
        "0": 11.0,
        "17": 1.0,
        "6": 4.0,
        "4": 1.0,
        "3": 2.0,
        "11": 1.0,
        "2": 1.0,
        "1": 0.0,
        "40": 20.0,
    }


@pytest.mark.parametrize(
    "key,value", [("13", 9.0), ("15", 4.0), ("17", 5.0), ("4", 5.0), ("0", 12.0)]
)
def test_inconsistent_basketball_accounting_rejected(key, value):
    stats = box() | {key: value}
    with pytest.raises(DataError):
        stat_values(stats, "test-player", partial=True)


def test_partial_projection_marks_missing_instead_of_zero():
    stats = box()
    del stats["4"]
    result = {s.id: s for s in stat_values(stats, "player", partial=True)}
    assert result["OREB"].value is None
    assert result["OREB"].missing_reason
    with pytest.raises(DataError, match="missing played-game"):
        stat_values(stats, "player", partial=False)


def test_espn_infinite_unused_ratios_do_not_contaminate_count_stats():
    record = EspnStats(
        seasonId=2025,
        statSourceId=0,
        statSplitTypeId=5,
        stats=box() | {"35": "Infinity"},
        externalId="g",
        proTeamId=1,
    )
    assert len(stat_values(record.stats, "g", partial=False)) == 13
    with pytest.raises(ValueError, match="ratio fields"):
        EspnStats(
            seasonId=2025,
            statSourceId=0,
            statSplitTypeId=5,
            stats=box() | {"13": "Infinity"},
            externalId="g",
            proTeamId=1,
        )


def test_archived_league_response_preserves_explicit_player_records():
    import json

    player = {"id": 42, "fullName": "Player", "proTeamId": 1, "stats": []}
    direct = decode_players(json.dumps([player]).encode(), "direct")
    archived = decode_players(json.dumps({"players": [{"player": player}]}).encode(), "archive")
    assert direct == archived
    with pytest.raises(DataError, match="duplicate"):
        decode_players(json.dumps({"players": [{"player": player}] * 2}).encode(), "archive")


def game(gid="one", day=date(2025, 10, 20)):
    return Game(
        id=gid,
        home_team_id="a",
        away_team_id="b",
        tipoff=datetime(2025, 10, 20, 23, tzinfo=UTC),
        local_date=day,
        source_id="schedule",
        status="scheduled",
    )


def counts(announced=1):
    return tuple(
        ScheduleCount(
            team_id=t,
            announced=announced,
            pending=2,
            pending_reason="Unassigned Cup games",
            source_id="official",
        )
        for t in ("a", "b")
    )


def test_pending_games_not_invented():
    validate_schedule((game(),), counts())


def test_duplicate_team_day_rejected():
    with pytest.raises(DataError, match="duplicate team date"):
        validate_schedule((game(), game("two")), counts(2))


def test_official_count_is_independent_of_schedule():
    with pytest.raises(DataError, match="count mismatch"):
        validate_schedule((game(),), counts(2))
