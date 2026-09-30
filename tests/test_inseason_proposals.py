import pytest
from inseason_support import fixture

from fba.contracts.base import DataError
from fba.contracts.inseason import Proposal
from fba.core.proposals import latest_proposals
from fba.formulas.fitting import fit_acceptance


def test_proposal_versions_are_counted_once_and_stale_updates_are_rejected():
    now = fixture()[-1]
    original = Proposal(
        id="first",
        created_at=now,
        opponent="team1",
        send=("p0",),
        receive=("p3",),
        rank_delta=1,
        need_delta=0.1,
        probability=0.6,
        outcome="pending",
        supersedes=None,
    )
    accepted = original.model_copy(
        update={"id": "accepted", "outcome": "accepted", "supersedes": "first"}
    )
    stale = original.model_copy(
        update={"id": "stale", "outcome": "rejected", "supersedes": "first"}
    )
    assert latest_proposals((original, accepted)) == (accepted,)
    with pytest.raises(DataError, match="already superseded"):
        latest_proposals((original, accepted, stale))
    with pytest.raises(DataError, match="already superseded"):
        fit_acceptance((original, accepted, stale), 1, 1e-9)
    changed_trade = accepted.model_copy(update={"send": ("p1",)})
    with pytest.raises(DataError, match="original trade"):
        latest_proposals((original, changed_trade))
