import pytest
from test_backtest import expanded_fixture, replay_fixture
from test_backtest import kernel as kernel

from fba.contracts.backtest import Pairing
from fba.contracts.base import DataError
from fba.core.scoring import matchup


def test_week_tie_and_category_rounding(kernel):
    source, auction = replay_fixture(kernel)
    league = source.config.league
    axes = auction.management.stat_ids
    values = auction.management.players[0].means
    pairing = Pairing(week_id=league.matchups[0].id, home="a", away="b")
    result = matchup(league, axes, pairing, values, values)
    assert result.winner is None and all(c.winner == "tie" for c in result.categories)
    assert result.home_points == result.away_points
    assert (
        matchup(league, axes, pairing.model_copy(update={"away": None}), values, ()).winner == "a"
    )


def test_signed_category_formula_preserves_display_value_and_winner(kernel):
    from fba.contracts.config import Linear, Term

    source, auction = replay_fixture(kernel)
    category = source.config.league.categories[0].model_copy(
        update={
            "formula": Linear(kind="linear", terms=(Term(stat_id="TO", coefficient=-1.0),)),
            "direction": "higher",
        }
    )
    league = source.config.league.model_copy(update={"categories": (category,)})
    axes = auction.management.stat_ids
    a, b = [0.0] * len(axes), [0.0] * len(axes)
    a[axes.index("TO")], b[axes.index("TO")] = 2.0, 3.0
    result = matchup(league, axes, Pairing(week_id="w", home="a", away="b"), tuple(a), tuple(b))
    assert result.winner == "a"
    assert (result.categories[0].home, result.categories[0].away) == (-2.0, -3.0)


@pytest.mark.parametrize("rule", ["tie", "half_win", "loss"])
def test_regular_tie_record_and_seeding_rules(kernel, rule):
    from fba.core.scoring import standings

    source, auction = replay_fixture(kernel)
    league = source.config.league.model_copy(
        update={
            "scoring": source.config.league.scoring.model_copy(update={"week_tie": rule}),
            "playoffs": source.config.league.playoffs.model_copy(
                update={"seeding": ("record", "head_to_head", "category_record", "seed")}
            ),
        }
    )
    a, b = source.teams
    values = auction.management.players[0].means
    tie = matchup(
        league,
        auction.management.stat_ids,
        Pairing(week_id=league.matchups[0].id, home=a.id, away=b.id),
        values,
        values,
    )
    table = standings(league, source.teams, (tie,))
    assert [r.team_id for r in table] == [a.id, b.id]
    assert table[0].wins == (0.5 if rule == "half_win" else 0)
    assert table[0].losses == (1 if rule == "loss" else 0)
    assert table[0].ties == 1


def test_playoff_bracket_reseeding_ties_and_invalid_bracket(kernel):
    from fba.core.scoring import playoff_round, score_season, standings

    source, auction = expanded_fixture(kernel, 12, False, 0)
    league = source.config.league
    values = auction.management.players[0].means
    boxes = tuple(tuple(values for _ in league.matchups) for _ in source.teams)
    for rule in ("higher_seed", "category_record"):
        variant = league.model_copy(
            update={
                "playoffs": league.playoffs.model_copy(update={"reseed": True, "matchup_tie": rule})
            }
        )
        outcomes, table, champion = score_season(
            variant,
            auction.management.stat_ids,
            source.teams,
            source.pairings,
            boxes,
            tuple(w.id for w in league.matchups),
        )
        assert champion == source.teams[0].id
        assert sum(o.away is None for o in outcomes) == 2
        assert outcomes[-1].winner == champion
    bad = league.model_copy(
        update={
            "playoffs": league.playoffs.model_copy(
                update={"week_ids": league.playoffs.week_ids[:-1]}
            )
        }
    )
    with pytest.raises(DataError, match="incomplete bracket"):
        score_season(
            bad,
            auction.management.stat_ids,
            source.teams,
            source.pairings,
            boxes,
            tuple(w.id for w in league.matchups),
        )
    table = standings(league, source.teams, ())
    seeds = {t.id: t.seed for t in source.teams}

    def play(pairing):
        return matchup(league, auction.management.stat_ids, pairing, values, values)

    assert playoff_round(
        league, league.playoffs.week_ids[0], [None, source.teams[0].id], play, seeds, table, []
    ) == [source.teams[0].id]
    with pytest.raises(DataError, match="paired empty"):
        playoff_round(league, league.playoffs.week_ids[0], [None, None], play, seeds, table, [])

    def invalid(pairing):
        return play(pairing).model_copy(update={"winner": None})

    with pytest.raises(DataError, match="bye must have"):
        playoff_round(
            league,
            league.playoffs.week_ids[0],
            [source.teams[0].id, None],
            invalid,
            seeds,
            table,
            [],
        )
