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


def resolved_proposals(features, outcomes):
    now = fixture()[-1]
    return tuple(
        Proposal(
            id=str(i),
            created_at=now,
            opponent="team1",
            send=("p0",),
            receive=("p3",),
            rank_delta=float(x[0]),
            need_delta=float(x[1]),
            probability=0.5,
            outcome="accepted" if y else "rejected",
            supersedes=None,
        )
        for i, (x, y) in enumerate(zip(features, outcomes, strict=True))
    )


def test_acceptance_fit_recovers_known_nonseparable_probabilities():
    import numpy as np

    # Every feature pattern contains both outcomes. The 1:3 vs 3:1 odds
    # yield beta_rank=log(3)/100, beta_need=0, threshold=0 exactly.
    features, outcomes = [], []
    for rank in (-100, 100):
        for need in (-1, 1):
            features.extend([(rank, need)] * 4)
            outcomes.extend([True] * (1 if rank < 0 else 3) + [False] * (3 if rank < 0 else 1))
    a, b, threshold, _, predicted, _ = fit_acceptance(
        resolved_proposals(features, outcomes), 10, 1e-9
    )
    assert a == pytest.approx(np.log(3) / 100, abs=1e-8)
    assert b == pytest.approx(0, abs=1e-8)
    assert threshold == pytest.approx(0, abs=1e-8)
    assert predicted == pytest.approx([0.25] * 8 + [0.75] * 8, abs=1e-8)


@pytest.mark.parametrize("quasi", [False, True])
def test_separated_acceptance_data_cannot_be_marked_fitted(quasi):
    features = [(-2, -1), (-1, 1), (1, -1), (2, 1)]
    outcomes = [False, False, True, True]
    if quasi:
        features += [(0, 0), (0, 0)]
        outcomes += [False, True]
    with pytest.raises(DataError, match="separated outcomes"):
        fit_acceptance(resolved_proposals(features, outcomes), 4, 1e-9)


@pytest.mark.parametrize("seed", range(40))
def test_acceptance_fit_meets_gradient_tolerance_on_overlapping_samples(seed):
    import numpy as np
    from scipy.special import expit

    rng = np.random.default_rng(seed)
    features = rng.normal(size=(200, 2)) * [20, 0.1]
    outcomes = rng.random(200) < expit(features @ np.array([0.05, 2.0]) - 0.2)
    a, b, threshold, _, predicted, _ = fit_acceptance(
        resolved_proposals(features, outcomes), 20, 1e-9
    )
    x = np.column_stack((features, -np.ones(200)))
    x /= np.maximum(np.max(np.abs(x), axis=0), 1)
    residual = x.T @ (np.array(predicted) - outcomes) / len(outcomes)
    assert np.max(np.abs(residual)) <= 1e-9
    assert np.isfinite([a, b, threshold]).all()
