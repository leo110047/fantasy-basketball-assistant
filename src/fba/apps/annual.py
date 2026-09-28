import tempfile
from pathlib import Path

from fba.adapters.annual import previous_evaluation
from fba.adapters.auction import auction_file, draft_template, prepare_auction
from fba.adapters.codec import decode, read_bytes
from fba.adapters.config import load_config
from fba.adapters.preparation import project
from fba.contracts.auction import AuctionResult, Infeasible
from fba.contracts.base import DataError


def finish_annual(snapshot: Path, model: Path, output: Path, previous: Path | None) -> Path:
    config = load_config(snapshot / "config/league.json", snapshot / "config/season.json", model)
    evaluation = previous_evaluation(snapshot, config)
    try:
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".annual-", dir=output) as staging:
            pending = project(snapshot, model, Path(staging), previous)
            (pending / "previous-evaluation.json").write_bytes(evaluation)
            auction = prepare_auction(pending, model, pending / "auction")
            # Empty room template; these numbered seats do not identify the user's Yahoo team.
            draft = draft_template(
                auction / "auction-input.json", 1, pending / "opening-draft.json"
            )
            result = auction_file(
                auction / "auction-input.json", draft, pending / "opening", "equal"
            )
            opening = decode(AuctionResult, read_bytes(result), str(result))
            if isinstance(opening.plan, Infeasible):
                raise DataError(f"annual.opening: {opening.plan.reason}")
            destination = output / pending.name
            if destination.exists():
                raise DataError(f"{destination}: annual result already exists")
            pending.rename(destination)
        return destination
    except OSError as exc:
        raise DataError(f"{output}: annual publication failed: {exc}") from exc
