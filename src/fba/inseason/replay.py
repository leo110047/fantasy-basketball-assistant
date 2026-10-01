"""Paired weekly historical trials with strictly separated decision/outcome inputs."""

from datetime import timedelta

import numpy as np

from fba.contracts.base import DataError
from fba.contracts.inseason import InseasonParameters, WeekForecast
from fba.contracts.inseason_replay import (
    PolicyOutcome,
    PolicyReplayReport,
    PolicyReplayStudy,
    ReplayCase,
    ReplayRow,
)
from fba.contracts.inseason_results import RosterMove
from fba.data.codec import canonical, digest
from fba.formulas.categories import derive_games
from fba.formulas.registry import evaluate
from fba.inseason.matchup import Simulation
from fba.inseason.projection import observed_boxes, visible_games
from fba.inseason.recommendations import (
    admissible_plan,
    candidate_moves,
    changed_simulation,
    earliest_move,
    evaluate_plan,
    search_adds,
)
from fba.inseason.replay_oracle import exhaustive_plans
from fba.inseason.season import season_value


def simulation_for(case: ReplayCase, params: InseasonParameters) -> Simulation:
    week = next((w for w in case.league.matchups if w.id == case.week_id), None)
    if week is None:
        raise DataError("replay.week_id: no configured week")
    sim = Simulation(
        case.league, params, case.players, case.priors, case.ledger, case.snapshot, case.as_of
    )
    if not week.start <= case.as_of.astimezone(sim.zone).date() <= week.end:
        raise DataError("replay.as_of: decision must be inside the selected week")
    if case.players.as_of > case.as_of:
        raise DataError("replay.players.as_of: use a snapshot available at decision time")
    if (
        case.final_players.season_id != case.league.season_id
        or case.final_snapshot.league_id != case.league.league_id
        or case.final_snapshot.as_of.astimezone(sim.zone).date() <= week.end
        or case.final_players.as_of.astimezone(sim.zone).date() <= week.end
    ):
        raise DataError("replay.final: requires matching, completed-week outcome snapshots")
    if any(b.known_at > case.players.as_of for b in case.players.boxes):
        raise DataError("replay.players.boxes: snapshot contains not-yet-published results")
    return sim


def realized(
    case: ReplayCase,
    sim: Simulation,
    forecast: WeekForecast,
    moves: tuple[RosterMove, ...],
    policy: str,
) -> PolicyOutcome:
    # Outcomes are read only after choices and lineups have been frozen.
    own, through = sim.actual(forecast.home, case.week_id)
    total = own[0].copy()
    opponent = next(
        (
            s
            for s in case.final_snapshot.actual
            if s.week_id == case.week_id and s.team_id == forecast.away and s.final
        ),
        None,
    )
    if opponent is None:
        raise DataError("replay.final_snapshot.actual: final opponent Yahoo score is missing")
    # Reuse the live required-stat validation for final scores.
    final_sim = Simulation(
        sim.league,
        sim.params,
        case.final_players,
        sim.priors,
        sim.ledger,
        case.final_snapshot,
        case.final_snapshot.as_of,
    )
    away, _ = final_sim.actual(forecast.away, case.week_id)
    final_games = {g.id: g for g in visible_games(case.final_players, case.final_players.as_of)}
    # visible_games intentionally excludes postponed/cancelled games; these do not count.
    all_final_ids = {g.id for g in case.final_players.games}
    boxes = observed_boxes(case.final_players, case.final_players.as_of)
    for day in forecast.lineups:
        if day.team_id != forecast.home:
            continue
        players = {p.player.id: p.player for p in sim.projection(day.on).players}
        for pid in day.slots.values():
            for game in sim.games:
                if (
                    game.tipoff <= max(through, sim.as_of)
                    or game.tipoff.astimezone(sim.zone).date() != day.on
                    or players[pid].team_id not in (game.home, game.away)
                ):
                    continue
                if game.id not in all_final_ids:
                    raise DataError(f"replay.final_players.games.{game.id}: unresolved outcome")
                final = final_games.get(game.id)
                if final is None or final.tipoff.astimezone(sim.zone).date() != day.on:
                    continue
                if final.status != "completed":
                    raise DataError(f"replay.final_players.games.{game.id}: not completed")
                box = next((b for b in boxes.get(pid, ()) if b.game_id == game.id), None)
                if box is not None:
                    missing = set(sim.league.base_stats) - box.stats.keys()
                    if missing:
                        raise DataError(
                            f"replay.final_players.{pid}: missing stats {sorted(missing)}"
                        )
                    vector = np.array([box.stats[s] for s in sim.league.base_stats])
                    total += derive_games(vector, sim.league.base_stats, sim.league.derived)
    points, score = sim.score(total[None, :], away[:1])
    return PolicyOutcome.model_validate(
        {
            "policy": policy,
            "forecast": forecast,
            "moves": moves,
            "actual_score": float(score[0]),
            "actual_categories": {
                c.id: float(points[0, i]) for i, c in enumerate(sim.league.categories)
            },
        }
    )


def replay_case(
    case: ReplayCase, study: PolicyReplayStudy, params: InseasonParameters
) -> ReplayRow:
    sim = simulation_for(case, params)
    prefs = study.preferences
    sim.untouchable = frozenset(prefs.untouchable)
    mine = sim.snapshot.mine
    team = next(t for t in sim.snapshot.teams if t.id == mine)
    if team.adds_used is None:
        raise DataError("replay.adds_used: required historical add count")
    pair = next(
        p for p in sim.snapshot.pairings if p.week_id == case.week_id and mine in (p.home, p.away)
    )
    rival = pair.away if pair.home == mine else pair.home
    week = next(w for w in sim.league.matchups if w.id == case.week_id)
    before = sim.week(mine, rival, case.week_id)
    plans = search_adds(sim, prefs, case.week_id, lambda _progress: None)
    best = plans[0] if plans and plans[0].score > params.tolerance.value else None
    recommended = best.after if best else before
    keys = tuple(c.id for c in before.categories if c.strategy == "key")
    candidates: list[tuple[float, tuple[RosterMove, ...]]] = []
    remaining = max(0, sim.league.adds_per_week - team.adds_used - prefs.reserve_adds)
    on = max(earliest_move(sim), week.start)
    if remaining and keys:
        while on <= week.end:
            candidates.extend(candidate_moves(sim, prefs, team.players, (), on, week.end, keys))
            on += timedelta(days=1)
    candidates.sort(
        key=lambda row: (
            -round(row[0] / params.tolerance.value),
            tuple((m.add, m.drop, m.effective_on) for m in row[1]),
        )
    )
    retained: bool | None = None
    future = season_value(sim, mine, after=week.end + timedelta(days=1))
    if candidates:
        evaluated = [evaluate_plan(sim, prefs, moves, before, future) for _, moves in candidates]
        eligible = [p for p in evaluated if admissible_plan(p, params.tolerance.value)]
        if eligible:
            maximum = max(round(p.score / params.tolerance.value) for p in eligible)
            retained = any(
                admissible_plan(p, params.tolerance.value)
                and round(p.score / params.tolerance.value) == maximum
                for p in evaluated[: params.shortlist.value]
            )
    plan_count, search_retained = exhaustive_plans(
        sim,
        prefs,
        before,
        future,
        remaining,
        study.oracle_max_plans,
        best.score if best else 0.0,
    )
    ranked: tuple[RosterMove, ...] = ()
    roster: tuple[str, ...] = team.players
    on = max(earliest_move(sim), week.start)
    while len(ranked) < remaining and on <= week.end:
        possible = candidate_moves(sim, prefs, roster, ranked, on, week.end, ())
        players = {p.player.id: p.player for p in sim.projection(on).players}
        if any(
            players[moves[-1].add].public_rank is None
            or players[moves[-1].drop].public_rank is None
            for _, moves in possible
        ):
            raise DataError(
                "replay.public_rank: ranking policy requires a dated rank for every candidate"
            )
        improving = [
            moves
            for _, moves in possible
            if int(players[moves[-1].add].public_rank or 0)
            < int(players[moves[-1].drop].public_rank or 0)
        ]
        if not improving:
            on += timedelta(days=1)
            continue
        ranked = min(
            improving,
            key=lambda moves: (
                players[moves[-1].add].public_rank,
                -int(players[moves[-1].drop].public_rank or 0),
                moves[-1].add,
                moves[-1].drop,
            ),
        )
        roster = changed_simulation(sim, mine, ranked).transitions[mine][-1][1]
    ranking = (
        changed_simulation(sim, mine, ranked).week(mine, rival, case.week_id) if ranked else before
    )
    return ReplayRow(
        league_id=sim.league.league_id,
        season_id=sim.league.season_id,
        week_id=case.week_id,
        as_of=case.as_of,
        input_sha256=digest(canonical(case)),
        full_candidates=len(candidates),
        shortlist_retained_best=retained,
        full_plan_count=plan_count,
        search_retained_best=search_retained,
        outcomes=(
            realized(case, sim, before, (), "unchanged"),
            realized(case, sim, ranking, ranked, "ranking"),
            realized(case, sim, recommended, best.moves if best else (), "recommended"),
        ),
    )


def run_policy_replay(study: PolicyReplayStudy, params: InseasonParameters) -> PolicyReplayReport:
    identities = {(c.league.league_id, c.league.season_id, c.week_id) for c in study.cases}
    if len(identities) != len(study.cases):
        raise DataError(
            "replay.cases: duplicate league/season/week would overweight the same outcome"
        )
    rows = tuple(replay_case(case, study, params) for case in study.cases)
    means = {
        policy: evaluate(
            "mean",
            values=tuple(
                next(o.actual_score for o in row.outcomes if o.policy == policy) for row in rows
            ),
        ).result
        for policy in ("unchanged", "ranking", "recommended")
    }
    measured = [r.shortlist_retained_best for r in rows if r.shortlist_retained_best is not None]
    recall = evaluate("mean", values=tuple(float(v) for v in measured)).result if measured else None
    beam_measured = [r.search_retained_best for r in rows if r.search_retained_best is not None]
    beam_recall = (
        evaluate("mean", values=tuple(float(v) for v in beam_measured)).result
        if beam_measured
        else None
    )
    enough = len(rows) >= study.minimum_cases
    return PolicyReplayReport(
        input_sha256=digest(canonical(study)),
        parameters_sha256=digest(canonical(params)),
        rows=rows,
        mean_scores=means,
        recall=recall,
        beam_recall=beam_recall,
        sufficient_cases=enough,
        recall_passed=enough
        and len(measured) >= study.minimum_cases
        and recall is not None
        and recall >= study.recall_target
        and len(beam_measured) >= study.minimum_cases
        and beam_recall is not None
        and beam_recall >= study.recall_target,
        policy_passed=enough
        and means["recommended"] + params.tolerance.value
        >= max(means["unchanged"], means["ranking"]),
        evidence=study.evidence,
        scope="Paired weekly trials from recorded starting rosters; "
        "outcomes never enter decisions. "
        "Recall covers the one-add shortlist and the complete legal multi-add search space "
        "within oracle_max_plans. This is not a persistent-roster season simulation "
        "and does not measure real opponent responses.",
    )
