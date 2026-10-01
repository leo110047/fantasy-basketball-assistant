"""Only certified elimination pressure permits sacrificing future strength."""

from time import monotonic
from typing import TYPE_CHECKING

from fba.contracts.inseason import MatchupPriority
from fba.core.qualification import qualification_possible

if TYPE_CHECKING:
    from fba.inseason.matchup import Simulation


def matchup_priority(sim: "Simulation", week_id: str) -> MatchupPriority:
    if week_id in sim.priority_cache:
        return sim.priority_cache[week_id]
    result = classify_matchup(sim, week_id)
    sim.priority_cache[week_id] = result
    return result


def classify_matchup(sim: "Simulation", week_id: str) -> MatchupPriority:
    week = next(w for w in sim.league.matchups if w.id == week_id)
    mine = sim.snapshot.mine
    selected = next(
        (p for p in sim.snapshot.pairings if p.week_id == week_id and mine in (p.home, p.away)),
        None,
    )
    if selected is None:
        return MatchupPriority(status="unknown", reason="缺少已確認的本週對手；保留整季強度。")
    if week.phase == "playoff":
        if selected.elimination is None:
            return MatchupPriority(
                status="unknown", reason="尚未確認此對戰是否為淘汰賽或安慰賽；保留整季強度。"
            )
        return MatchupPriority(
            status="must_win" if selected.elimination else "normal",
            reason="已確認淘汰賽：輸掉即淘汰。"
            if selected.elimination
            else "已確認此對戰不會因輸球淘汰；保留整季強度。",
        )
    today = sim.as_of.astimezone(sim.zone).date()
    if sim.league.playoff_seeding != "overall":
        return MatchupPriority(
            status="unknown", reason="尚未確認季後賽名額完全依總排名決定；保留整季強度。"
        )
    weeks = tuple(w.id for w in sim.league.matchups if w.phase == "regular" and w.end >= today)
    teams = tuple(sorted(sim.snapshot.teams, key=lambda t: (t.seed, t.id)))
    played = tuple(t.wins + t.losses + t.ties for t in teams)
    if max(played) - min(played) > sim.params.tolerance.value:
        return MatchupPriority(
            status="unknown", reason="各隊已入帳場數不同，無法用總積分證明晉級結果；保留整季強度。"
        )
    ids = {t.id: i for i, t in enumerate(teams)}
    pairings = tuple(p for p in sim.snapshot.pairings if p.week_id in weeks)
    expected = {(w, t.id) for w in weeks for t in teams}
    covered = [(p.week_id, t) for p in pairings for t in (p.home, p.away)]
    if set(covered) != expected or len(covered) != len(expected) or selected not in pairings:
        return MatchupPriority(
            status="unknown",
            reason="剩餘對戰不完整，無法證明輸球即無緣季後賽；保留整季強度。",
        )
    if any(s.final and s.week_id in weeks for s in sim.snapshot.actual):
        return MatchupPriority(
            status="unknown", reason="剩餘週已有完賽比分，需先確認排名已入帳的週次；保留整季強度。"
        )
    sim.check_limits()
    deadline = min(
        monotonic() + sim.params.budgets["week"].value,
        sim.deadline[0] if sim.deadline else float("inf"),
    )
    points = tuple(t.wins + sim.league.week_tie_value * t.ties for t in teams)
    ties = (
        (sim.league.week_tie_value,)
        if sim.league.scoring == "h2h_one_win"
        else tuple(
            c.tie_value if sim.league.category_ties == "use_tie_value" else 0.0
            for c in sim.league.categories
        )
    )

    def possible(win: bool, favorable_ties: bool) -> bool | None:
        # Prove elimination even with every final standings tie in our favor.
        # A winning route is certified with every tie against us. This avoids
        # assuming that today's seed is Yahoo's final regular-season tiebreak.
        order = (
            tuple(0 if t.id == mine else i + 1 for i, t in enumerate(teams))
            if favorable_ties
            else tuple(len(teams) if t.id == mine else i for i, t in enumerate(teams))
        )
        return qualification_possible(
            points,
            order,
            ids[mine],
            sim.league.playoff_teams,
            tuple((ids[p.home], ids[p.away]) for p in pairings),
            pairings.index(selected),
            ties,
            win,
            max(0.0, deadline - monotonic()),
            sim.params.tolerance.value,
        )

    losing = possible(False, True)
    if losing is True:
        return MatchupPriority(
            status="normal",
            qualification_if_loss=True,
            reason="尚無法證明輸這週必定無緣季後賽；保留整季強度。",
        )
    winning = possible(True, False) if losing is False else None
    if losing is False and winning is True:
        return MatchupPriority(
            status="must_win",
            qualification_if_loss=False,
            qualification_if_win=True,
            reason="即使同分判定全有利於我方，輸這週仍無法晉級；贏球則有不依賴同分的晉級路徑。",
        )
    winning_with_ties = possible(True, True) if losing is False and winning is False else winning
    if losing is False and winning_with_ties is False:
        return MatchupPriority(
            status="eliminated",
            qualification_if_loss=False,
            qualification_if_win=False,
            reason="即使贏這週且同分判定有利，也無法晉級；不為本週犧牲整季強度。",
        )
    return MatchupPriority(
        status="unknown",
        qualification_if_loss=losing,
        qualification_if_win=winning,
        reason="未能完成可驗證的晉級可行性判定；保留整季強度。",
    )
