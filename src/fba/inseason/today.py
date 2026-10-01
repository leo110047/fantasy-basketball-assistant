from datetime import date, datetime
from time import monotonic
from zoneinfo import ZoneInfo

import numpy as np

from fba.contracts.base import DataError
from fba.contracts.formula import FormulaTrace
from fba.contracts.inseason import DayLineup, InjuryReturn
from fba.contracts.inseason_results import (
    AddPlan,
    TodayAction,
    TodayPlayer,
    TodayResult,
)
from fba.core.lineups import legal_assignment
from fba.formulas.registry import evaluate
from fba.formulas.simulation import mean_array
from fba.inseason.injury_returns import cache_saved_returns, reuse_return_plans
from fba.inseason.matchup import Simulation
from fba.inseason.recommendations import admissible_plan, changed_simulation, earliest_move


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
    if plan is not None and plan.before.week_id != week.id:
        raise DataError("today.plan_id: F3 計畫所屬對戰週與選擇日期不同")
    if plan is not None and any(
        m.effective_on < earliest_move(sim) or sim.locked(m.drop, m.effective_on)
        for m in plan.moves
    ):
        raise DataError("today.plan_id: 計畫已超過生效或球員鎖定時間，請重新計算 F3")
    team = next(t for t in sim.snapshot.teams if t.id == sim.snapshot.mine)
    pairing = next(
        (p for p in sim.snapshot.pairings if p.week_id == week.id and team.id in (p.home, p.away)),
        None,
    )
    if pairing is None:
        raise DataError("today.opponent: actual scheduled opponent missing")
    opponent = pairing.away if pairing.home == team.id else pairing.home
    saved = (
        {team.id: plan.before.injury_returns} if plan is not None and team.injury_players else None
    )
    sim, pending = reuse_return_plans(sim, (team.id, opponent), saved)
    forecast = sim.week(team.id, opponent, week.id)
    if plan is not None and plan.moves:
        current_policy = plan.model_copy(update={"priority": forecast.priority})
        if not admissible_plan(current_policy, sim.params.tolerance.value):
            raise DataError("today.plan_id: 計畫不符合目前整季保護規則，請重新計算 F3")
        child = changed_simulation(sim, team.id, plan.moves)
        child.injury_plan_cache = sim.injury_plan_cache.copy()
        if team.injury_players:
            cache_saved_returns(child, team.id, plan.after.injury_returns)
        sim = child
        forecast = sim.week(team.id, opponent, week.id).model_copy(
            update={
                "no_moves_raw_score": forecast.raw_score,
                "no_moves_score": forecast.score,
            }
        )
    lineup, before, after = sim.optimize_day(team.id, week.id, on, opponent)
    players = {p.player.id: p for p in sim.projection(on).players}
    active = roster_on(sim, team.id, on)
    returns = sim.injury_plan_cache.get((team.id, tuple(sorted(team.players))), ())
    drops = {move.drop for move in returns if move.drop is not None and move.effective_on <= on}
    actions: list[TodayAction] = []
    locks: dict[str, datetime] = {}
    for pid in tuple(dict.fromkeys((*active, *sorted(drops), *team.injury_players))):
        games = sim.games_on(players[pid].player.team_id, on)
        if games:
            lock = (
                min(g.tipoff for g in games)
                if sim.league.lineup_lock == "player_game"
                else datetime.combine(on, sim.league.lineup_lock_time, sim.zone)
            )
            locks[pid] = lock.astimezone(ZoneInfo(timezone))
        slot = next((s for s, p in lineup.slots.items() if p == pid), None)
        if pid in drops:
            identifier = f"{on}:injury_drop:{pid}"
            actions.append(
                TodayAction(
                    id=identifier,
                    kind="drop",
                    player_id=pid,
                    slot=None,
                    reason=(
                        "依 IL 回歸情境釋出，讓回歸球員符合名單容量；請先核對並在 Yahoo 手動操作"
                    ),
                    completed=identifier in completed,
                )
            )
        if pid in active:
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
    actions = [
        a.model_copy(update={"reason": reasons[a.player_id]}) if a.kind != "drop" else a
        for a in actions
    ]
    actions = [
        a.model_copy(
            update={"kind": "locked", "reason": "已超過鎖定時間，依 Yahoo 已鎖定位置；本日無法調整"}
        )
        if a.player_id in locks and locks[a.player_id] <= sim.as_of
        else a
        for a in actions
    ]
    actions.extend(injury_actions(sim, on, completed, pending))
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
                    ),
                )
    actions = ordered_roster_actions(actions, returns, on)
    return TodayResult(
        on=on,
        lineup=lineup,
        actions=tuple(actions),
        locks=locks,
        score_before=before,
        score_after=after,
        plan_id=plan.id if plan else None,
        recommendation=plan,
        injury_pending=pending,
        week_forecast=forecast,
        priority=forecast.priority,
        players=details,
        traces=(
            sim.calibrated_score(
                float(
                    mean_array(
                        sim.score(sim.total(team.id, week.id)[0], sim.total(opponent, week.id)[0])[
                            1
                        ]
                    )
                )
            ),
            sim.week(team.id, opponent, week.id).traces[0],
        ),
    )


def roster_on(sim: Simulation, team: str, on: date) -> tuple[str, ...]:
    return sim.projected_roster(team, on, sim.roster(team))


def ordered_roster_actions(
    actions: list[TodayAction], returns: tuple[InjuryReturn, ...], on: date
) -> list[TodayAction]:
    identifiers: list[str] = []
    for move in returns:
        if move.effective_on <= on:
            if move.drop is not None:
                identifiers.append(f"{on}:injury_drop:{move.drop}")
            identifiers.append(f"{on}:injury_out:{move.player_id}")
    by_id = {a.id: a for a in actions}
    ordered = [by_id[key] for key in identifiers if key in by_id]
    remaining = [a for a in actions if a.id not in identifiers]
    return [
        *ordered,
        *(a for a in remaining if a.kind == "add_drop"),
        *(a for a in remaining if a.kind != "add_drop"),
    ]


def injury_actions(
    sim: Simulation, on: date, completed: tuple[str, ...], pending: tuple[str, ...] = ()
) -> tuple[TodayAction, ...]:
    team = next(t for t in sim.snapshot.teams if t.id == sim.snapshot.mine)
    players = {p.player.id: p for p in sim.projection(on).players}
    returns = {
        m.player_id: m
        for m in sim.injury_plan_cache.get((team.id, tuple(sorted(team.players))), ())
    }
    activating_ids = {pid for pid, move in returns.items() if move.effective_on <= on}
    occupied = {
        s.id: sum(v == s.id and pid not in activating_ids for pid, v in team.injury_players.items())
        for s in sim.league.injury_slots
    }
    result: list[TodayAction] = []
    for pid, slot_id in sorted(team.injury_players.items()):
        slot = next(s for s in sim.league.injury_slots if s.id == slot_id)
        planned = returns.get(pid)
        activating = planned is not None and planned.effective_on <= on
        if players[pid].player.status not in slot.eligible_statuses or activating:
            identifier = f"{on}:injury_out:{pid}"
            result.append(
                TodayAction(
                    id=identifier,
                    kind="injury_out",
                    player_id=pid,
                    slot=slot_id,
                    reason=(
                        (
                            "依預估回歸情境，請於回歸後移出傷兵格；尚需確認實際狀態。"
                            if activating and planned is not None and planned.estimated
                            else "狀態已不符合傷兵格資格，請移出。"
                        )
                        + (
                            f"回歸情境需先釋出 {returns[pid].drop}。"
                            if pid in returns
                            and returns[pid].drop is not None
                            and len(team.players)
                            >= len(sim.league.starter_slots) + sim.league.bench_slots
                            else "名單已滿，需先到本週頁計算可沿用的必要釋出計畫。"
                            if pid in pending
                            else "目前有空名額。"
                        )
                    ),
                    completed=identifier in completed,
                )
            )
    # More restrictive slots are filled first; no ineligible IL recommendation.
    for pid in roster_on(sim, team.id, on):
        if pid in activating_ids:
            continue
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


def lineup_effects(
    sim: Simulation, lineup: DayLineup, opponent: str, week: str
) -> dict[str, tuple[FormulaTrace, dict[str, tuple[FormulaTrace, ...]]]]:
    own, other, _ = sim.matchup_totals(lineup.team_id, opponent, week)
    _, through = sim.actual(lineup.team_id, week)
    roster = roster_on(sim, lineup.team_id, lineup.on)
    draws = sim.daily_draws(roster, lineup.on, max(sim.as_of, through))
    selected = tuple(lineup.slots.values())
    rest = own - sum((draws[p] for p in selected), start=np.zeros_like(own))
    categories, score = sim.score(own, other)
    selected_score = sim.calibrated_score(float(mean_array(score))).result
    team = next(t for t in sim.snapshot.teams if t.id == lineup.team_id)
    locked_slots = {slot for slot, pid in team.selected_slots.items() if sim.locked(pid, lineup.on)}
    movable_slots = tuple(s for s in sim.league.starter_slots if s.id not in locked_slots)
    positions = {p.player.id: p.player.positions for p in sim.projection(lineup.on).players}
    results: dict[str, tuple[FormulaTrace, dict[str, tuple[FormulaTrace, ...]]]] = {}
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
        changes: dict[str, tuple[FormulaTrace, ...]] = {}
        for i, category in enumerate(sim.league.categories):
            delta = evaluate(
                "difference",
                after=float(mean_array(categories[:, i])),
                before=float(mean_array(changed_categories[:, i])),
            )
            scaled = evaluate(
                "product", gain=delta.result, probability=sim.params.calibration.value
            )
            changes[category.label or category.id] = (delta, scaled)
        results[pid] = (
            evaluate("difference", after=selected_score, before=alternative),
            changes,
        )
    return results


def today_players(
    sim: Simulation, lineup: DayLineup, opponent: str, week: str, timezone: str
) -> tuple[TodayPlayer, ...]:
    effects = lineup_effects(sim, lineup, opponent, week)
    team = next(t for t in sim.snapshot.teams if t.id == lineup.team_id)
    projections = {p.player.id: p for p in sim.projection(lineup.on).players}
    active = roster_on(sim, team.id, lineup.on)
    rows: list[TodayPlayer] = []
    returns = sim.injury_plan_cache.get((team.id, tuple(sorted(team.players))), ())
    dropped = tuple(m.drop for m in returns if m.drop is not None and m.effective_on <= lineup.on)
    for pid in tuple(dict.fromkeys((*active, *dropped, *team.injury_players))):
        player = projections[pid].player
        games = sim.games_on(player.team_id, lineup.on)
        slot = next(
            (s for s, p in lineup.slots.items() if p == pid),
            team.injury_players.get(pid) if pid not in active else None,
        )
        effect = effects.get(pid)
        reason = (
            "今日沒有待開賽場次"
            if not games
            else "已鎖定，維持 Yahoo 已登錄位置"
            if sim.locked(pid, lineup.on)
            else "目前可調整的先發格沒有符合此球員的位置"
        )
        if pid in team.injury_players and pid not in active:
            reason = "目前在傷兵格；依最新狀態核對移出提醒"
        elif pid not in active:
            reason = "依 IL 回歸情境釋出；需核對名單容量與手動操作"
        elif effect is not None:
            trace, changes = effect
            helpful = [
                label
                for label, traces in changes.items()
                if traces[-1].result > sim.params.tolerance.value
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
                category_changes={label: traces[-1].result for label, traces in effect[1].items()}
                if effect
                else {},
                category_traces=effect[1] if effect else {},
            )
        )
    return tuple(rows)
