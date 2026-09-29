from math import ceil, fsum

import numpy as np

from fba.contracts.auction import AuctionPlayer, DraftState, MarketPrice, MarketResult, TeamBudget
from fba.contracts.config import LeagueRules, MarketParameters
from fba.core.roster import capacity, completable


def bidders_by_position(
    league: LeagueRules,
    parameters: MarketParameters,
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


def bidder_order(
    players: tuple[AuctionPlayer, ...], room: tuple[TeamBudget, ...], mine: str
) -> list[int]:
    positions = {p.id: tuple(sorted(p.positions)) for p in players}
    # Equal states are exchangeable; IDs never choose the random coordinate of a bidder.
    return sorted(
        range(len(room)),
        key=lambda j: (
            room[j].id != mine,
            room[j].budget,
            room[j].slots,
            tuple(sorted(positions[p] for p in room[j].owned)),
        ),
    )


def price_market(
    league: LeagueRules,
    parameters: MarketParameters,
    players: tuple[AuctionPlayer, ...],
    state: DraftState,
    room: tuple[TeamBudget, ...],
) -> MarketResult:
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
    z = np.random.default_rng(parameters.seed).standard_normal((parameters.samples, league.teams))
    normal = np.random.default_rng(parameters.normalization_seed).standard_normal(
        (parameters.normalization_samples, league.teams)
    )
    shift = float(np.log(np.exp(parameters.volatility * np.sort(normal, axis=1)[:, -2]).mean()))
    multipliers = np.empty_like(z)
    multipliers[:, bidder_order(players, room, state.mine)] = np.exp(
        parameters.volatility * z - shift
    )
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
        indices = [j for j, _ in participants]
        wealth = np.array([value for _, value in participants])
        maximum = np.array([room[j].maximum_bid for j in indices])
        bids = (
            np.floor(
                np.minimum(
                    maximum[:, None],
                    np.maximum(
                        league.minimum_bid,
                        league.minimum_bid
                        + (anchor - league.minimum_bid)
                        * inflation
                        * wealth[:, None]
                        * multipliers[:, indices].T,
                    ),
                )
                / league.bid_increment
            )
            * league.bid_increment
        )
        foe_rows = np.array([room[j].id != state.mine for j in indices])
        foes = bids[foe_rows]
        lower, upper = (parameters.samples - 1) // 2, parameters.samples // 2
        middle = np.partition(foes, (lower, upper), axis=1)
        bidders = int(
            np.count_nonzero(
                (maximum[foe_rows] >= parameters.competition_bid)
                & ((middle[:, lower] + middle[:, upper]) / 2 >= parameters.competition_bid)
            )
        )
        ordered = np.sort(bids, axis=0)
        expected = (
            float(np.minimum(ordered[-1], ordered[-2] + league.bid_increment).mean())
            if len(bids) > 1
            else float(league.minimum_bid)
        )
        high = np.max(foes, axis=0) if len(foes) else np.zeros(parameters.samples)
        needed = np.where(
            high <= league.minimum_bid,
            league.minimum_bid,
            high + league.bid_increment,
        )
        acquisition = float(needed.mean())
        prices[player.id] = MarketPrice(
            player_id=player.id,
            anchor=anchor,
            expected=expected,
            acquisition=acquisition,
            planning_cost=ceil(acquisition / league.bid_increment) * league.bid_increment,
            bidders=bidders,
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
