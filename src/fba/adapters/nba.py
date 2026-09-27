import re
from html.parser import HTMLParser

from fba.contracts.base import DataError
from fba.contracts.data import ScheduleCount


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def release_counts(
    data: bytes,
    season_id: str,
    source_id: str,
    team_ids: tuple[str, ...],
) -> tuple[ScheduleCount, ...]:
    parser = TextParser()
    try:
        parser.feed(data.decode("utf-8"))
    except UnicodeError as exc:
        raise DataError(f"{source_id}: NBA release is not UTF-8") from exc
    text = " ".join(" ".join(parser.parts).split())
    if f"{season_id} regular season" not in text:
        raise DataError(f"{source_id}: NBA release season mismatch")
    matches = re.findall(
        r"schedule includes defined dates and opponents for (\d+) of each team[’']s (\d+) games",
        text,
    )
    if len(matches) != 1 or "unassigned games" not in text or "NBA Cup" not in text:
        raise DataError(f"{source_id}: NBA release count format changed; supply explicit counts")
    announced, total = (int(value) for value in matches[0])
    team_count = re.findall(r"with all (\d+) teams in action", text)
    if len(team_count) != 1 or int(team_count[0]) != len(team_ids):
        raise DataError(f"{source_id}: schedule team coverage differs from official release")
    if total < announced or not team_ids:
        raise DataError(f"{source_id}: invalid published game counts or missing teams")
    return tuple(
        ScheduleCount(
            team_id=team,
            announced=announced,
            pending=total - announced,
            pending_reason="NBA Cup group results determine unassigned games"
            if total > announced
            else None,
            source_id=source_id,
        )
        for team in sorted(team_ids)
    )
