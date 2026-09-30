from datetime import date, datetime
from time import monotonic
from zoneinfo import ZoneInfo

import numpy as np

from fba.contracts.base import DataError
from fba.contracts.formula import FormulaTrace
from fba.contracts.inseason import DayLineup
from fba.contracts.inseason_results import (
    AddPlan,
    DropAssessment,
    TodayAction,
    TodayPlayer,
    TodayResult,
)
from fba.core.lineups import legal_assignment
from fba.formulas.registry import evaluate
from fba.inseason.matchup import Simulation
from fba.inseason.recommendations import legal_roster
from fba.inseason.season import season_value


def today(
    sim: Simulation,
    on: date,
    timezone: str,
    plan: AddPlan | None = None,
    completed: tuple[str, ...] = (),
) -> TodayResult:
    week = next((w for w in sim.league.matchups if w.start <= on <= w.end), None)
    if week is None:
        raise DataError("today.date: no fantasy matchup week")
    team = next(t for t in sim.snapshot.teams if t.id == sim.snapshot.mine)
    pairing = next(
        (p for p in sim.snapshot.pairings if p.week_id == week.id and team.id in (p.home, p.away)),
        None,
    )
    if pairing is None:
        raise DataError("today.opponent: actual scheduled opponent missing")
    opponent = pairing.away if pairing.home == team.id else pairing.home
    lineup, before, after = sim.optimize_day(team.id, week.id, on, opponent)
    players = {p.player.id: p for p in sim.projection(on).players}
    actions: list[TodayAction] = []
    locks: dict[str, datetime] = {}
    for pid in (*team.players, *team.injury_players):
        games = tuple(
            g
            for g in sim.games
            if players[pid].player.team_id in (g.home, g.away)
            and g.tipoff.astimezone(sim.zone).date() == on
        )
        if games:
            lock = (
                min(g.tipoff for g in games)
                if sim.league.lineup_lock == "player_game"
                else datetime.combine(on, sim.league.lineup_lock_time, sim.zone)
            )
            locks[pid] = lock.astimezone(ZoneInfo(timezone))
        slot = next((s for s, p in lineup.slots.items() if p == pid), None)
        if pid in team.players:
            identifier = f"{on}:lineup:{pid}"
            actions.append(
                TodayAction(
                    id=identifier,
                    kind="start" if slot else "bench",
                    player_id=pid,
                    slot=slot,
                    reason="依合法位置最大化本週模擬結果；同分優先較多出賽"
                    if slot
                    else "本週類別風險或位置限制；本日先放板凳",
                    completed=identifier in completed,
                )
            )
    details = today_players(sim, lineup, opponent, week.id, timezone)
    reasons = {p.player_id: p.reason for p in details}
    actions = [a.model_copy(update={"reason": reasons[a.player_id]}) for a in actions]
    assessment = drop_assessment(sim, on)
    actions = [
        a.model_copy(
            update={"kind": "locked", "reason": "已超過鎖定時間，依 Yahoo 已鎖定位置；本日無法調整"}
        )
        if a.player_id in locks and locks[a.player_id] <= sim.as_of
        else a
        for a in actions
    ]
    actions.extend(injury_actions(sim, on, completed, assessment))
    if plan is not None:
        for move in plan.moves:
            if move.effective_on == on:
                identifier = f"{plan.id}:{move.add}:{move.drop}"
                actions.append(
                    TodayAction(
                        id=identifier,
                        kind="add_drop",
                        player_id=move.add,
                        slot=None,
                        reason=f"F3 計畫：加入 {move.add}，釋出 {move.drop}；請在 Yahoo 手動操作",
                        completed=identifier in completed,
                    )
                )
    return TodayResult(
        on=on,
        lineup=lineup,
        actions=tuple(actions),
        locks=locks,
        score_before=before,
        score_after=after,
        plan_id=plan.id if plan else None,
        drop_assessment=assessment,
        players=details,
        traces=(
            sim.calibrated_score(
                float(
                    sim.score(sim.total(team.id, week.id)[0], sim.total(opponent, week.id)[0])[
                        1
                    ].mean()
                )
            ),
            sim.week(team.id, opponent, week.id).traces[0],
        ),
    )


def injury_actions(
    sim: Simulation, on: date, completed: tuple[str, ...], assessment: DropAssessment | None = None
) -> tuple[TodayAction, ...]:
    team = next(t for t in sim.snapshot.teams if t.id == sim.snapshot.mine)
    players = {p.player.id: p for p in sim.projection(on).players}
    occupied = {
        s.id: sum(v == s.id for v in team.injury_players.values()) for s in sim.league.injury_slots
    }
    result: list[TodayAction] = []
    for pid, slot_id in sorted(team.injury_players.items()):
        slot = next(s for s in sim.league.injury_slots if s.id == slot_id)
        if players[pid].player.status not in slot.eligible_statuses:
            identifier = f"{on}:injury_out:{pid}"
            result.append(
                TodayAction(
                    id=identifier,
                    kind="injury_out",
                    player_id=pid,
                    slot=slot_id,
                    reason=(
                        "狀態已不符合傷兵格資格，請移出。"
                        + (
                            f"名單額滿時可釋出 {assessment.drop}。"
                            if assessment
                            and len(team.players)
                            >= len(sim.league.starter_slots) + sim.league.bench_slots
                            else "目前有空名額。"
                        )
                    ),
                    completed=identifier in completed,
                )
            )
    # More restrictive slots are filled first; no ineligible IL recommendation.
    for pid in sorted(team.players):
        for slot in sorted(sim.league.injury_slots, key=lambda s: (len(s.eligible_statuses), s.id)):
            if (
                occupied[slot.id] < slot.count
                and players[pid].player.status in slot.eligible_statuses
            ):
                identifier = f"{on}:injury_in:{pid}"
                result.append(
                    TodayAction(
                        id=identifier,
                        kind="injury_in",
                        player_id=pid,
                        slot=slot.id,
                        reason="目前狀態符合已確認的傷兵格規則，可移入騰出名額",
                        completed=identifier in completed,
                    )
                )
                occupied[slot.id] += 1
                break
    return tuple(result)


def drop_assessment(sim: Simulation, on: date) -> DropAssessment | None:
    team = sim.snapshot.mine
    roster = sim.roster(team)
    if not roster:
        return None
    base = season_value(sim, team)
    candidates = [
        (season_value(sim, team, {team: tuple(p for p in roster if p != drop)}), drop)
        for drop in roster
        if legal_roster(sim, tuple(p for p in roster if p != drop), on)
    ]
    if not candidates:
        return None
    value, drop = min(candidates, key=lambda r: (-round(r[0] / sim.params.tolerance.value), r[1]))
    available = [
        (
            season_value(
                sim, team, {team: tuple(p for p in roster if p != drop) + (free.player_id,)}
            ),
            free.player_id,
        )
        for free in sim.snapshot.free_agents
        if free.status == "free"
        and legal_roster(sim, tuple(p for p in roster if p != drop) + (free.player_id,), on)
    ]
    replacement = (
        min(available, key=lambda r: (-round(r[0] / sim.params.tolerance.value), r[1]))
        if available
        else None
    )
    player = next(p.player for p in sim.projection(on).players if p.player.id == drop)
    lost = evaluate("difference", after=base, before=value)
    gain = evaluate("difference", after=replacement[0], before=base) if replacement else None
    return DropAssessment(
        drop=drop,
        add=replacement[1] if replacement else None,
        remaining_value_lost=lost.result,
        replacement_gain=gain.result if gain else None,
        ownership=player.ownership,
        ownership_change=player.ownership_change,
        traces=(lost, *((gain,) if gain else ())),
    )


def lineup_effects(
    sim: Simulation, lineup: DayLineup, opponent: str, week: str
) -> dict[str, tuple[FormulaTrace, dict[str, float]]]:
    own, other, _ = sim.matchup_totals(lineup.team_id, opponent, week)
    _, through = sim.actual(lineup.team_id, week)
    draws = sim.daily_draws(sim.roster(lineup.team_id), lineup.on, max(sim.as_of, through))
    selected = tuple(lineup.slots.values())
    rest = own - sum((draws[p] for p in selected), start=np.zeros_like(own))
    categories, score = sim.score(own, other)
    selected_score = sim.calibrated_score(float(score.mean())).result
    team = next(t for t in sim.snapshot.teams if t.id == lineup.team_id)
    locked_slots = {slot for slot, pid in team.selected_slots.items() if sim.locked(pid, lineup.on)}
    movable_slots = tuple(s for s in sim.league.starter_slots if s.id not in locked_slots)
    positions = {p.player.id: p.player.positions for p in sim.projection(lineup.on).players}
    results: dict[str, tuple[FormulaTrace, dict[str, float]]] = {}
    started = monotonic()
    for pid in draws:
        if sim.locked(pid, lineup.on):
            continue
        if pid not in selected and legal_assignment(movable_slots, positions, (pid,)) is None:
            continue
        # Compare the chosen lineup with the best legal lineup that reverses
        # this player's decision, holding other dates and the same draws fixed.
        alternative_slots, alternative = sim.optimize_assignment(
            lineup.team_id,
            lineup.on,
            draws,
            rest,
            other,
            started,
            required=() if pid in selected else (pid,),
            excluded=(pid,) if pid in selected else (),
        )
        altered = rest + sum(
            (draws[p] for p in alternative_slots.values()), start=np.zeros_like(rest)
        )
        changed_categories, _ = sim.score(altered, other)
        results[pid] = (
            evaluate("difference", after=selected_score, before=alternative),
            {
                c.label or c.id: evaluate(
                    "product",
                    gain=float(categories[:, i].mean() - changed_categories[:, i].mean()),
                    probability=sim.params.calibration.value,
                ).result
                for i, c in enumerate(sim.league.categories)
            },
        )
    return results


def today_players(
    sim: Simulation, lineup: DayLineup, opponent: str, week: str, timezone: str
) -> tuple[TodayPlayer, ...]:
    effects = lineup_effects(sim, lineup, opponent, week)
    team = next(t for t in sim.snapshot.teams if t.id == lineup.team_id)
    projections = {p.player.id: p for p in sim.projection(lineup.on).players}
    rows: list[TodayPlayer] = []
    for pid in (*team.players, *team.injury_players):
        player = projections[pid].player
        games = tuple(
            g
            for g in sim.games
            if player.team_id in (g.home, g.away)
            and g.tipoff.astimezone(sim.zone).date() == lineup.on
        )
        slot = next((s for s, p in lineup.slots.items() if p == pid), team.injury_players.get(pid))
        effect = effects.get(pid)
        reason = (
            "今日沒有待開賽場次"
            if not games
            else "已鎖定，維持 Yahoo 已登錄位置"
            if sim.locked(pid, lineup.on)
            else "目前可調整的先發格沒有符合此球員的位置"
        )
        if pid in team.injury_players:
            reason = "目前在傷兵格；依最新狀態核對移出提醒"
        elif effect is not None:
            trace, changes = effect
            helpful = [
                label for label, delta in changes.items() if delta > sim.params.tolerance.value
            ]
            reason = (
                "先發" if pid in lineup.slots.values() else "板凳"
            ) + "較相反安排的最佳合法陣容"
            reason += f"增加本週分數 {trace.result:.3f}"
            reason += (
                "；有利類別：" + "、".join(helpful) if helpful else "；同分優先出賽場次與球員 ID"
            )
        rows.append(
            TodayPlayer(
                player_id=pid,
                opponents=tuple(g.away if g.home == player.team_id else g.home for g in games),
                tipoffs=tuple(g.tipoff.astimezone(ZoneInfo(timezone)) for g in games),
                status=player.status,
                slot=slot,
                reason=reason,
                marginal=effect[0] if effect else None,
                category_changes=effect[1] if effect else {},
            )
        )
    return tuple(rows)
