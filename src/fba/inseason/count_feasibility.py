"""Exact count realization using unit player/day and starter-slot capacities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import maximum_flow

if TYPE_CHECKING:
    from fba.inseason.weekly_lineups import Assignment, WeeklySearch
    from fba.inseason.weekly_samples import CountSamples

type Counts = NDArray[np.int64]


@dataclass
class CountFeasibility:
    search: WeeklySearch
    samples: CountSamples
    remaining: Counts
    required: Counts
    capacities: tuple[int, ...]
    graphs: tuple[tuple[csr_matrix, int], ...]
    rows: tuple[tuple[Assignment, ...], ...]
    row_counts: tuple[Counts, ...]
    cuts: dict[tuple[int, ...], int] = field(default_factory=dict)

    @classmethod
    def create(cls, search: WeeklySearch, samples: CountSamples) -> CountFeasibility:
        count = len(samples.choices)
        shape = len(search.days) + 1, count
        remaining = np.zeros(shape, dtype=np.int64)
        required = remaining.copy()
        capacities = [0] * shape[0]
        fixed = tuple(
            set(day.rows[0].values()).intersection(*(set(row.values()) for row in day.rows[1:]))
            for day in search.days
        )
        for d in reversed(range(len(search.days))):
            remaining[d], required[d] = remaining[d + 1], required[d + 1]
            capacities[d] = capacities[d + 1] + max(map(len, search.days[d].rows))
            for pid in {p for row in search.days[d].rows for p in row.values()}:
                i = samples.by_day[d][pid]
                remaining[d, i] += 1
                required[d, i] += int(pid in fixed[d])
        graphs = tuple(flow_graph(search, samples, fixed, d) for d in range(shape[0]))
        rows = tuple(
            tuple(sorted(day.rows, key=lambda row: tuple(sorted(row.values()))))
            for day in search.days
        )
        row_counts: list[Counts] = []
        for d, choices in enumerate(rows):
            counts = np.zeros((len(choices), count), dtype=np.int64)
            for j, row in enumerate(choices):
                for pid in row.values():
                    counts[j, samples.by_day[d][pid]] += 1
            row_counts.append(counts)
        return cls(
            search, samples, remaining, required, tuple(capacities), graphs, rows, tuple(row_counts)
        )

    def feasible(self, counts: Counts, start: int) -> bool:
        self.search.sim.check_limits()
        if (
            np.any(counts < self.required[start])
            or np.any(counts > self.remaining[start])
            or int(counts.sum()) > self.capacities[start]
        ):
            return False
        demands = counts - self.required[start]
        if not demands.any():
            return True
        template, sink = self.graphs[start]
        graph = template.copy()
        # CSR source row has precisely the ordered group edges (including zero demand).
        graph.data[graph.indptr[0] : graph.indptr[1]] = demands
        result = maximum_flow(graph, 0, sink)
        valid = bool(result.flow_value == demands.sum())
        if not valid and start == 0:
            self.record_cut(graph, graph - result.flow)
        return valid

    def record_cut(self, graph: csr_matrix, residual: csr_matrix) -> None:
        """An infeasible flow supplies a necessary group-capacity inequality.

        Remove source edges from the residual min cut. For every future count
        vector, demand on reachable groups cannot exceed that fixed capacity.
        This is a certificate, not an empirical exclusion of a failed vector.
        """
        reached, queue = {0}, [0]
        for node in queue:
            for edge in range(int(residual.indptr[node]), int(residual.indptr[node + 1])):
                target = int(residual.indices[edge])
                if residual.data[edge] > 0 and target not in reached:
                    reached.add(target)
                    queue.append(target)
        capacity = sum(
            int(graph.data[edge])
            for node in reached - {0}
            for edge in range(int(graph.indptr[node]), int(graph.indptr[node + 1]))
            if int(graph.indices[edge]) not in reached
        )
        groups = tuple(i for i in range(len(self.samples.choices)) if i + 1 in reached)
        self.cuts[groups] = capacity

    def prefix_possible(self, order: tuple[int, ...], chosen: tuple[int, ...]) -> bool:
        demand = {
            group: count - int(self.required[0, group])
            for group, count in zip(order, chosen, strict=False)
        }
        return all(
            sum(demand.get(i, 0) for i in groups) <= cap for groups, cap in self.cuts.items()
        )

    def canonical(self, counts: tuple[int, ...]) -> tuple[Assignment, ...] | None:
        left = np.asarray(counts, dtype=np.int64)
        if not self.feasible(left, 0):
            return None
        result: list[Assignment] = []
        for d, rows in enumerate(self.rows):
            # Batch the cheap suffix-count constraints, then flow-check only
            # survivors in lexical order. Every row remains represented.
            alternatives = left - self.row_counts[d]
            allowed = (
                np.all(alternatives >= self.required[d + 1], axis=1)
                & np.all(alternatives <= self.remaining[d + 1], axis=1)
                & (alternatives.sum(axis=1) <= self.capacities[d + 1])
            )
            for index in np.flatnonzero(allowed):
                after = alternatives[index]
                if self.feasible(after, d + 1):
                    result.append(rows[int(index)])
                    left = after
                    break
            else:
                raise AssertionError("feasible counts lack a legal daily realization")
        return tuple(result)


def flow_graph(
    search: WeeklySearch,
    samples: CountSamples,
    fixed: tuple[set[str], ...],
    start: int,
) -> tuple[csr_matrix, int]:
    count = len(samples.choices)
    edges: list[tuple[int, int, int]] = []
    node = count + 1
    for d in range(start, len(search.days)):
        search.sim.check_limits()
        day = search.days[d]
        locked = {slot for slot, pid in day.rows[0].items() if pid in fixed[d]}
        slots = {slot for row in day.rows for slot in row} - locked
        slot_nodes = {slot: node + i for i, slot in enumerate(sorted(slots))}
        node += len(slots)
        for pid in sorted({p for row in day.rows for p in row.values()} - fixed[d]):
            player_node = node
            node += 1
            edges.append((samples.by_day[d][pid] + 1, player_node, 1))
            usable = {slot for row in day.rows for slot, player in row.items() if player == pid}
            edges.extend((player_node, slot_nodes[slot], 1) for slot in sorted(usable))
        edges.extend((value, -1, 1) for value in slot_nodes.values())
    sink = node
    edges = [(u, sink if v == -1 else v, c) for u, v, c in edges]
    edges.extend((0, i + 1, 1) for i in range(count))
    graph = csr_matrix(
        ([c for _, _, c in edges], ([u for u, _, _ in edges], [v for _, v, _ in edges])),
        shape=(sink + 1, sink + 1),
        dtype=np.int32,
    )
    return graph, sink
