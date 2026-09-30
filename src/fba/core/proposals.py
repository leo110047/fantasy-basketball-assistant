from fba.contracts.base import DataError
from fba.contracts.inseason import Proposal


def latest_proposals(history: tuple[Proposal, ...]) -> tuple[Proposal, ...]:
    current: dict[str, Proposal] = {}
    seen: set[str] = set()
    for row in history:
        if row.id in seen:
            raise DataError("proposal.id: duplicate immutable proposal record")
        seen.add(row.id)
        if row.supersedes is not None:
            previous = current.pop(row.supersedes, None)
            if previous is None:
                raise DataError("proposal.supersedes: missing or already superseded proposal")
            if (row.opponent, row.send, row.receive) != (
                previous.opponent,
                previous.send,
                previous.receive,
            ):
                raise DataError("proposal.supersedes: cannot change the original trade")
        current[row.id] = row
    return tuple(current.values())
