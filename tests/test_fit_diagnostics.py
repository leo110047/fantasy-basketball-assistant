import numpy as np
import pytest
from test_auction import config
from test_fit import fitted_case as fitted_case

from fba.auction.auction import calculate_auction
from fba.auction.fit import FittedUtility
from fba.contracts.auction import Infeasible, Plan
from fba.formulas.auction_fit import category_diagnostics, margin_score


def test_category_explanation_uses_configured_axes_steps_and_strict_lead_shares():
    parameters = config().model.fit
    margins = np.array([[2.0, 0.0, -2.0], [2.0, 0.0, -2.0]])
    rows = category_diagnostics(margins, ("high", "pivot", "low"), parameters)
    assert [row.lead_share for row in rows] == [1.0, 0.0, 0.0]
    assert [row.marginal_weight for row in rows] == [0.0, 3.0, 0.0]
    assert np.all(margin_score(margins, parameters.bandwidth) == 0.5)
    symmetric = np.array([[0.0, 0.0], [0.0, 0.0]])
    rows = category_diagnostics(symmetric, ("one", "two"), parameters)
    assert [row.marginal_weight for row in rows] == [1.0, 1.0]
    rows = category_diagnostics(np.full((2, 3), 1e6), ("a", "b", "c"), parameters)
    assert all(row.marginal_weight == 0 for row in rows)


@pytest.mark.parametrize("accept", [True, False])
def test_diagnostics_describe_selected_roster_not_last_rejected_proposal(
    fitted_case, monkeypatch, accept
):
    inputs, draft, kernel = fitted_case
    # Last proposal is rejected. With accept=True the middle proposal is retained.
    scores = iter((0.2, 0.8 if accept else 0.1, 0.1))
    blocks = iter((np.array([0.2, 0.2]), np.array([0.8, 0.8]), np.array([0.1, 0.1])))
    observed = []
    difference = FittedUtility.difference

    def capture(self, mean, noise):
        result = difference(self, mean, noise)
        observed.append((result.copy(), self.opponent_rosters))
        return result

    monkeypatch.setattr(
        FittedUtility, "gradient", lambda self, mean, noise: np.ones(self.manager.k)
    )
    monkeypatch.setattr(FittedUtility, "score", lambda *args: next(scores))
    monkeypatch.setattr(FittedUtility, "block_scores", lambda *args: next(blocks))
    monkeypatch.setattr(FittedUtility, "difference", capture)
    result = calculate_auction(inputs, draft, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    assert isinstance(result.plan, Plan) and result.fit.diagnostics is not None
    detail = result.fit.diagnostics
    assert detail.baseline_score == 0.2
    assert detail.selected_score == (0.8 if accept else 0.2)
    assert result.fit.selected_step == (0.25 if accept else 0.0)
    assert detail.compared_roster == result.plan.players
    assert not set(detail.compared_roster).intersection(p for r in detail.opponents for p in r)
    assert len(observed) == (2 if accept else 1)
    expected = category_diagnostics(
        observed[-1][0],
        tuple(c.id for c in inputs.config.league.categories),
        inputs.config.model.fit,
    )
    assert detail.categories == expected


def test_equal_mode_and_infeasible_fit_do_not_claim_category_diagnostics(fitted_case):
    inputs, draft, kernel = fitted_case
    equal = calculate_auction(inputs, draft, "0" * 64, "3" * 64)
    assert equal.fit is None
    empty = inputs.model_copy(
        update={
            "players": tuple(p.model_copy(update={"projected_price": None}) for p in inputs.players)
        }
    )
    result = calculate_auction(empty, draft, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    assert isinstance(result.plan, Infeasible) and result.fit is None
