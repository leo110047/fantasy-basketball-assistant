from itertools import product
from math import floor, fsum

import numpy as np

from fba.contracts.auction import (
    AuctionInput,
    AuctionPlayer,
    AuctionResult,
    DraftState,
    Infeasible,
    ManagedFitSummary,
    MarketResult,
    Plan,
    Sale,
    TeamBudget,
)
from fba.contracts.base import DataError
from fba.contracts.config import AuctionModel, LeagueRules
from fba.contracts.paths import AuctionPath, Order, PathBid, PathPair, Regime, StressSettings
from fba.core.auction import portfolio_for
from fba.core.market import (
    bidders_by_position,
    market_factors,
    normalization_shift,
    price_market,
    require_distribution,
)
from fba.core.roster import completable, effective_players, validate_draft


def clearing(
    league: LeagueRules, bids: tuple[int, ...], ties: tuple[float, ...]
) -> tuple[int | None, int]:
    order = sorted(
        (i for i, bid in enumerate(bids) if bid >= league.minimum_bid),
        key=lambda i: (-bids[i], ties[i], i),
    )
    if not order:
        return None, 0
    buyer = order[0]
    amount = (
        min(bids[buyer], bids[order[1]] + league.bid_increment)
        if len(order) > 1
        else league.minimum_bid
    )
    return buyer, amount


def own_bid(
    inputs: AuctionInput,
    players: tuple[AuctionPlayer, ...],
    state: DraftState,
    market: MarketResult,
    player: int,
    eligible: tuple[bool, ...],
) -> tuple[int, int, str | None]:
    """Public information only: no order, regime, taste or future bids."""
    portfolio = portfolio_for(inputs, players, market, state)
    portfolio.available = tuple(a and b for a, b in zip(portfolio.available, eligible, strict=True))
    held = tuple(p for p in players if p.id in portfolio.owned)
    if (
        not portfolio.slots
        or not portfolio.available[player]
        or not completable(inputs.config.league, (*held, players[player]))
    ):
        return 0, 0, "ineligible buyer"
    base = portfolio.solve()
    if isinstance(base, Infeasible):
        return 0, portfolio.calls, base.reason
    amount = portfolio.cap(player, base)[0]
    return amount, portfolio.calls, None


class AuctionPaths:
    """One invocation, paired private draws, public-state rebidding, no shared cache."""

    def __init__(
        self,
        inputs: AuctionInput,
        state: DraftState,
        current: AuctionResult,
        settings: StressSettings,
    ) -> None:
        self.inputs, self.state, self.settings = inputs, state, settings
        self.league = inputs.config.league
        model = inputs.config.model
        if not isinstance(model, AuctionModel):
            raise DataError("auction_stress: requires auction model")
        self.parameters = require_distribution(model.market)
        self.players = effective_players(self.league, inputs.players, state)
        if isinstance(current.fit, ManagedFitSummary):
            utility = {p.id: p.utility for p in current.fit.players}
            if set(utility) != {p.id for p in self.players}:
                raise DataError("auction_stress: fitted population mismatch")
            self.players = tuple(
                p.model_copy(update={"utility": utility[p.id]}) for p in self.players
            )
        elif current.fit is not None and current.fit.selected_step:
            raise DataError("auction_stress: rebuild legacy fitted input")
        if not isinstance(current.plan, Plan):
            raise DataError("auction_stress: current public plan is infeasible")
        self.room = validate_draft(self.league, self.players, state)
        self.targets = set(current.plan.purchases)
        self.anchors, ranked, _, _ = market_factors(self.league, self.players, state, self.room)
        self.rank = {p.id: i for i, p in enumerate(ranked)}
        self.eligible = tuple(
            p.active
            and p.utility is not None
            and p.positions_confirmed
            and self.anchors[p.id] is not None
            for p in self.players
        )
        sold = {s.player_id for s in state.sales}
        if any(p.id in sold and (p.utility is None or not p.active) for p in self.players):
            raise DataError(
                "auction_stress: observed purchase has no usable projection or active team"
            )
        self.pool = tuple(
            i for i, p in enumerate(self.players) if self.eligible[i] and p.id not in sold
        )
        ordered = sorted(
            (p for p in self.players if self.anchors[p.id] is not None),
            key=lambda p: (-(self.anchors[p.id] or 0), -(p.fair or 0), p.id),
        )
        self.stars = {p.id for p in ordered[: self.league.teams * settings.premium_per_team]}
        self.shift = normalization_shift(self.league.teams, self.parameters.volatility)

    def validate_nomination(self, player_id: str, ceiling: int) -> int:
        candidates = [i for i in self.pool if self.players[i].id == player_id]
        own = next(t for t in self.room if t.id == self.state.mine)
        held = tuple(p for p in self.players if p.id in own.owned)
        if not candidates or not completable(self.league, (*held, self.players[candidates[0]])):
            raise DataError("auction_stress.player: unavailable or cannot complete positions")
        if (
            not self.league.minimum_bid <= ceiling <= own.maximum_bid
            or ceiling % self.league.bid_increment
        ):
            raise DataError("auction_stress.ceiling: outside legal bid range")
        return candidates[0]

    def opponent_bids(
        self,
        player: int,
        state: DraftState,
        room: tuple[TeamBudget, ...],
        regime: Regime,
        taste: tuple[float, ...],
    ) -> tuple[int, ...]:
        _, _, inflation, surplus = market_factors(self.league, self.players, state, room)
        participants = dict(
            bidders_by_position(
                self.league,
                self.parameters,
                (
                    self.players[player],
                    *tuple(p for p in self.players if any(p.id in t.owned for t in room)),
                ),
                room,
                surplus,
            )[tuple(sorted(self.players[player].positions))]
        )
        candidate = self.players[player]
        anchor = candidate.fair if regime == "category" else self.anchors[candidate.id]
        assert anchor is not None
        bids: list[int] = []
        for i, team in enumerate(room):
            if team.id == state.mine or i not in participants:
                bids.append(0)
                continue
            premium = (
                self.settings.premium_multiplier
                if regime == "premium"
                and candidate.id in self.stars
                and len(self.stars.intersection(team.owned)) < self.settings.premium_per_team
                else 1.0
            )
            willingness = (
                self.league.minimum_bid
                + max(0.0, anchor - self.league.minimum_bid)
                * inflation
                * participants[i]
                * taste[i]
                * premium
            )
            amount = min(team.maximum_bid, willingness)
            bids.append(floor(amount / self.league.bid_increment) * self.league.bid_increment)
        return tuple(bids)

    def continuation(
        self, player: int, ceiling: int, regime: Regime, order: Order, seed: int, participate: bool
    ) -> AuctionPath:
        rng = np.random.default_rng(seed)
        taste = np.exp(
            self.parameters.volatility * rng.standard_normal((len(self.players), len(self.room)))
            - self.shift
        )
        ties, mixed = rng.random(taste.shape), rng.random(len(self.players))
        queue = sorted(
            self.pool,
            key=lambda i: (
                (mixed[i], self.players[i].id)
                if order == "mixed"
                else (
                    int(order == "targets" and self.players[i].id not in self.targets),
                    self.rank[self.players[i].id],
                )
            ),
        )
        queue = [player, *(i for i in queue if i != player)]
        state, room = self.state, self.room
        first_sale: Sale | None = None
        bids: list[PathBid] = []
        calls = policy_calls = 0
        for sweep in range(self.settings.maximum_sweeps):
            before = len(state.sales)
            for i in queue:
                if not any(t.slots for t in room):
                    break
                if self.players[i].id in {s.player_id for s in state.sales}:
                    continue
                amounts = list(
                    self.opponent_bids(i, state, room, regime, tuple(float(v) for v in taste[i]))
                )
                mine = next(j for j, t in enumerate(room) if t.id == state.mine)
                policy_calls += int(bool(room[mine].slots) and not (sweep == 0 and i == player))
                amount, count, reason = self.public_bid(
                    i, player, ceiling, sweep, participate, state, room
                )
                calls += count
                amounts[mine] = amount
                bids.append(
                    PathBid(player_id=self.players[i].id, sweep=sweep, amount=amount, reason=reason)
                )
                buyer, price = clearing(
                    self.league, tuple(amounts), tuple(float(v) for v in ties[i])
                )
                if buyer is None:
                    continue
                sale = Sale(
                    id=self.sale_id(state),
                    player_id=self.players[i].id,
                    buyer=room[buyer].id,
                    amount=price,
                )
                state = state.model_copy(update={"sales": (*state.sales, sale)})
                room = validate_draft(self.league, self.players, state)
                if sweep == 0 and i == player:
                    first_sale = sale
            if sweep and len(state.sales) == before:
                break
        own = next(t for t in room if t.id == state.mine)
        values = {p.id: p.utility for p in self.players if p.utility is not None}
        if not set(own.owned) <= values.keys():
            raise DataError("auction_stress: final roster has missing utility")
        return AuctionPath(
            complete=not any(t.slots for t in room),
            room=room,
            score=fsum(values[i] for i in own.owned),
            first_sale=first_sale,
            sales=state.sales[len(self.state.sales) :],
            bids=tuple(bids),
            solver_calls=calls,
            policy_calls=policy_calls,
        )

    def public_bid(
        self,
        candidate: int,
        first: int,
        ceiling: int,
        sweep: int,
        participate: bool,
        state: DraftState,
        room: tuple[TeamBudget, ...],
    ) -> tuple[int, int, str | None]:
        if sweep == 0 and candidate == first:
            return (
                (ceiling if participate else 0),
                0,
                "initial ceiling" if participate else "skip current nomination",
            )
        if not next(t for t in room if t.id == state.mine).slots:
            return 0, 0, "roster full"
        market = price_market(self.league, self.parameters, self.players, state, room)
        return own_bid(self.inputs, self.players, state, market, candidate, self.eligible)

    @staticmethod
    def sale_id(state: DraftState) -> str:
        candidate = f"stress-{len(state.sales)}"
        existing = {s.id for s in state.sales}
        while candidate in existing:
            candidate += "-"
        return candidate

    def compare(self, player_id: str, ceiling: int) -> tuple[PathPair, ...]:
        player = self.validate_nomination(player_id, ceiling)
        pairs: list[PathPair] = []
        for regime, order, seed in product(
            self.settings.regimes, self.settings.orders, self.settings.seeds
        ):
            buy = self.continuation(player, ceiling, regime, order, seed, True)
            skip = self.continuation(player, ceiling, regime, order, seed, False)
            pairs.append(
                PathPair(
                    regime=regime,
                    order=order,
                    seed=seed,
                    participate=buy,
                    skip=skip,
                    delta=buy.score - skip.score if buy.complete and skip.complete else None,
                )
            )
        return tuple(pairs)
