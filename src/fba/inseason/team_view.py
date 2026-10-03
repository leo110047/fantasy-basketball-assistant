from datetime import timedelta
from zoneinfo import ZoneInfo

from fba.contracts.config import Category, Linear
from fba.contracts.inseason import (
    AdjustmentLedger,
    EffectiveProjection,
    FrozenPriors,
    InseasonLeague,
    InseasonParameters,
    PlayerSnapshot,
    ProjectionRules,
    SeasonGame,
)
from fba.contracts.inseason_results import TeamPlayerView, TeamProjectionView
from fba.formulas.registry import evaluate
from fba.formulas.simulation import mean_array
from fba.inseason.adjustments import active_entries
from fba.inseason.projection import observed_boxes, with_expectations
from fba.inseason.sampling import sample_game


def player_categories(
    projection: EffectiveProjection,
    rules: ProjectionRules,
    params: InseasonParameters,
    priors: FrozenPriors,
    snapshot: PlayerSnapshot,
    games: tuple[SeasonGame, ...],
) -> dict[str, dict[str, float | None]]:
    """Current means conditional on playing; ratios use conditional mean totals.

    Nonlinear derived counts reuse the game's sampler, never threshold mean stats.
    The next captured game supplies the RNG identity, not a future role assumption.
    """
    next_games: dict[str, SeasonGame] = {}
    for game in sorted(games, key=lambda g: (g.tipoff, g.id)):
        if game.tipoff >= projection.as_of:
            for team in (game.home, game.away):
                next_games.setdefault(team, game)
    axes = (*rules.base_stats, *(d.id for d in rules.derived))
    history = observed_boxes(snapshot, projection.as_of)
    needed = {
        t.stat_id
        for c in rules.categories
        for t in (
            c.formula.terms
            if isinstance(c.formula, Linear)
            else (*c.formula.numerator, *c.formula.denominator)
        )
    }
    result: dict[str, dict[str, float | None]] = {}
    for player in projection.players:
        conditional = with_expectations(player.model_copy(update={"probability": 1.0}), rules)
        expected = dict(conditional.expected)
        missing = tuple(d.id for d in rules.derived if d.id in needed and d.id not in expected)
        game = next_games.get(player.player.team_id)
        boxes = history.get(player.player.id, ())
        if (
            missing
            and game
            and (any(b.minutes > 0 for b in boxes) or priors.distribution is not None)
        ):
            sampled = sample_game(
                conditional,
                boxes,
                rules,
                params,
                priors,
                params.simulations.value,
                game,
            )
            for stat in missing:
                expected[stat] = float(mean_array(sampled[:, axes.index(stat)], axis=0))
        result[player.player.id] = {c.id: category_estimate(expected, c) for c in rules.categories}
    return result


def category_estimate(expected: dict[str, float], category: Category) -> float | None:
    formula = category.formula
    terms = (
        formula.terms if isinstance(formula, Linear) else (*formula.numerator, *formula.denominator)
    )
    if any(t.stat_id not in expected for t in terms):
        return None
    numerator_terms = formula.terms if isinstance(formula, Linear) else formula.numerator
    numerator = evaluate(
        "linear",
        values=tuple(expected[t.stat_id] for t in numerator_terms),
        weights=tuple(t.coefficient for t in numerator_terms),
    ).result
    if isinstance(formula, Linear):
        return numerator
    denominator = evaluate(
        "linear",
        values=tuple(expected[t.stat_id] for t in formula.denominator),
        weights=tuple(t.coefficient for t in formula.denominator),
    ).result
    return (
        evaluate("ratio", numerator=numerator, denominator=denominator, zero_value=0.0).result
        if denominator > 0
        else None
    )


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
