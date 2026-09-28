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


def opening_anchors(
    league: LeagueRules, players: tuple[AuctionPlayer, ...]
) -> dict[str, float | None]:
    quoted = sorted(
        (p for p in players if p.projected_price is not None),
        key=lambda p: (-(p.projected_price or 0), p.id),
    )
    top = quoted[: league.teams * capacity(league)]
    # Exact piecewise-linear water filling; unknown quotes never enter the floor pool.
    active = tuple(p for p in top if p.projected_price)
    scale = 0.0
    while active:
        budget = league.teams * league.budget - (len(top) - len(active)) * league.minimum_bid
        scale = budget / fsum(p.projected_price or 0 for p in active)
        above = tuple(p for p in active if (p.projected_price or 0) * scale > league.minimum_bid)
        if above == active:
            break
        active = above
    selected = {p.id for p in top}
    return {
        p.id: None
        if p.projected_price is None
        else max(league.minimum_bid, (p.projected_price or 0) * scale)
        if p.id in selected
        else float(league.minimum_bid)
        for p in players
    }


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
    denominator = fsum(max(0, (anchors[p.id] or 0) - league.minimum_bid) for p in remaining[:slots])
    inflation = max(0.0, surplus / denominator) if denominator > 0 else 1.0
    average_surplus = max(surplus / max(slots, 1), np.finfo(float).eps)
    z = np.random.default_rng(parameters.seed).standard_normal((parameters.samples, league.teams))
    normal = np.random.default_rng(parameters.normalization_seed).standard_normal(
        (parameters.normalization_samples, league.teams)
    )
    shift = float(np.log(np.exp(parameters.volatility * np.sort(normal, axis=1)[:, -2]).mean()))
    multipliers = np.exp(parameters.volatility * z - shift)
    eligible = bidders_by_position(league, parameters, players, room, average_surplus)
    own = next(t for t in room if t.id == state.mine)
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
            np.where(high == own.maximum_bid, high, high + league.bid_increment),
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
