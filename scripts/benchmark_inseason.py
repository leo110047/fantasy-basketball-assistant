"""Reproducible synthetic scale audit; never a real NBA/Yahoo acceptance report."""

import argparse
import json
import os
import platform
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import combinations
from math import isfinite
from pathlib import Path
from statistics import median
from threading import Event
from time import perf_counter

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
from inseason_support import DEFAULTS, fixture  # noqa: E402

from fba.apps.inseason.trade_workers import TradeWorkers  # noqa: E402
from fba.apps.workers import worker_limit  # noqa: E402
from fba.contracts.config import Matchup, StarterSlot  # noqa: E402
from fba.contracts.inseason import (  # noqa: E402
    FreeAgent,
    InseasonParameters,
    InseasonPreferences,
    SeasonGame,
    SeasonPairing,
)
from fba.core.lineups import legal_assignment  # noqa: E402
from fba.inseason.matchup import Simulation  # noqa: E402
from fba.inseason.recommendations import search_adds  # noqa: E402
from fba.inseason.today import today  # noqa: E402


def scale_inputs(universal=False, clustered_games=False):
    args = list(fixture(teams=110))
    count = 14 * 13 + 150
    positions = (
        ("PG",),
        ("SG",),
        ("SF",),
        ("PF",),
        ("C",),
        ("PG", "SG"),
        ("SG", "SF"),
        ("SF", "PF"),
        ("PF", "C"),
    )
    slots = tuple(
        StarterSlot(id=label, label=label, eligible_positions=eligible)
        for label, eligible in (
            ("PG", ("PG",)),
            ("SG", ("SG",)),
            ("SF", ("SF",)),
            ("PF", ("PF",)),
            ("C", ("C",)),
            ("G", ("PG", "SG")),
            ("F", ("SF", "PF")),
            *((f"U{i}", ("PG", "SG", "SF", "PF", "C")) for i in range(3)),
        )
    )
    if universal:
        slots = tuple(
            s.model_copy(update={"eligible_positions": ("PG", "SG", "SF", "PF", "C")})
            for s in slots
        )
    weeks = tuple(
        Matchup(
            id=str(i + 1),
            start=args[0].starts_on + timedelta(days=7 * i),
            end=args[0].starts_on + timedelta(days=7 * i + 6),
            phase="regular" if i < 25 else "playoff",
        )
        for i in range(28)
    )
    args[0] = args[0].model_copy(
        update={
            "teams": 14,
            "positions": ("PG", "SG", "SF", "PF", "C"),
            "starter_slots": slots,
            "bench_slots": 3,
            "matchups": weeks,
            "ends_on": weeks[-1].end,
            "playoff_weeks": tuple(w.id for w in weeks if w.phase == "playoff"),
            "playoff_teams": 8,
        }
    )
    args[1] = InseasonParameters.model_validate_json((DEFAULTS / "parameters.json").read_bytes())
    players = tuple(
        p.model_copy(update={"team_id": f"NBA{i % 30}", "positions": positions[i % len(positions)]})
        for i, p in enumerate(args[2].players[:count])
    )
    ids = {p.id for p in players}
    factor = {p.id: (0.75 + (i % 40) / 80) / (1 + i / 20) for i, p in enumerate(players)}
    boxes = tuple(
        b.model_copy(
            update={
                "team_id": players[int(b.player_id[1:])].team_id,
                "stats": {s: v * factor[b.player_id] for s, v in b.stats.items()},
            }
        )
        for b in args[2].boxes
        if b.player_id in ids
    )
    games = tuple(
        SeasonGame(
            id=f"g:{day}:{i}",
            home=f"NBA{i}",
            away=f"NBA{i + 15}",
            tipoff=args[-1] + timedelta(days=day * 2 + (0 if clustered_games else i % 2), hours=12),
            known_at=args[-1] - timedelta(days=20),
            status="scheduled",
        )
        for day in range(82)
        for i in range(15)
    )
    args[2] = args[2].model_copy(update={"players": players, "boxes": boxes, "games": games})
    args[3] = args[3].model_copy(
        update={
            "players": tuple(
                p.model_copy(
                    update={"rates": {s: v * factor[p.player_id] for s, v in p.rates.items()}}
                )
                for p in args[3].players
                if p.player_id in ids
            )
        }
    )
    teams = []
    for i, team in enumerate(args[5].teams[:14]):
        roster = tuple(f"p{13 * i + j}" for j in range(13))
        eligible = {pid: players[int(pid[1:])].positions for pid in roster}
        assignment = next(
            a
            for chosen in combinations(roster, 10)
            if (a := legal_assignment(slots, eligible, chosen)) is not None
        )
        teams.append(team.model_copy(update={"players": roster, "selected_slots": assignment}))
    team_ids = {t.id for t in teams}
    args[5] = args[5].model_copy(
        update={
            "teams": tuple(teams),
            "pairings": tuple(
                SeasonPairing(week_id=w.id, home=f"team{i}", away=f"team{i + 1}")
                for w in weeks
                for i in range(0, 14, 2)
            ),
            "actual": tuple(a for a in args[5].actual if a.team_id in team_ids),
            "free_agents": tuple(
                FreeAgent(player_id=f"p{i}", status="free", clears_at=None)
                for i in range(182, count)
            ),
        }
    )
    return args


def measure_trade(sim, operation, prefs, workers):
    start = perf_counter()
    result = workers.search(
        sim,
        prefs,
        "team2" if operation == "trade_many" else None,
        2 if operation == "trade_many" else 1,
        lambda _: None,
        Event(),
    )
    seconds = perf_counter() - start
    payload = json.dumps(
        [row.model_dump(mode="json") for row in result], sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "status": "completed",
        "seconds": seconds,
        "rows": len(result),
        "counts": sim.trade_search_counts.copy(),
        "payload_sha256": sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
    }


def measure(sim, operation, prefs, workers):
    start = perf_counter()
    try:
        if operation.startswith("trade_"):
            return measure_trade(sim, operation, prefs, workers)
        elif operation == "week":
            with sim.budget("week"):
                result = sim.week("team0", "team1", "2")
            details = {"score": result.score, "samples": result.simulations}
        elif operation == "today":
            with sim.budget("today"):
                today(sim, sim.as_of.astimezone(sim.zone).date(), "Asia/Taipei")
            details = {}
        elif operation == "recommendations":
            result = search_adds(sim, prefs, "2", lambda _: None)
            details = {"rows": len(result)}
        return {"status": "completed", "seconds": perf_counter() - start, **details}
    except TimeoutError as exc:
        return {
            "status": type(exc).__name__,
            "seconds": perf_counter() - start,
            "error": str(exc),
            "counts": sim.trade_search_counts.copy(),
        }


def positive_seconds(value):
    seconds = float(value)
    if not isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("diagnostic budget must be finite and positive")
    return seconds


def parse_options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--operations",
        nargs="+",
        choices=["week", "today", "recommendations", "trade_one", "trade_many"],
        default=["week", "today", "recommendations", "trade_one", "trade_many"],
    )
    parser.add_argument("--universal", action="store_true")
    parser.add_argument("--clustered-games", action="store_true")
    parser.add_argument("--stratified-ranks", action="store_true")
    parser.add_argument("--workers", type=worker_limit, default=worker_limit("auto"))
    parser.add_argument("--cold-only", action="store_true")
    parser.add_argument("--diagnostic-budget", type=positive_seconds)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.rounds < 1:
        parser.error("rounds must be positive")
    return options


def summarize(rows, operations, temperatures):
    result = []
    for operation in operations:
        for temperature in temperatures:
            subset = [
                r for r in rows if r["operation"] == operation and r["temperature"] == temperature
            ]
            seconds = sorted(r["seconds"] for r in subset)
            completed = sorted(r["seconds"] for r in subset if r["status"] == "completed")
            result.append(
                {
                    "operation": operation,
                    "temperature": temperature,
                    "runs": len(subset),
                    "completed": sum(r["status"] == "completed" for r in subset),
                    "observed_wall_p50": median(seconds),
                    "observed_wall_p95": seconds[max(0, int(0.95 * len(seconds) + 0.999) - 1)],
                    "completed_p50": median(completed) if completed else None,
                    "completed_p95": completed[max(0, int(0.95 * len(completed) + 0.999) - 1)]
                    if completed
                    else None,
                }
            )
    return result


def prepare_inputs(options):
    args = scale_inputs(options.universal, options.clustered_games)
    product_budgets = {k: v.value for k, v in args[1].budgets.items()}
    if options.diagnostic_budget is not None:
        args[1] = args[1].model_copy(
            update={
                "budgets": {
                    k: v.model_copy(update={"value": options.diagnostic_budget})
                    for k, v in args[1].budgets.items()
                }
            }
        )
    if options.stratified_ranks:
        teams, capacity = len(args[5].teams), len(args[5].teams[0].players)
        args[2] = args[2].model_copy(
            update={
                "players": tuple(
                    p.model_copy(update={"public_rank": (i % capacity) * teams + i // capacity + 1})
                    if i < teams * capacity
                    else p
                    for i, p in enumerate(args[2].players)
                )
            }
        )
    return args, product_budgets


def report_metadata(options, args, product_budgets, rows):
    return {
        "started_at": datetime.now(UTC).isoformat(),
        "scope": "synthetic 14x13, 10 starters, 150 FA, 28 weeks; NOT live NBA/Yahoo",
        "measurement": (
            "calculation/controller; includes lazy pool startup, shared baselines and "
            "result transfer; excludes final JSON serialization, API storage, HTTP/UI"
        ),
        "warmth": "hot reuses the same Simulation and pool; no persisted result cache",
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "parameters_sha256": sha256((DEFAULTS / "parameters.json").read_bytes()).hexdigest(),
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_hashes": {
            str(p.relative_to(Path(__file__).parents[1])): sha256(p.read_bytes()).hexdigest()
            for p in sorted((Path(__file__).parents[1] / "src/fba").rglob("*.py"))
        },
        "preferences_sha256": sha256((DEFAULTS / "preferences.json").read_bytes()).hexdigest(),
        "universal_slots": options.universal,
        "stratified_ranks": options.stratified_ranks,
        "worker_limit": options.workers,
        "diagnostic_budget": options.diagnostic_budget,
        "product_budgets": product_budgets,
        "schedule": "all NBA teams on the same days (extreme synthetic workload)"
        if options.clustered_games
        else "7-8 NBA games per day, fixed synthetic opponents",
        "budgets": {k: v.value for k, v in args[1].budgets.items()},
        "rows": rows,
        "summary": [],
    }


def main():
    options = parse_options()
    args, product_budgets = prepare_inputs(options)
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    rows = []
    report = report_metadata(options, args, product_budgets, rows)
    temperatures = ("cold",) if options.cold_only else ("cold", "hot")
    for operation in options.operations:
        for run in range(options.rounds):
            samples = args[1].season_simulations.value if operation.startswith("trade_") else None
            sim = Simulation(*args, samples=samples)
            workers = TradeWorkers(options.workers)
            try:
                for temperature in temperatures:
                    row = {
                        "operation": operation,
                        "run": run,
                        "temperature": temperature,
                        **measure(sim, operation, prefs, workers),
                    }
                    rows.append(row)
                    options.output.write_text(json.dumps(report, indent=2) + "\n")
                    print(json.dumps(row), flush=True)
            finally:
                workers.close()
    report["summary"] = summarize(rows, options.operations, temperatures)
    report["finished_at"] = datetime.now(UTC).isoformat()
    options.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
