from math import exp, floor, fsum, isfinite, log, pi

import numpy as np
from numpy.typing import NDArray
from scipy.integrate import quad
from scipy.special import log_ndtr, ndtr

from fba.contracts.auction import AuctionPlayer, DraftState, MarketPrice, MarketResult, TeamBudget
from fba.contracts.base import ConfigError
from fba.contracts.config import (
    LeagueRules,
    MarketAssumptions,
    MarketParameters,
    SampledMarketParameters,
)
from fba.core.roster import capacity, completable

type FloatArray = NDArray[np.float64]


def bidders_by_position(
    league: LeagueRules,
    parameters: MarketAssumptions,
    players: tuple[AuctionPlayer, ...],
    room: tuple[TeamBudget, ...],
    average_surplus: float,
) -> dict[tuple[str, ...], tuple[tuple[int, float], ...]]:
    by_id = {p.id: p for p in players}
    representatives = {tuple(sorted(p.positions)): p for p in players}
    bidders: dict[tuple[str, ...], list[tuple[int, float]]] = {key: [] for key in representatives}
    for j, team in enumerate(room):
        if not team.slots:
            continue
        held = tuple(by_id[i] for i in team.owned)
        wealth = float(
            np.clip(
                (team.budget / team.slots - league.minimum_bid) / average_surplus,
                parameters.wealth_lower,
                parameters.wealth_upper,
            )
            ** parameters.wealth_exponent
        )
        for positions, player in representatives.items():
            # Matching cardinality depends on positions, never player identity or price.
            if completable(league, (*held, player)):
                bidders[positions].append((j, wealth))
    return {key: tuple(value) for key, value in bidders.items()}


def anchor_scale(quotes: tuple[float, ...], cash: int, minimum: int, maximum: int) -> float:
    total = float(len(quotes) * minimum)
    target = min(cash, sum(maximum if q > 0 else minimum for q in quotes))
    if target <= total:
        return 0.0
    events: dict[float, list[float]] = {}
    for quote in quotes:
        if quote > 0:
            events.setdefault(minimum / quote, []).append(quote)
            events.setdefault(maximum / quote, []).append(-quote)
    previous = slope = 0.0
    for point, changes in sorted(events.items()):
        upper = total + (point - previous) * slope
        if slope > 0 and upper >= target:
            return previous + (target - total) / slope
        total, previous = upper, point
        slope = fsum((slope, *changes))
    return previous


def opening_anchors(
    league: LeagueRules, players: tuple[AuctionPlayer, ...]
) -> dict[str, float | None]:
    quoted = sorted(
        (p.projected_price for p in players if p.projected_price is not None), reverse=True
    )
    slots = league.teams * capacity(league)
    top = tuple(quoted[:slots])
    minimum = league.minimum_bid
    maximum = league.budget - (capacity(league) - 1) * minimum
    cash = league.teams * league.budget - (slots - len(top)) * minimum
    scale = anchor_scale(top, cash, minimum, maximum)
    cutoff = top[-1] if top else 0.0
    return {
        p.id: None
        if p.projected_price is None
        else min(maximum, max(minimum, p.projected_price * scale))
        if p.projected_price >= cutoff
        else float(minimum)
        for p in players
    }


def normalization_shift(teams: int, volatility: float) -> float:
    """Log E[exp(sigma * Z_(n-1))] for the second highest of n normal bids."""
    if volatility == 0:
        return 0.0

    def integrand(z: float) -> float:
        return exp(
            volatility * z
            + log(teams * (teams - 1))
            + (teams - 2) * float(log_ndtr(z))
            + float(log_ndtr(-z))
            - z * z / 2
            - log(2 * pi) / 2
        )

    try:
        report = quad(integrand, -np.inf, np.inf, full_output=1, epsabs=1e-10, epsrel=1e-10)
        value, error, _diagnostics, *message = report
        if message or not isfinite(value) or value <= 0 or error > 1e-8 * max(1.0, value):
            raise ArithmeticError("normalization did not converge")
        return log(value)
    except (ArithmeticError, ValueError) as error:
        raise ConfigError("model.market.volatility: cannot resolve bid normalization") from error


def bid_cdf(
    levels: FloatArray,
    maximum: FloatArray,
    premium: FloatArray,
    volatility: float,
    shift: float,
    minimum: int,
    increment: int,
) -> FloatArray:
    """P(discrete, capped bid <= level), with a floor at the minimum bid."""
    threshold = np.maximum(0.0, levels + increment - minimum)
    ratio = np.divide(
        threshold[None, :],
        premium[:, None],
        out=np.full((len(premium), len(levels)), np.inf),
        where=premium[:, None] > 0,
    )
    if volatility == 0:
        probability = (
            minimum + premium[:, None] * exp(-shift) < levels[None, :] + increment
        ).astype(float)
    else:
        logarithm = np.full(ratio.shape, -np.inf)
        np.log(ratio, out=logarithm, where=ratio > 0)
        probability = ndtr((logarithm + shift) / volatility)
    return np.where(
        levels[None, :] < minimum,
        0.0,
        np.where(levels[None, :] >= maximum[:, None], 1.0, probability),
    )


def sale_survival(cdf: FloatArray, previous: FloatArray) -> FloatArray:
    # At level b, the sale price exceeds b unless everybody bids <= b, or
    # exactly one bids > b while all the others bid < b (one increment below).
    edge = np.ones((1, cdf.shape[1]))
    prefix = np.concatenate((edge, np.cumprod(previous, axis=0)), axis=0)
    suffix = np.concatenate((np.cumprod(previous[::-1], axis=0)[::-1], edge), axis=0)
    single = np.sum((1 - cdf) * prefix[:-1] * suffix[1:], axis=0)
    return np.clip(1 - np.prod(cdf, axis=0) - single, 0, 1)


def distribution_price(
    league: LeagueRules,
    parameters: MarketParameters,
    anchor: float,
    participants: tuple[tuple[int, float], ...],
    room: tuple[TeamBudget, ...],
    mine: str,
    shift: float,
    player_id: str,
) -> MarketPrice:
    minimum, increment = league.minimum_bid, league.bid_increment
    # A stable economic order also makes floating-point reductions independent
    # of team identifiers. Identical distributions are interchangeable.
    participants = tuple(
        sorted(participants, key=lambda p: (room[p[0]].id != mine, room[p[0]].maximum_bid, p[1]))
    )
    maximum = np.array([room[j].maximum_bid for j, _ in participants], dtype=float)
    premium = np.array([max(0.0, anchor - minimum) * wealth for _, wealth in participants])
    levels = np.arange(minimum, maximum.max(), increment, dtype=float)
    cdf = bid_cdf(levels, maximum, premium, parameters.volatility, shift, minimum, increment)
    previous = bid_cdf(
        levels - increment, maximum, premium, parameters.volatility, shift, minimum, increment
    )
    expected = minimum + increment * fsum(sale_survival(cdf, previous))
    foes = np.array([room[j].id != mine for j, _ in participants])
    high_survival = 1 - np.prod(cdf[foes], axis=0)
    # Floor ties assume our nomination; every higher foe bid must be beaten.
    acquisition = minimum + increment * (
        fsum(high_survival) + (float(high_survival[0]) if len(levels) else 0)
    )
    median = np.floor(np.minimum(maximum, minimum + premium * exp(-shift)) / increment) * increment
    bidders = int(np.count_nonzero(foes & (median >= parameters.competition_bid)))
    return MarketPrice(
        player_id=player_id,
        anchor=anchor,
        expected=expected,
        acquisition=acquisition,
        # The integer point estimate nearest to E[cost] avoids a full-increment
        # upward bias for an arbitrarily small upper-tail probability.
        planning_cost=max(minimum, floor(acquisition / increment + 0.5) * increment),
        bidders=bidders,
    )


def require_distribution(
    parameters: MarketParameters | SampledMarketParameters,
) -> MarketParameters:
    if not isinstance(parameters, MarketParameters):
        raise ConfigError(
            "model.market: sampled pricing is retired; set format_version to 2, remove samples, "
            "seed, normalization_samples and normalization_seed, "
            "then prepare-auction with that model"
        )
    return parameters


def price_market(
    league: LeagueRules,
    parameters: MarketParameters | SampledMarketParameters,
    players: tuple[AuctionPlayer, ...],
    state: DraftState,
    room: tuple[TeamBudget, ...],
) -> MarketResult:
    parameters = require_distribution(parameters)
    anchors = opening_anchors(league, players)
    for override in state.overrides:
        if override.market is not None:
            anchors[override.player_id] = override.market
    sold = {s.player_id for s in state.sales}
    remaining = sorted(
        (p for p in players if p.id not in sold and anchors[p.id] is not None),
        key=lambda p: (-(anchors[p.id] or 0), -(p.fair or 0), p.id),
    )
    slots = sum(t.slots for t in room)
    surplus = sum(t.budget - t.slots * league.minimum_bid for t in room if t.slots)
    denominator = fsum(max(league.minimum_bid, anchors[p.id] or 0) for p in remaining[:slots])
    denominator += max(0, slots - len(remaining)) * league.minimum_bid
    cash = sum(t.budget for t in room if t.slots)
    inflation = cash / denominator if denominator > 0 else 1.0
    average_surplus = max(surplus / max(slots, 1), np.finfo(float).eps)
    shift = normalization_shift(league.teams, parameters.volatility)
    eligible = bidders_by_position(league, parameters, players, room, average_surplus)
    prices: dict[str, MarketPrice] = {}
    quotes_by_anchor_position: dict[tuple[float, tuple[str, ...]], MarketPrice] = {}
    for player in remaining:
        anchor = anchors[player.id]
        assert anchor is not None
        key = (anchor, tuple(sorted(player.positions)))
        if key in quotes_by_anchor_position:
            prices[player.id] = quotes_by_anchor_position[key].model_copy(
                update={"player_id": player.id}
            )
            continue
        participants = eligible[key[1]]
        if not participants:
            continue
        prices[player.id] = distribution_price(
            league,
            parameters,
            anchor,
            tuple((j, wealth * inflation) for j, wealth in participants),
            room,
            state.mine,
            shift,
            player.id,
        )
        quotes_by_anchor_position[key] = prices[player.id]
    return MarketResult(
        room=room,
        prices=tuple(
            prices.get(
                p.id,
                MarketPrice(
                    player_id=p.id,
                    anchor=anchors[p.id],
                    expected=None,
                    acquisition=None,
                    planning_cost=None,
                    bidders=0,
                ),
            )
            for p in players
        ),
        inflation=inflation,
    )
