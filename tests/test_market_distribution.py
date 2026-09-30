import itertools
import json
from math import erf, exp, floor, log, prod, sqrt

import numpy as np
import pytest
from test_auction import config, inputs_for, player, state

from fba.auction.auction import market_context
from fba.contracts.auction import TeamBudget
from fba.contracts.base import ConfigError
from fba.contracts.config import MarketParameters, ModelDocument, SampledMarketParameters
from fba.formulas.market import distribution_price, normalization_shift


@pytest.mark.parametrize("volatility", [0.0, 0.25, 1.0])
def test_normalization_matches_two_bidder_closed_form(volatility):
    expected = 2 * exp(volatility**2 / 2) * (1 + erf(-volatility / 2)) / 2
    assert exp(normalization_shift(2, volatility)) == pytest.approx(expected, rel=1e-10)


def test_unresolvable_volatility_fails_instead_of_publishing_a_price():
    with pytest.raises(ConfigError, match="market.volatility"):
        normalization_shift(14, 1000.0)


def bid_mass(maximum, premium, sigma, shift, minimum, increment):
    # Independent scalar normal CDF and explicit discrete PMF for exhaustive
    # joint-bid enumeration; no production CDF or tail-sum helper is used.
    def cdf(bid):
        if bid < minimum:
            return 0.0
        if bid >= maximum:
            return 1.0
        if not sigma or not premium:
            fixed = floor(min(maximum, minimum + premium * exp(-shift)) / increment) * increment
            return float(fixed <= bid)
        z = (log((bid + increment - minimum) / premium) + shift) / sigma
        return (1 + erf(z / sqrt(2))) / 2

    return tuple((b, cdf(b) - cdf(b - increment)) for b in range(minimum, maximum + 1, increment))


@pytest.mark.parametrize("volatility", [0.0, 0.25, 1.0])
@pytest.mark.parametrize("minimum,increment", [(1, 1), (2, 1), (4, 2)])
@pytest.mark.parametrize("indices", [(0,), (1,), (0, 1), (1, 2), (0, 1, 2)])
def test_distribution_prices_equal_exhaustive_joint_bids(volatility, minimum, increment, indices):
    league = config().league.model_copy(
        update={"teams": 3, "minimum_bid": minimum, "bid_increment": increment}
    )
    parameters = config().model.market.model_copy(update={"volatility": volatility})
    room = tuple(
        TeamBudget(id=str(j), owned=(), slots=1, budget=maximum, maximum_bid=maximum)
        for j, maximum in enumerate(
            (minimum + 2 * increment, minimum + 3 * increment, minimum + 4 * increment)
        )
    )
    participants = tuple((j, (0.5, 1.0, 2.0)[j]) for j in indices)
    anchor = minimum + 2.0 * increment
    shift = normalization_shift(3, volatility)
    pmfs = [
        bid_mass(
            room[j].maximum_bid, (anchor - minimum) * wealth, volatility, shift, minimum, increment
        )
        for j, wealth in participants
    ]
    expected = acquisition = mass = 0.0
    for rows in itertools.product(*pmfs):
        probability = prod(p for _, p in rows)
        bids = [b for b, _ in rows]
        ordered = sorted(bids, reverse=True)
        sale = min(ordered[0], ordered[1] + increment) if len(bids) > 1 else minimum
        high = max((b for j, b in zip(indices, bids, strict=True) if j != 0), default=0)
        needed = minimum if high <= minimum else high + increment
        mass += probability
        expected += probability * sale
        acquisition += probability * needed
    actual = distribution_price(league, parameters, anchor, participants, room, "0", shift, "p")
    assert mass == pytest.approx(1.0, abs=1e-12)
    assert actual.expected == pytest.approx(expected, abs=1e-12)
    assert actual.acquisition == pytest.approx(acquisition, abs=1e-12)
    assert actual.planning_cost == max(minimum, floor(acquisition / increment + 0.5) * increment)
    assert actual == distribution_price(
        league, parameters, anchor, participants[::-1], room, "0", shift, "p"
    )


@pytest.mark.parametrize("anchor", [0.0, 0.5, 1.0])
def test_floor_or_lower_anchor_never_generates_a_premium(anchor):
    league, parameters = config().league, config().model.market
    room = tuple(
        TeamBudget(id=str(j), owned=(), slots=1, budget=20, maximum_bid=20) for j in range(2)
    )
    result = distribution_price(
        league,
        parameters,
        anchor,
        ((0, 1.0), (1, 2.0)),
        room,
        "0",
        normalization_shift(2, parameters.volatility),
        "p",
    )
    assert result.expected == result.acquisition == result.planning_cost == 1


def sampled_parameters(market):
    data = market.model_dump(mode="json", exclude={"format_version"})
    data.update(samples=512, seed=0, normalization_samples=100000, normalization_seed=1)
    return SampledMarketParameters.model_validate_json(json.dumps(data))


def test_legacy_market_remains_readable_but_requires_explicit_rebuild():
    inputs = inputs_for((player(0),))
    original = inputs.config.model
    data = original.model_dump(mode="json")
    data["market"] = sampled_parameters(original.market).model_dump(mode="json")
    raw = json.dumps(data)
    legacy = ModelDocument.model_validate_json(raw).root
    assert legacy.model_dump(mode="json") == data
    inputs = inputs.model_copy(
        update={"config": inputs.config.model_copy(update={"model": legacy})}
    )
    with pytest.raises(ConfigError, match="prepare-auction"):
        market_context(inputs, state(inputs), "0" * 64)
    for field in ("samples", "seed", "normalization_samples", "normalization_seed"):
        bad = original.market.model_dump(mode="json")
        bad[field] = 1
        with pytest.raises(ValueError, match="Extra inputs"):
            MarketParameters.model_validate_json(json.dumps(bad))


def test_distribution_market_does_not_sample(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("A distribution calculation must not sample random bids")

    monkeypatch.setattr(np.random, "default_rng", fail)
    inputs = inputs_for(tuple(player(i, quote=float(i + 1)) for i in range(20)))
    _, first = market_context(inputs, state(inputs), "0" * 64)
    _, second = market_context(inputs, state(inputs), "0" * 64)
    assert first == second
