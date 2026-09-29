from itertools import combinations

from fba.contracts.auction import Assignment, AuctionPlayer, DraftState, TeamBudget
from fba.contracts.base import DataError
from fba.contracts.config import LeagueRules
from fba.core.config import require_members, unique


def capacity(league: LeagueRules) -> int:
    return len(league.starter_slots) + league.bench_slots


def match_slots(
    eligible: tuple[tuple[bool, ...], ...], order: tuple[int, ...], slots: int
) -> tuple[tuple[int, ...], tuple[int | None, ...]]:
    matched: dict[int, int] = {}

    def augment(player: int, seen: set[int]) -> bool:
        for slot in range(slots):
            if slot in seen or not eligible[player][slot]:
                continue
            seen.add(slot)
            if slot not in matched or augment(matched[slot], seen):
                matched[slot] = player
                return True
        return False

    chosen: list[int] = []
    for player in order:
        if augment(player, set()):
            chosen.append(player)
        if len(chosen) == slots:
            break
    return tuple(chosen), tuple(matched.get(slot) for slot in range(slots))


def assign(league: LeagueRules, players: tuple[AuctionPlayer, ...]) -> tuple[Assignment, ...]:
    eligible = tuple(
        tuple(
            bool(set(p.positions).intersection(slot.eligible_positions))
            for slot in league.starter_slots
        )
        for p in players
    )
    order = tuple(sorted(range(len(players)), key=lambda i: players[i].id))
    _, assigned = match_slots(eligible, order, len(league.starter_slots))
    return tuple(
        Assignment(slot_id=league.starter_slots[i].id, player_id=players[p].id)
        for i, p in enumerate(assigned)
        if p is not None
    )


def completable(league: LeagueRules, players: tuple[AuctionPlayer, ...]) -> bool:
    return len(players) <= capacity(league) and (
        len(assign(league, players)) + capacity(league) - len(players) >= len(league.starter_slots)
    )


def hall_constraints(league: LeagueRules) -> tuple[tuple[frozenset[str], int], ...]:
    rows: list[tuple[frozenset[str], int]] = []
    for size in range(1, len(league.positions) + 1):
        for positions in combinations(sorted(league.positions), size):
            union = frozenset(positions)
            need = sum(set(slot.eligible_positions) <= union for slot in league.starter_slots)
            if need:
                rows.append((union, need))
    return tuple(rows)


def effective_players(
    league: LeagueRules, players: tuple[AuctionPlayer, ...], state: DraftState
) -> tuple[AuctionPlayer, ...]:
    unique(tuple(p.id for p in players), "auction.players")
    unique(tuple(o.player_id for o in state.overrides), "draft.overrides")
    by_id = {p.id: p for p in players}
    require_members(tuple(o.player_id for o in state.overrides), set(by_id), "draft.overrides")
    for override in state.overrides:
        player = by_id[override.player_id]
        positions = player.positions if override.positions is None else override.positions
        if (
            override.market is not None
            and not league.minimum_bid <= override.market <= league.budget
        ):
            raise DataError(f"draft.overrides.{player.id}.market: outside league bid bounds")
        by_id[player.id] = player.model_copy(
            update={
                "positions": positions,
                "positions_confirmed": player.positions_confirmed or override.positions is not None,
            }
        )
    for player in by_id.values():
        if not player.positions or len(set(player.positions)) != len(player.positions):
            raise DataError(f"auction.players.{player.id}.positions: empty or duplicate")
        require_members(player.positions, set(league.positions), f"auction.players.{player.id}")
        if (player.fair is None) != (player.utility is None):
            raise DataError(f"auction.players.{player.id}: fair and utility availability disagree")
    return tuple(by_id[k] for k in sorted(by_id))


def validate_labels(players: tuple[AuctionPlayer, ...], state: DraftState) -> None:
    if any(not team.name.strip() for team in state.teams):
        raise DataError("draft.teams: names must not be blank")
    if len({team.name.strip().casefold() for team in state.teams}) != len(state.teams):
        raise DataError("draft.teams: duplicate names")
    unique(state.watch, "draft.watch")
    require_members(state.watch, {p.id for p in players}, "draft.watch")


def validate_draft(
    league: LeagueRules, players: tuple[AuctionPlayer, ...], state: DraftState
) -> tuple[TeamBudget, ...]:
    unique(tuple(t.id for t in state.teams), "draft.teams")
    validate_labels(players, state)
    unique(tuple(s.id for s in state.sales), "draft.sales.id")
    unique(tuple(s.player_id for s in state.sales), "draft.sales.player_id")
    if len(state.teams) != league.teams or state.mine not in {t.id for t in state.teams}:
        raise DataError("draft.teams: team count or mine disagrees with league")
    by_id = {p.id: p for p in players}
    owned: dict[str, tuple[AuctionPlayer, ...]] = {t.id: () for t in state.teams}
    budget = dict.fromkeys(owned, league.budget)
    for sale in state.sales:
        if sale.player_id not in by_id or sale.buyer not in owned:
            raise DataError(f"draft.sales.{sale.id}: unknown player or buyer")
        remaining = capacity(league) - len(owned[sale.buyer])
        maximum = budget[sale.buyer] - (remaining - 1) * league.minimum_bid
        if (
            remaining == 0
            or not league.minimum_bid <= sale.amount <= maximum
            or sale.amount % league.bid_increment
        ):
            raise DataError(f"draft.sales.{sale.id}.amount: exceeds legal bid or roster capacity")
        owned[sale.buyer] += (by_id[sale.player_id],)
        if not completable(league, owned[sale.buyer]):
            raise DataError(f"draft.sales.{sale.id}: cannot complete starter positions")
        budget[sale.buyer] -= sale.amount
    return tuple(
        TeamBudget(
            id=team,
            owned=tuple(sorted(p.id for p in owned[team])),
            budget=budget[team],
            slots=capacity(league) - len(owned[team]),
            maximum_bid=(
                budget[team] - (capacity(league) - len(owned[team]) - 1) * league.minimum_bid
            )
            if len(owned[team]) < capacity(league)
            else 0,
        )
        for team in sorted(owned)
    )
