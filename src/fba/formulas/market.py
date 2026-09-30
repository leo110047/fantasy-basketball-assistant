from math import exp, fsum

import numpy as np
from numpy.typing import NDArray

from fba.contracts.auction import AuctionPlayer, DraftState, MarketPrice, MarketResult, TeamBudget
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import (
    LeagueRules,
    MarketAssumptions,
    MarketParameters,
    SampledMarketParameters,
)
from fba.core.roster import capacity, completable
from fba.formulas.arrays import evaluate_array
from fba.formulas.registry import evaluate

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
        wealth = evaluate(
            "bidder_wealth",
            budget=float(team.budget),
            slots=float(team.slots),
            minimum=float(league.minimum_bid),
            average_surplus=average_surplus,
            lower=parameters.wealth_lower,
            upper=parameters.wealth_upper,
            exponent=parameters.wealth_exponent,
        ).result
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
        else evaluate(
            "anchor",
            quote=p.projected_price,
            scale=scale,
            minimum=float(minimum),
            maximum=float(maximum),
            cutoff=cutoff,
        ).result
        for p in players
    }


def normalization_shift(teams: int, volatility: float) -> float:
    try:
        return evaluate("market_normalization", teams=float(teams), volatility=volatility).result
    except DataError as error:
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
    return evaluate_array(
        "bid_distribution",
        levels=levels,
        maximum=maximum,
        premium=premium,
        volatility=volatility,
        shift=shift,
        minimum=float(minimum),
        increment=float(increment),
    ).result


def sale_survival(cdf: FloatArray, previous: FloatArray) -> FloatArray:
    return evaluate_array("second_bid_survival", cdf=cdf, previous=previous).result


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
    expected = evaluate(
        "sale_cost",
        minimum=float(minimum),
        increment=float(increment),
        survival=tuple(float(x) for x in sale_survival(cdf, previous)),
    )
    foes = np.array([room[j].id != mine for j, _ in participants])
    high_survival = 1 - np.prod(cdf[foes], axis=0)
    # Floor ties assume our nomination; every higher foe bid must be beaten.
    acquisition = evaluate(
        "winning_cost",
        minimum=float(minimum),
        increment=float(increment),
        survival=tuple(float(x) for x in high_survival),
    )
    planning = evaluate(
        "planning_cost",
        minimum=float(minimum),
        increment=float(increment),
        acquisition=acquisition.result,
    )
    median = np.floor(np.minimum(maximum, minimum + premium * exp(-shift)) / increment) * increment
    bidders = int(np.count_nonzero(foes & (median >= parameters.competition_bid)))
    return MarketPrice(
        player_id=player_id,
        anchor=anchor,
        expected=expected.result,
        acquisition=acquisition.result,
        # The integer point estimate nearest to E[cost] avoids a full-increment
        # upward bias for an arbitrarily small upper-tail probability.
        planning_cost=int(planning.result),
        traces=(expected, acquisition, planning),
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


def market_factors(
    league: LeagueRules,
    players: tuple[AuctionPlayer, ...],
    state: DraftState,
    room: tuple[TeamBudget, ...],
) -> tuple[dict[str, float | None], tuple[AuctionPlayer, ...], float, float]:
    anchors = opening_anchors(league, players)
    for override in state.overrides:
        if override.market is not None:
            anchors[override.player_id] = override.market
    sold = {s.player_id for s in state.sales}
    remaining = tuple(
        sorted(
            (p for p in players if p.id not in sold and anchors[p.id] is not None),
            key=lambda p: (-(anchors[p.id] or 0), -(p.fair or 0), p.id),
        )
    )
    slots = sum(t.slots for t in room)
    surplus = sum(t.budget - t.slots * league.minimum_bid for t in room if t.slots)
    denominator = fsum(max(league.minimum_bid, anchors[p.id] or 0) for p in remaining[:slots])
    denominator += max(0, slots - len(remaining)) * league.minimum_bid
    cash = sum(t.budget for t in room if t.slots)
    inflation = evaluate("inflation", cash=float(cash), anchor_total=denominator).result
    average_surplus = max(surplus / max(slots, 1), np.finfo(float).eps)
    return anchors, remaining, inflation, average_surplus


def price_market(
    league: LeagueRules,
    parameters: MarketParameters | SampledMarketParameters,
    players: tuple[AuctionPlayer, ...],
    state: DraftState,
    room: tuple[TeamBudget, ...],
) -> MarketResult:
    parameters = require_distribution(parameters)
    anchors, remaining, inflation, average_surplus = market_factors(league, players, state, room)
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
