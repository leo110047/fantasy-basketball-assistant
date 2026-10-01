from math import fsum
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from fba.contracts.backtest import CategoryOutcome, Pairing, ReplayTeam, Standing, WeekOutcome
from fba.contracts.base import DataError
from fba.contracts.config import LeagueRules
from fba.formulas.categories import category_values
from fba.formulas.simulation import standings_credit
from fba.formulas.vector import category_points

type FloatArray = NDArray[np.float64]


class MatchupScorer(Protocol):
    def __call__(self, pairing: Pairing, /) -> WeekOutcome: ...


def categories(box: FloatArray, league: LeagueRules, stat_ids: tuple[str, ...]) -> FloatArray:
    return category_values(box, league.categories, stat_ids)


def matchup(
    league: LeagueRules,
    axes: tuple[str, ...],
    pairing: Pairing,
    home: tuple[float, ...],
    away: tuple[float, ...],
) -> WeekOutcome:
    if pairing.away is None:
        return WeekOutcome(
            week_id=pairing.week_id,
            home=pairing.home,
            away=None,
            categories=(),
            home_points=1.0,
            away_points=0.0,
            winner=pairing.home,
        )
    outcomes: list[CategoryOutcome] = []
    hp, ap = 0.0, 0.0
    values = categories(np.array((home, away)), league, axes)
    for i, c in enumerate(league.categories):
        a, b = (round(float(v), c.comparison_decimals) for v in values[:, i])
        delta = a - b
        direction = 1 if c.direction == "higher" else -1
        a, b = a * direction, b * direction
        winner = "home" if delta > 0 else "away" if delta < 0 else "tie"
        outcomes.append(CategoryOutcome(id=c.id, home=a, away=b, winner=winner))
        tie = c.tie_value if league.scoring.category_ties == "use_tie_value" else 0.0
        hp += float(category_points({"differences": np.asarray(delta), "ties": np.asarray(tie)}))
        ap += float(category_points({"differences": np.asarray(-delta), "ties": np.asarray(tie)}))
    return WeekOutcome(
        week_id=pairing.week_id,
        home=pairing.home,
        away=pairing.away,
        categories=tuple(outcomes),
        home_points=hp,
        away_points=ap,
        winner=pairing.home if hp > ap else pairing.away if ap > hp else None,
    )


def standings(
    league: LeagueRules, teams: tuple[ReplayTeam, ...], outcomes: tuple[WeekOutcome, ...]
) -> tuple[Standing, ...]:
    rows: list[Standing] = []
    for team in teams:
        # A bye advances a playoff bracket but is not a played regular-season matchup.
        games = tuple(o for o in outcomes if o.away is not None and team.id in (o.home, o.away))
        wins = float(sum(o.winner == team.id for o in games))
        ties = sum(o.winner is None for o in games)
        losses = len(games) - int(wins) - ties
        if league.scoring.week_tie == "half_win":
            wins = float(
                standings_credit(
                    {
                        "wins": np.asarray(wins),
                        "ties": np.asarray(ties, dtype=float),
                        "tie_value": np.asarray(0.5),
                    }
                )
            )
        if league.scoring.week_tie == "loss":
            losses += ties
            ties = 0
        rows.append(
            Standing(
                team_id=team.id,
                wins=float(wins),
                losses=losses,
                ties=ties,
                category_points=fsum(
                    o.home_points if o.home == team.id else o.away_points for o in games
                ),
                seed=team.seed,
            )
        )
    # A multi-team tie uses the mini-league among the remaining tied teams.
    groups = [rows]
    for rule in league.playoffs.seeding:
        next_groups: list[list[Standing]] = []
        for group in groups:
            tied = {r.team_id for r in group}
            scores: dict[float, list[Standing]] = {}
            for row in group:
                if rule == "record":
                    score = float(
                        standings_credit(
                            {
                                "wins": np.asarray(row.wins),
                                "ties": np.asarray(row.ties, dtype=float),
                                "tie_value": np.asarray(
                                    0.5 if league.scoring.week_tie == "tie" else 0.0
                                ),
                            }
                        )
                    )
                elif rule == "head_to_head":
                    score = fsum(
                        1.0
                        if o.winner == row.team_id
                        else 0.5
                        if o.winner is None and league.scoring.week_tie != "loss"
                        else 0.0
                        for o in outcomes
                        if row.team_id in (o.home, o.away) and o.home in tied and o.away in tied
                    )
                else:
                    score = row.category_points if rule == "category_record" else -row.seed
                scores.setdefault(score, []).append(row)
            next_groups.extend(scores[k] for k in sorted(scores, reverse=True))
        groups = next_groups
    return tuple(r for group in groups for r in sorted(group, key=lambda r: (r.seed, r.team_id)))


def bracket_order(size: int) -> tuple[int, ...]:
    order = (0,)
    while len(order) < size:
        count = len(order) * 2
        order = tuple(p for seed in order for p in (seed, count - 1 - seed))
    return order


def score_season(
    league: LeagueRules,
    axes: tuple[str, ...],
    teams: tuple[ReplayTeam, ...],
    pairings: tuple[Pairing, ...],
    boxes: tuple[tuple[tuple[float, ...], ...], ...],
    week_ids: tuple[str, ...],
) -> tuple[tuple[WeekOutcome, ...], tuple[Standing, ...], str]:
    by_team = {t.id: i for i, t in enumerate(teams)}

    def play(p: Pairing) -> WeekOutcome:
        w = week_ids.index(p.week_id)
        return matchup(
            league,
            axes,
            p,
            boxes[by_team[p.home]][w],
            boxes[by_team[p.away]][w] if p.away is not None else (),
        )

    outcomes = [
        play(p) for p in sorted(pairings, key=lambda p: (week_ids.index(p.week_id), p.home))
    ]
    regular = {w.id for w in league.matchups if w.phase == "regular"}
    table = standings(league, teams, tuple(o for o in outcomes if o.week_id in regular))
    seeds = {r.team_id: i for i, r in enumerate(table)}
    entrants = tuple(r.team_id for r in table[: league.playoffs.team_count])
    size = league.playoffs.team_count + league.playoffs.byes
    bracket: list[str | None] = [
        entrants[i] if i < len(entrants) else None for i in bracket_order(size)
    ]
    for week_id in league.playoffs.week_ids:
        bracket = playoff_round(league, week_id, bracket, play, seeds, table, outcomes)
    if len(bracket) != 1 or bracket[0] is None:
        raise DataError("replay.playoffs: incomplete bracket")
    ordered = tuple(sorted(outcomes, key=lambda o: (week_ids.index(o.week_id), o.home)))
    return ordered, table, bracket[0]


def playoff_round(
    league: LeagueRules,
    week_id: str,
    bracket: list[str | None],
    play: MatchupScorer,
    seeds: dict[str, int],
    table: tuple[Standing, ...],
    outcomes: list[WeekOutcome],
) -> list[str | None]:
    active = list(bracket)
    if league.playoffs.reseed and week_id != league.playoffs.week_ids[0]:
        ordered = sorted((t for t in active if t is not None), key=lambda t: seeds[t])
        active = [
            t
            for pair in zip(
                ordered[: len(ordered) // 2], reversed(ordered[len(ordered) // 2 :]), strict=True
            )
            for t in pair
        ]
    points = {r.team_id: r.category_points for r in table}
    winners: list[str | None] = []
    for i in range(0, len(active), 2):
        a, b = active[i : i + 2]
        if a is None:
            a, b = b, a
        if a is None:
            raise DataError("replay.playoffs: paired empty bracket slots")
        result = play(Pairing(week_id=week_id, home=a, away=b))
        if result.winner is None:
            if b is None:
                raise DataError("replay.playoffs: bye must have a winner")
            winner = min(
                (a, b),
                key=lambda t: (
                    -points[t] if league.playoffs.matchup_tie == "category_record" else 0,
                    seeds[t],
                ),
            )
            result = result.model_copy(update={"winner": winner})
        outcomes.append(result)
        winners.append(result.winner)
    return winners
