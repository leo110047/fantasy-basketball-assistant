from typing import Literal, Protocol

from fba.contracts.auction import (
    AuctionInput,
    AuctionPlayer,
    AuctionResult,
    Cap,
    Comparison,
    DraftState,
    Infeasible,
    MarketResult,
    Nominations,
    Plan,
)
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import AuctionModel, ManagedPricingModel
from fba.contracts.season import SeasonKernel
from fba.core.fit import FeatureRunner, FittedUtility
from fba.core.market import price_market
from fba.core.portfolio import Portfolio
from fba.core.roster import completable, effective_players, validate_draft


class CapRunner(Protocol):
    def __call__(
        self, portfolio: Portfolio, base: Plan, candidates: tuple[int, ...]
    ) -> tuple[tuple[tuple[int, float | None, bool], ...], int]: ...


def result_number(value: float, decimals: int) -> float:
    rounded = round(value, decimals)
    return rounded if rounded else 0.0


def result_plan(plan: Plan | Infeasible, decimals: int) -> Plan | Infeasible:
    return (
        plan.model_copy(update={"utility": result_number(plan.utility, decimals)})
        if isinstance(plan, Plan)
        else plan
    )


def run_caps(
    portfolio: Portfolio, base: Plan, candidates: tuple[int, ...]
) -> tuple[tuple[tuple[int, float | None, bool], ...], int]:
    calls = portfolio.calls
    result = tuple(portfolio.cap(i, base) for i in candidates)
    return result, portfolio.calls - calls


def market_context(
    inputs: AuctionInput, state: DraftState, input_hash: str
) -> tuple[tuple[AuctionPlayer, ...], MarketResult]:
    if state.config != inputs.config.refs or state.input_sha256 != input_hash:
        raise DataError("draft: configuration or auction input hash mismatch")
    if not isinstance(inputs.config.model, AuctionModel):
        raise ConfigError("model: auction requires model format 5")
    league = inputs.config.league
    players = effective_players(league, inputs.players, state)
    room = validate_draft(league, players, state)
    return players, price_market(league, inputs.config.model.market, players, state, room)


def portfolio_for(
    inputs: AuctionInput,
    players: tuple[AuctionPlayer, ...],
    market: MarketResult,
    state: DraftState,
    *,
    prune: bool = True,
) -> Portfolio:
    assert isinstance(inputs.config.model, AuctionModel)
    own = next(t for t in market.room if t.id == state.mine)
    sold = {s.player_id for s in state.sales}
    prices = {q.player_id: q for q in market.prices}
    return Portfolio(
        inputs.config.league,
        inputs.config.model.solver,
        players,
        tuple(prices[p.id].planning_cost for p in players),
        tuple(
            p.id not in sold and p.utility is not None and prices[p.id].planning_cost is not None
            for p in players
        ),
        own.owned,
        own.budget,
        prune=prune,
    )


def caps_for(
    portfolio: Portfolio,
    market: MarketResult,
    base: Plan | Infeasible,
    runner: CapRunner = run_caps,
) -> tuple[Cap, ...]:
    sold = {i for t in market.room for i in t.owned}
    held = tuple(p for p in portfolio.players if p.id in portfolio.owned)
    prices = {q.player_id: q for q in market.prices}
    candidates = tuple(
        i
        for i, p in enumerate(portfolio.players)
        if p.id not in sold
        and p.utility is not None
        and p.active
        and portfolio.slots
        and isinstance(base, Plan)
        and completable(portfolio.league, (*held, p))
    )
    computed: dict[int, tuple[int, float | None, bool]] = {}
    if isinstance(base, Plan):
        before = portfolio.calls
        values, calls = runner(portfolio, base, candidates)
        portfolio.calls = before + calls
        computed = dict(zip(candidates, values, strict=True))
    caps: list[Cap] = []
    for i, player in enumerate(portfolio.players):
        amount: int | None = None
        reason: str | None = None
        loss: float | None = None
        forced = False
        if player.id in sold:
            reason = "sold"
        elif player.utility is None or not player.active:
            reason = "projection or active team unavailable"
        elif not portfolio.slots:
            amount, reason = 0, "roster full"
        elif isinstance(base, Infeasible):
            reason = base.reason
        elif not completable(portfolio.league, (*held, player)):
            amount, reason = 0, "position cannot complete roster"
        else:
            amount, loss, forced = computed[i]
        caps.append(
            Cap(
                player_id=player.id,
                amount=amount,
                reason=reason,
                conditional=not player.positions_confirmed or prices[player.id].anchor is None,
                forced=forced,
                loss=loss,
            )
        )
    return tuple(caps)


def nominations(
    caps: tuple[Cap, ...], market: MarketResult, base: Plan | Infeasible, count: int
) -> Nominations:
    chosen: set[str] = set(base.purchases) if isinstance(base, Plan) else set()
    prices = {q.player_id: q for q in market.prices}
    known = tuple(
        c for c in caps if c.amount is not None and not c.conditional and c.reason is None
    )
    target = sorted(
        (
            c
            for c in known
            if c.player_id in chosen
            and c.amount is not None
            and prices[c.player_id].planning_cost is not None
            and c.amount >= (prices[c.player_id].planning_cost or 0)
        ),
        key=lambda c: (
            -int(c.forced),
            -(c.loss or 0),
            -((c.amount or 0) - (prices[c.player_id].expected or 0)),
            c.player_id,
        ),
    )
    drain = sorted(
        (
            c
            for c in known
            if c.player_id not in chosen
            and prices[c.player_id].expected is not None
            and (prices[c.player_id].expected or 0) > (c.amount or 0)
            and prices[c.player_id].bidders >= 2
        ),
        key=lambda c: (-(prices[c.player_id].expected or 0), c.player_id),
    )
    return Nominations(
        target=tuple(c.player_id for c in target[:count]),
        drain=tuple(c.player_id for c in drain[:count]),
        mode="target" if not drain or any(c.forced for c in target) else "drain",
    )


def calculate_auction(
    inputs: AuctionInput,
    state: DraftState,
    input_hash: str,
    state_hash: str,
    *,
    prune: bool = True,
    runner: CapRunner = run_caps,
    mode: Literal["equal", "fit"] = "equal",
    kernel: SeasonKernel | None = None,
    feature_runner: FeatureRunner | None = None,
) -> AuctionResult:
    players, market = market_context(inputs, state, input_hash)
    portfolio = portfolio_for(inputs, players, market, state, prune=prune)
    base = portfolio.solve(canonical=True)
    assert isinstance(inputs.config.model, AuctionModel)
    fit = None
    if mode == "fit":
        if inputs.management is None or kernel is None:
            raise DataError("fit: managed projections and a native kernel are required")
        held = {s.player_id for s in state.sales}
        if any(p.id in held and (p.utility is None or not p.active) for p in players):
            raise DataError("fit: an observed purchase has no usable projection or active team")
        if isinstance(base, Plan) and base.purchases:
            fitted = FittedUtility(
                portfolio,
                market,
                state.mine,
                inputs.config.model.fit,
                inputs.management,
                kernel,
                base,
                inputs.config.model.pricing
                if isinstance(inputs.config.model, ManagedPricingModel)
                else None,
                inputs.config.model.management
                if isinstance(inputs.config.model, ManagedPricingModel)
                else None,
            )
            portfolio, base, fit = fitted.solve(base, feature_runner)
    caps = caps_for(portfolio, market, base, runner)
    decimals = inputs.config.model.solver.result_decimals
    caps = tuple(
        c.model_copy(
            update={"loss": result_number(c.loss, decimals) if c.loss is not None else None}
        )
        for c in caps
    )
    if fit is not None:
        fit = fit.model_copy(
            update={
                "steps": tuple(
                    s.model_copy(
                        update={
                            key: result_number(getattr(s, key), decimals)
                            for key in ("score", "block_minimum", "block_maximum")
                        }
                    )
                    for s in fit.steps
                )
            }
        )
    return AuctionResult(
        format_version=1,
        algorithm="conditional-auction-v5",
        config=inputs.config.refs,
        input_sha256=input_hash,
        state_sha256=state_hash,
        market=market,
        plan=result_plan(base, decimals),
        caps=caps,
        nominations=nominations(caps, market, base, inputs.config.model.solver.nomination_count),
        solver_calls=portfolio.calls,
        fit=fit,
    )


def comparison_branch(portfolio: Portfolio, exclude: int | None) -> tuple[Plan | Infeasible, int]:
    plan = portfolio.solve(exclude=exclude, canonical=True)
    return plan, portfolio.calls


def comparison_plans(
    portfolio: Portfolio, player: int, branch: Portfolio
) -> tuple[Plan | Infeasible, Plan | Infeasible, int]:
    skip, skip_calls = comparison_branch(portfolio, player)
    buy, buy_calls = comparison_branch(branch, None)
    return skip, buy, skip_calls + buy_calls


class ComparisonRunner(Protocol):
    def __call__(
        self, portfolio: Portfolio, player: int, branch: Portfolio
    ) -> tuple[Plan | Infeasible, Plan | Infeasible, int]: ...


def compare(
    portfolio: Portfolio,
    player_id: str,
    price: int,
    runner: ComparisonRunner = comparison_plans,
) -> Comparison:
    candidates = [i for i, p in enumerate(portfolio.players) if p.id == player_id]
    if not candidates or player_id in portfolio.owned:
        raise DataError("comparison.player_id: unknown or already owned")
    player = candidates[0]
    if not portfolio.available[player]:
        raise DataError("comparison.player_id: unavailable market candidate")
    max_bid = portfolio.budget - (portfolio.slots - 1) * portfolio.league.minimum_bid
    if (
        not portfolio.league.minimum_bid <= price <= max_bid
        or price % portfolio.league.bid_increment
    ):
        raise DataError("comparison.price: outside legal bid range")
    branch = Portfolio(
        portfolio.league,
        portfolio.parameters,
        portfolio.players,
        portfolio.costs,
        tuple(allowed and i != player for i, allowed in enumerate(portfolio.available)),
        (*portfolio.owned, player_id),
        portfolio.budget - price,
        prune=portfolio.prune,
    )
    skip, buy, calls = runner(portfolio, player, branch)
    if isinstance(buy, Plan):
        buy = buy.model_copy(
            update={
                "cost": buy.cost + price,
                "utility": buy.utility + float(portfolio.values[player]),
                "purchases": tuple(sorted((*buy.purchases, player_id))),
            }
        )
    delta = buy.utility - skip.utility if isinstance(buy, Plan) and isinstance(skip, Plan) else None
    decimals = portfolio.parameters.result_decimals
    return Comparison(
        player_id=player_id,
        price=price,
        buy=result_plan(buy, decimals),
        skip=result_plan(skip, decimals),
        delta=result_number(delta, decimals) if delta is not None else None,
        solver_calls=calls,
    )
