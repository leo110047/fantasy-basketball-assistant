# Weekly sampling and exact count search

The weekly model samples independent games conditional on a player's frozen
history, projected minutes, per-minute rates and availability probability.
Game IDs select RNG streams; game dates have no other distributional effect.
Within one week, games for the same player with identical sampling inputs are
therefore exchangeable. Selecting any k of them has the same joint distribution
as selecting the first k independent draws. Derived stats are still calculated
per game, before summation.

Use that count-prefix coupling for generated samples. It changes the finite
Monte Carlo objective across alternatives, not the probability law of an
individual fixed lineup. Results need not equal the former game-ID-coupled
objective. This also removes opportunities to choose dates merely for lucky
simulation draws. Do not merge distinct players, availability/projection
profiles, or unverified externally supplied scenario arrays. Multi-game days
retain the existing complete appearance search until modeled explicitly.

The weekly composition owns prefix sums and deterministic group-order totals.
Both the baseline opponent and optimized team use that composition. Search
enumerates every attainable group count; a unit-capacity flow network enforces
dates, positions, locked starters and roster changes. Canonical reconstruction
preserves the existing tie order: most starters, then sorted player IDs by day.
Bounds relax count choices but retain correlated numerator/denominator samples,
comparison rounding, calibration and outward floating-point error allowance.
No top-k, beam truncation, changed sample count or extended deadline is allowed.

Verification must include independent exhaustive legal-lineup oracles under
this coupling, flow feasibility versus enumeration, non-exchangeable fallback,
locks/transitions, counterfactual Today effects, bound soundness, interruption,
and the captured full league workload. Fixture/replay speed is not provider or
historical calibration evidence. The existing game-ID scenario oracles remain.

The portability fixture now records each team's sampling policy. Its numerical
reference migrates to `exchangeable_count_v1` after independent exhaustive count
oracles, exact finite-distribution enumeration and Today counterfactual checks;
the former game-ID reference is not a correctness target for this new coupling.
Platform tolerances and candidate coverage remain unchanged.

The fixed F3 cases were re-enumerated with the breadth-first reference (no z/drop
ordering or beam): 75, 60, 2,900 and 2,510 legal plans. Their new objective maxima
are 0.8512, 2.0869, 1.356 and 4.40832 respectively. The regression retains the
original samples/tolerance and additionally checks the number of evaluated plans.
Old arbitrary game-ID scenario oracles remain unchanged; quality enumeration
does not establish completion within product deadlines.

Infeasible flow results supply residual min-cut inequalities: the sum of demand
on source-reachable group nodes cannot exceed the fixed internal cut capacity.
Reuse these necessary inequalities for partial-count pruning. Canonical daily
rows are still reconstructed from the complete row set, with batched integer
suffix checks and exact flow validation. No failed candidate is extrapolated
without this capacity certificate.

F3 release screening uses confirmed regular-season opponents when future playoff
opponents are unknown. This heuristic only changes traversal order; full F3
evaluation retains explicit missing playoff-strength evidence. Injury-return
release decisions still require their complete configured objective.
