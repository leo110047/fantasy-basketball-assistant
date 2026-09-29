from fba.contracts.base import Finite, Natural, Nonnegative, PositiveInt, Record, Text


class StreamingScenario(Record):
    slots: Natural
    started: Nonnegative
    injury_adds: Nonnegative
    upgrade_adds: Nonnegative
    stream_adds: Nonnegative
    gain: Finite


class StreamingComparison(Record):
    slots: Natural
    versus: Natural
    paired_gain: Finite
    block_minimum: Finite
    block_maximum: Finite
    accepted: bool


class FlexPlayer(Record):
    player_id: Text
    removal_loss: Finite


class StreamingSummary(Record):
    roster: tuple[Text, ...]
    recommended_slots: Natural
    configured_slots: Natural
    scenarios: tuple[StreamingScenario, ...]
    comparisons: tuple[StreamingComparison, ...]
    flex: tuple[FlexPlayer, ...]
    shared_limit: Natural
    reserve_adds: Natural
    upgrades: bool
    health_samples: PositiveInt
    health_blocks: PositiveInt
    matchup_count: PositiveInt
    pool_size: Natural
