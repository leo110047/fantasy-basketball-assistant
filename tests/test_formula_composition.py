import numpy as np
import pytest

from fba.contracts.config import Term
from fba.contracts.inseason import DerivedStat
from fba.formulas.categories import derive_games
from fba.formulas.registry import evaluate
from fba.formulas.simulation import array_product, nonnegative_samples
from fba.formulas.vector import matrix_product


def test_derived_linear_preserves_original_ordered_numpy_cancellation_and_shape():
    definition = DerivedStat(
        id="derived",
        kind="linear",
        terms=tuple(Term(stat_id=s, coefficient=1) for s in ("a", "b", "c")),
        threshold=0,
        minimum_hits=1,
    )
    source = np.array([[[1e16, 1.0, -1e16], [4.0, -3.0, 2.0]]])
    actual = derive_games(source, ("a", "b", "c"), (definition,))
    assert actual.shape == (1, 2, 4)
    # NumPy's original ordered reduction is 0 here; fsum would change it to 1.
    np.testing.assert_array_equal(actual[..., -1], [[0.0, 3.0]])
    np.testing.assert_array_equal(actual[..., :3], source)


def test_health_mask_products_and_matrix_projection_use_float_outputs():
    health = np.array([[True, False], [False, True]])
    values = np.array([[2.0, 3.0], [5.0, 7.0]])
    np.testing.assert_array_equal(matrix_product({"left": health, "right": values}), values)
    masked = array_product({"values": values, "multiplier": health[:, :1]})
    np.testing.assert_array_equal(masked, [[2.0, 3.0], [0.0, 0.0]])
    assert masked.dtype == np.float64
    clipped = nonnegative_samples({"values": np.array([-1.0, -0.0, 3.0])})
    np.testing.assert_array_equal(clipped, [0.0, 0.0, 3.0])
    assert not np.signbit(clipped[1])


@pytest.mark.parametrize(
    ("eligible", "observed", "expected"), ((82, 10, 20), (82, 80, 70), (15, 40, 15), (0, 30, 0))
)
def test_historical_games_respects_both_observation_and_eligible_schedule(
    eligible, observed, expected
):
    assert (
        evaluate(
            "historical_games", eligible=eligible, observed=observed, lower=20, upper=70
        ).result
        == expected
    )
