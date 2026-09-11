from __future__ import annotations

import sqlite3

import pytest

from socagents.core.errors import InvalidTransition, SocAgentsError
from socagents.db.store import Store
from socagents.runtime.states import RUN_TRANSITIONS, RunStatus, check_run_transition

ALLOWED = [(src, dst) for src, targets in RUN_TRANSITIONS.items() for dst in targets]
DENIED = [(src, dst) for src in RunStatus for dst in RunStatus if dst not in RUN_TRANSITIONS[src]]


@pytest.mark.parametrize(("src", "dst"), ALLOWED)
def test_allowed_transitions(src: RunStatus, dst: RunStatus) -> None:
    check_run_transition(src, dst)


@pytest.mark.parametrize(("src", "dst"), DENIED)
def test_denied_transitions(src: RunStatus, dst: RunStatus) -> None:
    with pytest.raises(InvalidTransition):
        check_run_transition(src, dst)


def test_waiting_approval_resumes_running() -> None:
    assert RunStatus.RUNNING in RUN_TRANSITIONS[RunStatus.WAITING_APPROVAL]


def test_run_lifecycle(store: Store) -> None:
    run_id = store.create_run(kind="ask", mode="community", model="fixture/scripted", input={})
    assert store.get_run(run_id)["status"] == "queued"
    assert store.get_run(run_id)["agent_id"] is None

    store.transition_run(run_id, RunStatus.RUNNING)
    assert store.get_run(run_id)["started_at"] is not None
    store.transition_run(run_id, RunStatus.COMPLETED, output={"answer": "x"})
    row = store.get_run(run_id)
    assert row["finished_at"] is not None
    assert row["output"] == '{"answer":"x"}'

    with pytest.raises(InvalidTransition):
        store.transition_run(run_id, RunStatus.RUNNING)


def test_unknown_run(store: Store) -> None:
    with pytest.raises(SocAgentsError):
        store.transition_run("run_missing", RunStatus.RUNNING)


def test_event_sequence(store: Store) -> None:
    run_id = store.create_run(kind="ask", mode="community", model="m", input={})
    assert store.add_event(run_id, "a", {}) == 1
    assert store.add_event(run_id, "b", {"k": 1}) == 2
    assert [e["type"] for e in store.list_events(run_id)] == ["a", "b"]


def test_latest_checkpoint(store: Store) -> None:
    run_id = store.create_run(kind="ask", mode="community", model="m", input={})
    assert store.latest_checkpoint(run_id) is None
    store.save_checkpoint(run_id, 0, {"step": 0})
    store.save_checkpoint(run_id, 1, {"step": 1})
    assert store.latest_checkpoint(run_id) == {"step": 1}


def test_snapshots_are_immutable(store: Store) -> None:
    snapshot_id = store.save_snapshot(
        run_id=None,
        tool="get_quote",
        args={},
        payload={"a": 1},
        source="s",
        as_of="2026-09-10T17:45:00Z",
        delayed_sec=0,
        mode="community",
        trust="trusted",
    )
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        store._db.execute("UPDATE data_snapshots SET payload = '{}' WHERE id = ?", (snapshot_id,))


def test_audit_is_append_only(store: Store) -> None:
    store.audit(actor="user", action="config.changed", detail={"k": "v"})
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._db.execute("DELETE FROM audit_events")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._db.execute("UPDATE audit_events SET action = 'x'")


def test_store_creates_database_file(tmp_path) -> None:
    path = tmp_path / "nested" / "socagents.db"
    Store(path).close()
    assert path.is_file()
