"""Weekly common random samples for exchangeable generated game distributions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

import numpy as np

from fba.inseason.sampling import SamplingProfile

if TYPE_CHECKING:
    from fba.contracts.inseason import DayLineup
    from fba.inseason.matchup import Array, Simulation

type GroupKey = tuple[str, SamplingProfile | None, date | None]


@dataclass
class CountSamples:
    """One fixed pool per law; selecting k starts uses its first k game draws.

    Pools and total accumulation follow first appearance (date, player ID),
    independently of search ordering and selected slots. All available draws
    enter the pool, including those that a locked lineup cannot select.
    """

    choices: tuple[tuple[Array, ...], ...]
    by_day: tuple[dict[str, int], ...]

    @classmethod
    def create(
        cls,
        sim: Simulation,
        days: tuple[tuple[date, dict[str, Array]], ...],
        after: datetime,
    ) -> CountSamples | None:
        groups: dict[GroupKey, int] = {}
        prefixes: list[list[Array]] = []
        mappings: list[dict[str, int]] = []
        for on, draws in days:
            sim.check_limits()
            players = sim.player_index.get(sim.projection(on))
            mapping: dict[str, int] = {}
            for pid in sorted(draws):
                games = tuple(
                    g for g in sim.games_on(players[pid].player.team_id, on) if g.tipoff > after
                )
                if not games and not np.any(draws[pid]):
                    # A zero remaining draw can still occupy a locked starter.
                    key: GroupKey = pid, None, on
                elif len(games) == 1:
                    identity = pid, games[0].id
                    evidence = sim.draw_profiles.get(identity)
                    if evidence is None or evidence[0] is not sim.draws.get(identity):
                        return None  # Explicit scenario arrays have no exchangeability proof.
                    key = pid, evidence[1], None
                else:
                    return None  # Multiple games in one daily decision are not unit flows.
                if key not in groups:
                    groups[key] = len(prefixes)
                    prefixes.append([np.zeros_like(draws[pid])])
                index = groups[key]
                prefixes[index].append(prefixes[index][-1] + draws[pid])
                mapping[pid] = index
            mappings.append(mapping)
        return cls(tuple(tuple(p) for p in prefixes), tuple(mappings))

    def counts(self, rows: tuple[dict[str, str], ...]) -> tuple[int, ...]:
        counts = [0] * len(self.choices)
        for mapping, row in zip(self.by_day, rows, strict=True):
            for pid in row.values():
                counts[mapping[pid]] += 1
        return tuple(counts)

    def total(self, actual: Array, counts: tuple[int, ...]) -> Array:
        return sum(
            (choices[count] for choices, count in zip(self.choices, counts, strict=True)),
            start=actual.copy(),
        )


def lineup_samples(
    sim: Simulation, team: str, days: tuple[DayLineup, ...], through: datetime
) -> CountSamples | None:
    return CountSamples.create(
        sim,
        tuple(
            (
                day.on,
                sim.daily_draws(
                    sim.projected_roster(team, day.on, sim.roster(team)),
                    day.on,
                    max(sim.as_of, through),
                ),
            )
            for day in days
        ),
        max(sim.as_of, through),
    )
