import json

import pytest

from fba.adapters import espn
from fba.contracts.base import DataError


def page(year=2027):
    roster = {
        "metadata": {"year": year},
        "team": {"id": "1"},
        "athletes": [{"id": "42", "name": "Player B"}, {"id": "12", "name": "Player A"}],
    }
    payload = json.dumps({"page": {"content": {"roster": roster}}})
    return ("<html><script>window['__espnfitt__']=" + payload + ";</script></html>").encode()


def test_roster_page_uses_explicit_ids_and_declared_season():
    players = espn.roster_page(page(), 2027, "roster-page")
    assert [(p.id, p.name, p.team_id) for p in players] == [
        ("12", "Player A", "1"),
        ("42", "Player B", "1"),
    ]
    with pytest.raises(DataError, match="season mismatch"):
        espn.roster_page(page(2026), 2027, "roster-page")


@pytest.mark.parametrize(
    "data",
    [
        b"<html>Access denied</html>",
        page() + page(),
        page().replace(b'"year": 2027', b'"year": true'),
    ],
)
def test_roster_page_rejects_unavailable_ambiguous_or_invalid_payload(data):
    with pytest.raises(DataError):
        espn.roster_page(data, 2027, "roster-page")


def test_roster_page_rejects_duplicate_json_keys():
    data = page().replace(b'"year": 2027', b'"year": 2026, "year": 2027')
    with pytest.raises(DataError, match="duplicate key"):
        espn.roster_page(data, 2027, "roster-page")
