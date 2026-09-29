import pytest
from test_desk import desk as desk
from test_desk import wait_for

from fba.contracts.auction import DraftOverride, Sale
from fba.contracts.base import DataError
from fba.contracts.desk import SaveDraft


def save_state(desk, **changes):
    current = desk.bootstrap().desk
    return desk.save(
        SaveDraft(
            expected_sha256=current.market.state_sha256,
            draft=current.state.model_copy(update=changes),
        )
    )


def test_historical_correction_validates_later_sales_and_is_atomic(desk):
    initial = desk.bootstrap().desk.state
    sales = (
        Sale(id="first", player_id="000", buyer=initial.mine, amount=10),
        Sale(id="later", player_id="001", buyer=initial.mine, amount=180),
    )
    save_state(desk, sales=sales)
    before = desk.path.read_bytes()
    history = tuple((desk.path.parent / f".{desk.path.name}.history").iterdir())
    with pytest.raises(DataError, match=r"draft.sales.later.amount"):
        save_state(desk, sales=(sales[0].model_copy(update={"amount": 30}), sales[1]))
    assert desk.path.read_bytes() == before
    assert tuple((desk.path.parent / f".{desk.path.name}.history").iterdir()) == history
    changed = save_state(desk, sales=(sales[0].model_copy(update={"amount": 5}), sales[1]))
    assert tuple(s.id for s in changed.state.sales) == ("first", "later")
    assert next(t.budget for t in changed.market.market.room if t.id == initial.mine) == 15


def test_override_refresh_and_reset_use_same_durable_draft(desk):
    initial = desk.bootstrap().desk.state
    sale = Sale(id="first", player_id="000", buyer=initial.mine, amount=10)
    save_state(desk, sales=(sale,))
    original = desk.inputs.players[0]
    invalid = DraftOverride(
        player_id=original.id, market=15, positions=("C",), reason="Confirmed test correction"
    )
    before = desk.path.read_bytes()
    with pytest.raises(DataError, match="cannot complete starter"):
        save_state(desk, overrides=(invalid,))
    assert desk.path.read_bytes() == before
    valid = invalid.model_copy(update={"positions": ("PG",)})
    save_state(desk, overrides=(valid,), watch=(original.id,))
    boot = desk.bootstrap()
    assert boot.players[0].positions == ("PG",)
    assert desk.inputs.players[0] == original
    reset = save_state(desk, sales=(), overrides=(), watch=())
    assert reset.state.draft_id == initial.draft_id
    assert reset.state.mine == initial.mine and reset.state.teams == initial.teams
    assert desk.bootstrap().players[0] == original
    assert not reset.state.sales and not reset.state.watch and not reset.state.overrides


def test_completed_job_exposes_measured_duration_and_preserves_it_for_labels(desk):
    wait_for(lambda: desk.results().equal.status == "ready")
    before = desk.results().equal
    assert before.elapsed_ns is not None and before.elapsed_ns > 0
    save_state(desk, watch=("000",))
    after = desk.results().equal
    assert after.elapsed_ns == before.elapsed_ns
    assert after.result.state_sha256 == desk.bootstrap().desk.market.state_sha256
