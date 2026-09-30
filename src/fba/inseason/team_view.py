from datetime import timedelta
from zoneinfo import ZoneInfo

from fba.contracts.inseason import (
    AdjustmentLedger,
    EffectiveProjection,
    InseasonLeague,
    InseasonParameters,
    ProjectionRules,
    SeasonGame,
)
from fba.contracts.inseason_results import TeamPlayerView, TeamProjectionView
from fba.formulas.registry import evaluate
from fba.inseason.adjustments import active_entries


def team_views(
    projection: EffectiveProjection,
    rules: ProjectionRules,
    params: InseasonParameters,
    games: tuple[SeasonGame, ...],
    league: InseasonLeague | None,
    ledger: AdjustmentLedger,
) -> tuple[TeamProjectionView, ...]:
    zone = ZoneInfo(rules.timezone)
    weeks = tuple(sorted(league.matchups, key=lambda w: w.start)) if league else ()
    current = next((w for w in weeks if w.start <= projection.on <= w.end), None)
    following = next((w for w in weeks if current and w.start > current.end), None)
    teams = sorted(
        {p.player.team_id for p in projection.players}
        | {t for g in games for t in (g.home, g.away)}
    )
    rows: list[TeamProjectionView] = []
    for team in teams:
        players = tuple(p for p in projection.players if p.player.team_id == team)
        days = tuple(g.tipoff.astimezone(zone).date() for g in games if team in (g.home, g.away))
        minutes = evaluate(
            "linear",
            values=tuple(p.minutes for p in players),
            weights=tuple(p.probability for p in players),
        )
        budget = evaluate(
            "linear",
            values=(float(rules.regulation_minutes),),
            weights=(float(rules.players_on_court),),
        )
        details: list[TeamPlayerView] = []
        for p in players:
            recent = {
                str(count): evaluate("mean", values=p.observed_minutes[-count:])
                for count in (params.override_window.value, params.role_window.value)
                if p.observed_minutes
            }
            multipliers = {
                f.id: evaluate(
                    "linear",
                    values=tuple(float(e.value) for e in p.adjustments if e.field == f.id),
                    weights=(1.0,),
                )
                for f in params.fields
                if f.kind == "multiply" and any(e.field == f.id for e in p.adjustments)
            }
            status_fields = {
                f.id: f
                for f in params.fields
                if f.kind == "status" and "q" in f.targets and not f.only_back_to_back
            }
            statuses = tuple(e for e in p.adjustments if e.field in status_fields)
            groups = {e.group_id for e in statuses}
            returns = sorted(
                {
                    e.starts_on
                    for e in ledger.entries
                    if e.player_id == p.player.id
                    and e.group_id in groups
                    and e.starts_on > projection.on
                }
            )
            returning = next(
                (
                    on
                    for on in returns
                    if any(
                        e.player_id == p.player.id
                        and e.group_id in groups
                        and e.field in status_fields
                        and status_fields[e.field].statuses[str(e.value)] > p.probability
                        for e in active_entries(ledger, on, projection.as_of)
                    )
                ),
                None,
            )
            details.append(
                TeamPlayerView(
                    player_id=p.player.id,
                    recent_minutes=recent,
                    multipliers=multipliers,
                    return_on=p.player.return_on,
                    manual_return_on=returning,
                    manual_status=str(statuses[-1].value) if statuses else None,
                )
            )
        rows.append(
            TeamProjectionView(
                team_id=team,
                week_games=sum(current.start <= d <= current.end for d in days)
                if current
                else None,
                next_week_games=sum(following.start <= d <= following.end for d in days)
                if following
                else None,
                back_to_back=tuple(
                    d
                    for d in sorted(set(days))
                    if d >= projection.on and d - timedelta(days=1) in days
                ),
                minutes=minutes,
                budget=budget,
                difference=evaluate("difference", after=minutes.result, before=budget.result),
                players=tuple(details),
            )
        )
    return tuple(rows)
