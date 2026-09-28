import argparse
import json
import sys
import tempfile
from pathlib import Path

from fba.adapters.acquisition import Acquired, acquire
from fba.adapters.annual import archive_source
from fba.adapters.auction import auction_file, draft_template, prepare_auction
from fba.adapters.backtest import backtest_file
from fba.adapters.calculation import calculate_file, evaluate_file
from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.adapters.config import load_config
from fba.adapters.migration import migrate_projection
from fba.adapters.preparation import project
from fba.adapters.snapshots import frozen_inputs, inventory_json, load_snapshot, publish
from fba.apps.annual import finish_annual
from fba.apps.auction import AuctionSession
from fba.apps.build import assemble
from fba.apps.server import serve
from fba.contracts.auction import SolverError
from fba.contracts.base import ConfigError, DataError, IdentityError
from fba.contracts.config import ValidatedConfig


def build(league: Path, season: Path, model: Path, output: Path, version: int) -> Path:
    if version < 1:
        raise ConfigError("version: must be a positive integer")
    config = load_config(league, season, model)
    base = season.parent
    # No output directory is published until acquisition, identity and data checks all pass.
    sources = tuple(
        acquire(source, base, config.season.snapshot_as_of) for source in config.season.sources
    )
    inputs = {
        "config/league.json": read_bytes(league),
        "config/season.json": read_bytes(season),
        "config/model.json": read_bytes(model),
        "inputs/roster.csv": read_bytes(base / config.season.roster_import.path),
        "inputs/identities.json": read_bytes(base / config.season.identity_map_path),
        "inputs/adjustments.json": read_bytes(base / config.season.manual_adjustments_path),
    }
    for name, ref in (
        ("league", config.refs.league),
        ("season", config.refs.season),
        ("model", config.refs.model),
    ):
        if digest(inputs[f"config/{name}.json"]) != ref.input_sha256:
            raise ConfigError(
                f"{name}: changed during acquisition; rerun with stable configuration"
            )
    artifacts, files = frozen_inputs(config, sources, inputs)
    snapshot = assemble(
        config,
        sources,
        inputs["inputs/roster.csv"],
        inputs["inputs/identities.json"],
        inputs["inputs/adjustments.json"],
        artifacts,
        version,
    )
    return publish(snapshot, files, output)


def rebuild(root: Path, output: Path) -> Path:
    original = load_snapshot(root)
    config = load_config(
        root / "config/league.json", root / "config/season.json", root / "config/model.json"
    )
    effective = decode(
        ValidatedConfig, read_bytes(root / "config/effective.json"), "effective config"
    )
    if config != effective or original.config != config.refs:
        raise DataError("snapshot.config: frozen configuration hashes or values disagree")
    files = {a.path: read_bytes(root / a.path) for a in original.artifacts}
    source_files = {
        a.provenance.source_id: a for a in original.artifacts if a.provenance is not None
    }
    sources: list[Acquired] = []
    for source in config.season.sources:
        entry = source_files.get(source.id)
        if entry is None or entry.provenance is None:
            raise DataError(f"snapshot: missing frozen source {source.id}")
        sources.append(Acquired(source=source, data=files[entry.path], provenance=entry.provenance))
    candidate = assemble(
        config,
        tuple(sources),
        files["inputs/roster.csv"],
        files["inputs/identities.json"],
        files["inputs/adjustments.json"],
        original.artifacts,
        original.version,
    )
    if canonical(candidate) != canonical(original):
        raise DataError("snapshot: offline reconstruction differs from frozen snapshot")
    return publish(candidate, files, output)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="fba")
    commands = root.add_subparsers(dest="command", required=True)
    for action in ("build", "annual"):
        build_parser = commands.add_parser(
            action, help="Acquire a snapshot; annual also values and evaluates it"
        )
        for name in ("league", "season", "model", "output"):
            build_parser.add_argument(f"--{name}", required=True, type=Path)
        build_parser.add_argument("--version", required=True, type=int)
        if action == "annual":
            build_parser.add_argument("--previous", type=Path)
    projection = commands.add_parser("project", help="Prepare and value a frozen data snapshot")
    projection.add_argument("snapshot", type=Path)
    projection.add_argument("--model", required=True, type=Path)
    projection.add_argument("--output", required=True, type=Path)
    projection.add_argument("--previous", type=Path)
    migration = commands.add_parser(
        "migrate-projection", help="Upgrade a resource input using an explicit new model"
    )
    migration.add_argument("input", type=Path)
    migration.add_argument("--model", required=True, type=Path)
    migration.add_argument("--calibration-snapshot", required=True, type=Path)
    migration.add_argument("--output", required=True, type=Path)
    for name in ("calculate", "evaluate", "backtest"):
        calculation = commands.add_parser(name, help="Compute from a frozen calculation input")
        calculation.add_argument("input", type=Path)
        calculation.add_argument("--output", required=True, type=Path)
    for name in ("rebuild", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("snapshot", type=Path)
        if name == "rebuild":
            command.add_argument("--output", required=True, type=Path)
    auction = commands.add_parser("auction", help="Compute an offline auction state")
    auction.add_argument("input", type=Path)
    auction.add_argument("--draft", required=True, type=Path)
    auction.add_argument("--workers", required=True, type=int)
    auction.add_argument("--output", required=True, type=Path)
    auction.add_argument("--stage", choices=("market", "equal", "fit"), required=True)
    prepare = commands.add_parser(
        "prepare-auction", help="Freeze an auction from a valued projection"
    )
    prepare.add_argument("projection", type=Path)
    prepare.add_argument("--model", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)
    draft = commands.add_parser(
        "draft-template", help="Create an empty draft with editable team labels"
    )
    draft.add_argument("input", type=Path)
    draft.add_argument("--mine", required=True, type=int)
    draft.add_argument("--output", required=True, type=Path)
    desk = commands.add_parser("serve", help="Open a local offline auction desk")
    desk.add_argument("input", type=Path)
    desk.add_argument("--draft", required=True, type=Path)
    desk.add_argument("--log", required=True, type=Path)
    desk.add_argument("--workers", required=True, type=int)
    desk.add_argument("--port", type=int, default=0)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "serve":
            serve(args.input, args.draft, args.log, args.workers, args.port)
            return 0
        if args.command == "draft-template":
            print(json.dumps({"draft": str(draft_template(args.input, args.mine, args.output))}))
            return 0
        if args.command in ("auction", "prepare-auction"):
            if args.command == "auction":
                session = AuctionSession(args.workers)
                try:
                    path = auction_file(
                        args.input,
                        args.draft,
                        args.output,
                        args.stage,
                        session.caps,
                        session.features,
                        session.native() if args.stage == "fit" else None,
                    )
                finally:
                    session.close()
            else:
                path = prepare_auction(args.projection, args.model, args.output)
            print(json.dumps({"result": str(path)}))
            return 0
        if args.command in ("project", "annual"):
            if args.command == "annual":
                archive_source(load_config(args.league, args.season, args.model))
                with tempfile.TemporaryDirectory(prefix="fba-annual-") as folder:
                    snapshot = build(
                        args.league, args.season, args.model, Path(folder), args.version
                    )
                    path = finish_annual(snapshot, args.model, args.output, args.previous)
            else:
                path = project(args.snapshot, args.model, args.output, args.previous)
            print(json.dumps({"projection": str(path)}))
            return 0
        if args.command == "migrate-projection":
            path = migrate_projection(
                args.input, args.model, args.calibration_snapshot, args.output
            )
            print(json.dumps({"input": str(path / "projection-input.json")}))
            return 0
        if args.command in ("calculate", "evaluate", "backtest"):
            operation = {
                "calculate": calculate_file,
                "evaluate": evaluate_file,
                "backtest": backtest_file,
            }[args.command]
            result = operation(args.input, args.output)
            print(json.dumps({"result": str(result), "sha256": digest(read_bytes(result))}))
            return 0
        if args.command == "build":
            path = build(args.league, args.season, args.model, args.output, args.version)
        elif args.command == "rebuild":
            path = rebuild(args.snapshot, args.output)
        else:
            print(inventory_json(load_snapshot(args.snapshot)))
            return 0
        print(
            json.dumps(
                {"snapshot": str(path), "sha256": digest(read_bytes(path / "snapshot.json"))}
            )
        )
    except IdentityError as exc:
        print(json.dumps({"error": str(exc), "unresolved": exc.unresolved}), file=sys.stderr)
        return 2
    except (ConfigError, DataError, SolverError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
