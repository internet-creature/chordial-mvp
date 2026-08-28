"""the cycle spine (phase 5, ROOMS_DESIGN.md section 6).

covers the honesty invariants end to end:
- commitments evolve freely BEFORE the freeze; the baseline snapshot is
  written exactly once and never changes
- after the freeze, planned capacity moves only through explicit scope
  changes (resize/release/recapacity), each leaving a ledger row
- the projection = baseline + scope changes + progress, where progress is
  attributed from applied session.ended device events (task-first, then
  unambiguous plan match, never double-counted)
- the focus-flow processor turns focus_block.completed into exactly one of
  pip's observations, idempotently, and the whole flow works through the
  real sync endpoint (the vertical slice, server side)
"""
import asyncio
import sys
import tempfile
import uuid as uuid_mod
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from config import Config
from src.database.models import (
    Base,
    ConversationEvent,
    DeviceEvent,
    Observation,
    User,
)
from src.services import focus_flow
from src.services.cycles import CycleStore, CycleStoreError
from src.services.workspace import get_store
from src.web import device_auth
from src.web.server import WebService

U1 = "user-one"
U2 = "user-two"


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="megan", timezone="UTC"))
        s.add(User(uuid=U2, preferred_name="guest", timezone="UTC"))
        s.commit()
    yield TestSession
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


def _cycle(user=U1, **kw):
    today = date.today()
    defaults = dict(status="active",
                    start_date=(today - timedelta(days=3)).isoformat(),
                    end_date=(today + timedelta(days=10)).isoformat(),
                    theme="build rhythm", capacity_blocks=24)
    defaults.update(kw)
    return get_store().create_cycle(user, "cycle t", **defaults)


def _bank_event(env, user, device_pk, seq, *, task_id, seconds,
                event_type="session.ended", when=None, **extra):
    """a landed device event, as apply_events would leave it."""
    payload = {"task_id": task_id, "seconds": seconds, **extra}
    if event_type == "focus_block.completed":
        payload = {"task_id": task_id, "run_seconds": seconds,
                   "banked_seconds_today": seconds, **extra}
    with env() as s:
        s.add(DeviceEvent(
            event_uuid=str(uuid_mod.uuid4()), device_id=device_pk,
            user_uuid=user, seq=seq, event_type=event_type,
            payload=payload, occurred_at=when or datetime.utcnow(),
            rejected=False))
        s.commit()


def _device_pk(env, user=U1):
    code = device_auth.mint_device_link_code(user)
    device_uuid, token = device_auth.link_device(code, "test")
    from src.database.models import Device
    with env() as s:
        pk = s.query(Device.id).filter(
            Device.device_uuid == device_uuid).scalar()
    return pk, token


# --- commitments before the freeze ---------------------------------------------


def test_commitments_evolve_freely_before_freeze(env):
    cs = CycleStore()
    cyc = _cycle()
    row = cs.create_commitment(U1, cyc["id"], "room runtime v1",
                               priority="high", blocks_planned=5,
                               next_action="define the room model")
    assert row["public_id"].startswith("cm")
    assert row["status"] == "active"

    row = cs.update_commitment(U1, row["id"], blocks_planned=7,
                               next_action="write the schema")
    assert row["blocks_planned"] == 7

    row = cs.update_commitment(U1, row["id"], status="Released")
    assert row["status"] == "released"
    assert row["closed_at"] is not None


def test_commitment_tenancy(env):
    cs = CycleStore()
    cyc = _cycle()
    row = cs.create_commitment(U1, cyc["id"], "mine", blocks_planned=2)
    with pytest.raises(CycleStoreError):
        cs.update_commitment(U2, row["id"], blocks_planned=9)
    with pytest.raises(CycleStoreError):
        cs.create_commitment(U2, cyc["id"], "theirs")


def test_commitment_link_validation(env):
    cs = CycleStore()
    cyc = _cycle()
    with pytest.raises(CycleStoreError):
        cs.create_commitment(U1, cyc["id"], "ghost link", task_id=9999)
    other_task = get_store().create_task(U2, "not yours")
    with pytest.raises(CycleStoreError):
        cs.create_commitment(U1, cyc["id"], "cross-tenant link",
                             task_id=other_task["id"])


# --- the freeze -----------------------------------------------------------------


def test_freeze_snapshots_once_and_never_again(env):
    cs = CycleStore()
    cyc = _cycle()
    cs.create_commitment(U1, cyc["id"], "a", blocks_planned=5)
    cs.create_commitment(U1, cyc["id"], "b", blocks_planned=3)
    result = cs.freeze_baseline(U1, cyc["id"])
    assert result["commitments"] == 2

    with pytest.raises(CycleStoreError):
        cs.freeze_baseline(U1, cyc["id"])

    snap = cs.baseline(U1, cyc["id"])["snapshot"]
    assert [c["blocks_planned"] for c in snap["commitments"]] == [5, 3]
    assert snap["cycle"]["capacity_blocks"] == 24


def test_freeze_needs_commitments(env):
    cs = CycleStore()
    cyc = _cycle()
    with pytest.raises(CycleStoreError):
        cs.freeze_baseline(U1, cyc["id"])


def test_baseline_survives_live_row_evolution(env):
    """the live rows may change; the snapshot never does."""
    cs = CycleStore()
    cyc = _cycle()
    row = cs.create_commitment(U1, cyc["id"], "a", blocks_planned=5)
    cs.freeze_baseline(U1, cyc["id"])
    cs.change_scope(U1, reason="turned out bigger",
                    commitment_id=row["id"], blocks_planned=9)
    snap = cs.baseline(U1, cyc["id"])["snapshot"]
    assert snap["commitments"][0]["blocks_planned"] == 5
    view = cs.projection(U1, cyc["id"])
    c = view["commitments"][0]
    assert c["blocks_planned"] == 9 and c["baseline_blocks"] == 5


# --- post-freeze discipline ------------------------------------------------------


def test_frozen_capacity_only_moves_through_scope_changes(env):
    cs = CycleStore()
    cyc = _cycle()
    row = cs.create_commitment(U1, cyc["id"], "a", blocks_planned=5)
    cs.freeze_baseline(U1, cyc["id"])

    with pytest.raises(CycleStoreError):
        cs.update_commitment(U1, row["id"], blocks_planned=9)
    with pytest.raises(CycleStoreError):
        cs.update_commitment(U1, row["id"], status="Released")

    # completing is progress, not scope - always ordinary
    done = cs.update_commitment(U1, row["id"], status="Completed")
    assert done["status"] == "completed"


def test_scope_change_mutates_and_appends_atomically(env):
    cs = CycleStore()
    cyc = _cycle()
    row = cs.create_commitment(U1, cyc["id"], "a", blocks_planned=5)
    cs.freeze_baseline(U1, cyc["id"])

    cs.change_scope(U1, reason="the schema fought back",
                    commitment_id=row["id"], blocks_planned=8)
    changes = cs.list_scope_changes(U1, cyc["id"])
    assert len(changes) == 1
    assert changes[0]["deltas"]["blocks_planned"] == {"from": 5, "to": 8}

    cs.change_scope(U1, reason="deprioritized for launch",
                    commitment_id=row["id"], release=True)
    view = cs.projection(U1, cyc["id"])
    assert view["commitments"][0]["status"] == "released"
    assert view["totals"]["planned_blocks"] == 0   # released stops counting
    assert len(view["scope_changes"]) == 2


def test_scope_change_requires_reason(env):
    cs = CycleStore()
    cyc = _cycle()
    row = cs.create_commitment(U1, cyc["id"], "a", blocks_planned=5)
    with pytest.raises(CycleStoreError):
        cs.change_scope(U1, reason="  ", commitment_id=row["id"],
                        blocks_planned=2)


def test_adding_to_a_frozen_cycle_is_a_scope_change(env):
    cs = CycleStore()
    cyc = _cycle()
    cs.create_commitment(U1, cyc["id"], "a", blocks_planned=5)
    cs.freeze_baseline(U1, cyc["id"])

    with pytest.raises(CycleStoreError):
        cs.create_commitment(U1, cyc["id"], "surprise", blocks_planned=3)

    row = cs.create_commitment(U1, cyc["id"], "surprise", blocks_planned=3,
                               reason="a real deadline appeared")
    changes = cs.list_scope_changes(U1, cyc["id"])
    assert changes[-1]["deltas"]["action"] == "add"
    assert changes[-1]["commitment_uuid"] == row["uuid"]
    # not in the baseline, so no baseline_blocks
    view = cs.projection(U1, cyc["id"])
    added = [c for c in view["commitments"] if c["title"] == "surprise"][0]
    assert added["baseline_blocks"] is None


def test_every_door_respects_the_frozen_capacity(env):
    """the workspace store's general update path is a door too: after the
    freeze, capacity_blocks must bounce to change_scope; theme and title
    stay freely editable (words, not capacity)."""
    cs = CycleStore()
    ws = get_store()
    cyc = _cycle()
    cs.create_commitment(U1, cyc["id"], "a", blocks_planned=5)
    cs.freeze_baseline(U1, cyc["id"])

    with pytest.raises(ValueError, match="scope change"):
        ws.update_cycle(U1, cyc["id"], capacity_blocks=99)
    row = ws.update_cycle(U1, cyc["id"], theme="renamed theme")
    assert row["theme"] == "renamed theme"
    assert row["capacity_blocks"] == 24

    # pre-freeze the same edit is ordinary
    cyc2 = get_store().create_cycle(U1, "next cycle", capacity_blocks=10)
    assert ws.update_cycle(U1, cyc2["id"],
                           capacity_blocks=12)["capacity_blocks"] == 12


def test_cycle_capacity_scope_change(env):
    cs = CycleStore()
    cyc = _cycle()
    cs.create_commitment(U1, cyc["id"], "a", blocks_planned=5)
    cs.freeze_baseline(U1, cyc["id"])
    cs.change_scope(U1, reason="the week collapsed", cycle_id=cyc["id"],
                    capacity_blocks=16)
    view = cs.projection(U1, cyc["id"])
    assert view["cycle"]["capacity_blocks"] == 16
    assert view["totals"]["unallocated_blocks"] == 11
    # the baseline remembers the original ambition
    snap = CycleStore().baseline(U1, cyc["id"])["snapshot"]
    assert snap["cycle"]["capacity_blocks"] == 24


# --- progress attribution ---------------------------------------------------------


def test_progress_attributes_task_first_then_unique_plan(env):
    cs = CycleStore()
    ws = get_store()
    cyc = _cycle()
    plan = ws.create_plan(U1, "the album", "juniper", status="active")
    tracked = ws.create_task(U1, "tracked task")
    plan_task = ws.create_task(U1, "plan task", plan_id=plan["id"])

    direct = cs.create_commitment(U1, cyc["id"], "direct",
                                  blocks_planned=4, task_id=tracked["id"])
    via_plan = cs.create_commitment(U1, cyc["id"], "via plan",
                                    blocks_planned=4, plan_id=plan["id"])

    pk, _ = _device_pk(env)
    _bank_event(env, U1, pk, 1, task_id=tracked["id"], seconds=1500)
    _bank_event(env, U1, pk, 2, task_id=tracked["id"], seconds=750)
    _bank_event(env, U1, pk, 3, task_id=plan_task["id"], seconds=3000)
    _bank_event(env, U1, pk, 4, task_id=None, seconds=600)      # untasked

    view = cs.projection(U1, cyc["id"])
    by_title = {c["title"]: c for c in view["commitments"]}
    assert by_title["direct"]["seconds_done"] == 2250
    assert by_title["direct"]["blocks_done"] == round(
        2250 / (Config.POM_MINUTES * 60), 1)
    assert by_title["via plan"]["seconds_done"] == 3000
    assert view["totals"]["unattributed_seconds"] == 600


def test_ambiguous_plan_match_never_double_counts(env):
    cs = CycleStore()
    ws = get_store()
    cyc = _cycle()
    plan = ws.create_plan(U1, "shared plan", "pip", status="active")
    task = ws.create_task(U1, "shared task", plan_id=plan["id"])
    cs.create_commitment(U1, cyc["id"], "one", plan_id=plan["id"])
    cs.create_commitment(U1, cyc["id"], "two", plan_id=plan["id"])

    pk, _ = _device_pk(env)
    _bank_event(env, U1, pk, 1, task_id=task["id"], seconds=1500)

    view = cs.projection(U1, cyc["id"])
    assert all(c["seconds_done"] == 0 for c in view["commitments"])
    assert view["totals"]["unattributed_seconds"] == 1500


def test_duplicate_task_links_are_ambiguous_not_arbitrary(env):
    """two commitments anchoring the same task: the minutes go to
    unattributed, never to whichever row a dict happened to keep last -
    and an ambiguous task match never launders through the plan fallback."""
    cs = CycleStore()
    ws = get_store()
    cyc = _cycle()
    plan = ws.create_plan(U1, "the plan", "pip", status="active")
    task = ws.create_task(U1, "contested task", plan_id=plan["id"])
    cs.create_commitment(U1, cyc["id"], "claimant one", task_id=task["id"])
    cs.create_commitment(U1, cyc["id"], "claimant two", task_id=task["id"],
                         plan_id=plan["id"])

    pk, _ = _device_pk(env)
    _bank_event(env, U1, pk, 1, task_id=task["id"], seconds=1500)

    view = cs.projection(U1, cyc["id"])
    assert all(c["seconds_done"] == 0 for c in view["commitments"])
    assert view["totals"]["unattributed_seconds"] == 1500


def test_progress_respects_the_cycle_window(env):
    cs = CycleStore()
    ws = get_store()
    cyc = _cycle()
    task = ws.create_task(U1, "windowed")
    cs.create_commitment(U1, cyc["id"], "windowed", task_id=task["id"])

    pk, _ = _device_pk(env)
    _bank_event(env, U1, pk, 1, task_id=task["id"], seconds=1500)
    _bank_event(env, U1, pk, 2, task_id=task["id"], seconds=9999,
                when=datetime.utcnow() - timedelta(days=30))

    view = cs.projection(U1, cyc["id"])
    assert view["commitments"][0]["seconds_done"] == 1500


def test_progress_is_tenant_scoped(env):
    cs = CycleStore()
    ws = get_store()
    cyc = _cycle()
    task = ws.create_task(U1, "mine")
    cs.create_commitment(U1, cyc["id"], "mine", task_id=task["id"])

    pk2, _ = _device_pk(env, U2)
    _bank_event(env, U2, pk2, 1, task_id=task["id"], seconds=1500)

    view = cs.projection(U1, cyc["id"])
    assert view["commitments"][0]["seconds_done"] == 0


def test_projection_defaults_to_the_active_cycle(env):
    cs = CycleStore()
    assert cs.projection(U1) is None
    cyc = _cycle()
    cs.create_commitment(U1, cyc["id"], "a")
    assert cs.projection(U1)["cycle"]["id"] == cyc["id"]


# --- the focus-flow processor -----------------------------------------------------


def test_completed_block_becomes_one_pip_observation(env):
    ws = get_store()
    task = ws.create_task(U1, "bounce stems")
    pk, _ = _device_pk(env)
    _bank_event(env, U1, pk, 1, task_id=task["id"], seconds=1620,
                event_type="focus_block.completed", label="bounce stems")

    assert focus_flow.process_pending(U1) == 1
    with env() as s:
        obs = s.query(Observation).all()
        assert len(obs) == 1
        assert obs[0].helper_id == "pip"
        assert obs[0].kind == "progress"
        assert "bounce stems" in obs[0].content
        assert obs[0].evidence["run_seconds"] == 1620
        event = s.query(DeviceEvent).one()
        assert event.processed_at is not None
        assert obs[0].evidence["event_uuid"] == event.event_uuid

    # idempotent: nothing left to process, no second observation
    assert focus_flow.process_pending(U1) == 0
    with env() as s:
        assert s.query(Observation).count() == 1


def test_completed_block_is_presence_for_the_ladder(env):
    """a landed block also writes ONE user-authored action event: showing
    up by doing counts as showing up, so the outreach cadence resets for
    someone banking blocks all week without chatting. same savepoint as
    the observation, so the source_event_uuid floor guards both. the row
    carries occurred_at - when the block LANDED - so an offline backlog
    synced later reads as history, never as presence just now."""
    landed = datetime(2026, 6, 10, 9, 0)
    pk, _ = _device_pk(env)
    _bank_event(env, U1, pk, 1, task_id=None, seconds=1620,
                event_type="focus_block.completed", label="bounce stems",
                when=landed)

    assert focus_flow.process_pending(U1) == 1
    with env() as s:
        rows = s.query(ConversationEvent).all()
        assert len(rows) == 1
        assert rows[0].author_type == "user"
        assert rows[0].kind == "action"
        assert rows[0].message_type is None
        assert "bounce stems" in rows[0].content
        assert "27 min" in rows[0].content
        assert rows[0].created_at == landed
        event = s.query(DeviceEvent).one()
        assert rows[0].event_metadata["source_event_uuid"] == event.event_uuid

    # idempotent alongside the observation
    assert focus_flow.process_pending(U1) == 0
    with env() as s:
        assert s.query(ConversationEvent).count() == 1


def test_processor_stamps_non_consequence_events(env):
    pk, _ = _device_pk(env)
    _bank_event(env, U1, pk, 1, task_id=None, seconds=300)   # session.ended
    assert focus_flow.process_pending(U1) == 1
    with env() as s:
        assert s.query(Observation).count() == 0
        assert s.query(DeviceEvent).one().processed_at is not None


def test_processor_survives_a_malformed_payload(env):
    pk, _ = _device_pk(env)
    with env() as s:
        s.add(DeviceEvent(
            event_uuid=str(uuid_mod.uuid4()), device_id=pk, user_uuid=U1,
            seq=1, event_type="focus_block.completed", payload=None,
            rejected=False))
        s.commit()
    assert focus_flow.process_pending(U1) == 1
    with env() as s:
        assert s.query(DeviceEvent).one().processed_at is not None


def test_drain_processes_past_one_batch_limit(env):
    """a batch bigger than one pass's limit must not strand its tail -
    drain loops until the queue is empty."""
    ws = get_store()
    task = ws.create_task(U1, "many blocks")
    pk, _ = _device_pk(env)
    for seq in range(1, 4):
        _bank_event(env, U1, pk, seq, task_id=task["id"], seconds=1500,
                    event_type="focus_block.completed", label="many blocks")
    assert focus_flow.drain(U1, limit=1) == 3
    with env() as s:
        assert s.query(Observation).count() == 3
        assert all(e.processed_at is not None
                   for e in s.query(DeviceEvent).all())


def test_one_fact_never_becomes_two_noticings(env):
    """the unique source_event_uuid floor: even if the processed_at claim
    is ever lost or reset (the concurrency window sol reproduced), a
    second pass cannot insert a second observation for the same event."""
    ws = get_store()
    task = ws.create_task(U1, "raced block")
    pk, _ = _device_pk(env)
    _bank_event(env, U1, pk, 1, task_id=task["id"], seconds=1500,
                event_type="focus_block.completed", label="raced block")
    assert focus_flow.process_pending(U1) == 1

    # simulate the lost claim: the stamp vanishes, the observation stays
    with env() as s:
        s.query(DeviceEvent).update({DeviceEvent.processed_at: None})
        s.commit()

    assert focus_flow.process_pending(U1) == 1   # row seen again
    with env() as s:
        assert s.query(Observation).count() == 1     # but only one noticing
        assert s.query(DeviceEvent).one().processed_at is not None


def test_processor_skips_rejected_rows(env):
    pk, _ = _device_pk(env)
    with env() as s:
        s.add(DeviceEvent(
            event_uuid=str(uuid_mod.uuid4()), device_id=pk, user_uuid=U1,
            seq=1, event_type="focus_block.completed", payload={},
            rejected=True, error="unknown event type"))
        s.commit()
    assert focus_flow.process_pending(U1) == 0
    with env() as s:
        assert s.query(Observation).count() == 0


# --- the read model + the slice through the real endpoint --------------------------


def _service():
    return WebService(user_resolver=lambda: U1)


async def _with_service(service, fn):
    client = TestClient(TestServer(service.build_app()))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


def test_cycle_view_requires_a_device_token(env):
    async def flow(client):
        resp = await client.get("/api/v1/cycle")
        assert resp.status == 401
    _run(_with_service(_service(), flow))


def test_cycle_view_shape_and_no_cycle_case(env):
    cs = CycleStore()
    _, token = _device_pk(env)

    async def flow(client):
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get("/api/v1/cycle", headers=headers)
        assert resp.status == 200
        assert (await resp.json())["cycle"] is None

        cyc = _cycle()
        cs.create_commitment(U1, cyc["id"], "room runtime v1",
                             blocks_planned=5,
                             next_action="define the room model")
        cs.freeze_baseline(U1, cyc["id"])
        resp = await client.get("/api/v1/cycle", headers=headers)
        body = await resp.json()
        assert body["cycle"]["theme"] == "build rhythm"
        assert body["frozen"] is True
        assert body["commitments"][0]["next_action"] == \
            "define the room model"
        assert body["totals"]["capacity_blocks"] == 24
    _run(_with_service(_service(), flow))


def test_the_vertical_slice_server_side(env):
    """a block completed on a device flows sync -> pip observation ->
    the cycle view moves. the milestone's spine, minus the pixels."""
    cs = CycleStore()
    ws = get_store()
    cyc = _cycle()
    task = ws.create_task(U1, "bounce stems")
    cs.create_commitment(U1, cyc["id"], "mix track one", blocks_planned=6,
                         task_id=task["id"],
                         next_action="bounce stems (1 block)")
    cs.freeze_baseline(U1, cyc["id"])
    _, token = _device_pk(env)

    async def flow(client):
        headers = {"Authorization": f"Bearer {token}"}
        batch = {"events": [
            {"id": str(uuid_mod.uuid4()), "seq": 1, "type": "session.ended",
             "payload": {"task_id": task["id"], "label": "bounce stems",
                         "seconds": 1500, "reason": "finished"},
             "occurred_at": datetime.utcnow().isoformat()},
            {"id": str(uuid_mod.uuid4()), "seq": 2,
             "type": "focus_block.completed",
             "payload": {"task_id": task["id"], "label": "bounce stems",
                         "run_seconds": 1500,
                         "banked_seconds_today": 1500},
             "occurred_at": datetime.utcnow().isoformat()},
        ]}
        resp = await client.post("/api/v1/sync/events", headers=headers,
                                 json=batch)
        assert resp.status == 200
        assert (await resp.json())["acked_seq"] == 2

        resp = await client.get("/api/v1/cycle", headers=headers)
        body = await resp.json()
        c = body["commitments"][0]
        assert c["seconds_done"] == 1500
        assert c["blocks_done"] == round(1500 / (Config.POM_MINUTES * 60), 1)
    _run(_with_service(_service(), flow))

    with env() as s:
        obs = s.query(Observation).filter(
            Observation.helper_id == "pip").all()
        assert len(obs) == 1
        assert "bounce stems" in obs[0].content
        assert all(e.processed_at is not None
                   for e in s.query(DeviceEvent).all())
