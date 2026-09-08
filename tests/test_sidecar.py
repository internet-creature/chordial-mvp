"""the sidecar: local store, the task-clock focus model, authored lines, and
the outbox pump's round trip into the real sync contract (phase 4b of the
rooms overhaul - the deer's machinery).

the clock is injected everywhere: tests move time, they don't sleep
through it.
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.models import Base, DeviceEvent, User
from src.sidecar.focus import FocusEngine, FocusError
from src.sidecar.lines import LINE_POOLS, pick_line
from src.sidecar.server import SidecarService
from src.sidecar.store import SidecarStore
from src.sidecar.sync import OutboxPump
from src.web import device_auth
from src.web.server import WebService

U1 = "user-one"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path):
    s = SidecarStore(tmp_path / "sidecar.db")
    yield s
    s.close()


class Clock:
    """a hand-cranked clock."""

    def __init__(self):
        self.now = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


# --- the store ------------------------------------------------------------


def test_outbox_seq_is_monotonic_across_trims(store):
    store.enqueue("session.started", {"n": 1})
    store.enqueue("focus_block.completed", {"n": 2})
    store.trim(2)
    assert store.pending() == []
    seq = store.enqueue("session.started", {"n": 3})
    # AUTOINCREMENT never reuses a seq - the server's (device, seq)
    # uniqueness depends on it
    assert seq == 3


def test_outbox_rebase_renumbers_from_one(store):
    store.enqueue("session.started", {"n": 1})
    store.enqueue("focus_block.completed", {"n": 2})
    store.trim(1)                       # first event acked under old device
    moved = store.rebase_outbox()
    assert moved == 1
    pending = store.pending()
    assert [e["seq"] for e in pending] == [1]
    assert pending[0]["payload"] == {"n": 2}
    # and the sequence really restarts - the next event is seq 2, not 4
    assert store.enqueue("session.started", {"n": 3}) == 2


def test_banked_since_groups_by_task(store):
    store.insert_run(7, "novel", 25, "2026-08-12T14:00:00+00:00")
    store.close_run(1, "2026-08-12T14:10:00+00:00", 600, "paused")
    store.insert_run(7, "novel", 25, "2026-08-12T15:00:00+00:00")
    store.close_run(2, "2026-08-12T15:05:00+00:00", 300, "switched")
    store.insert_run(9, "stems", 25, "2026-08-12T15:05:00+00:00")
    store.close_run(3, "2026-08-12T15:25:00+00:00", 1200, "finished")
    store.insert_run(3, "old", 25, "2026-08-11T09:00:00+00:00")   # yesterday
    store.close_run(4, "2026-08-11T09:30:00+00:00", 1800, "paused")

    banked = store.banked_since("2026-08-12T00:00:00+00:00")
    assert banked == {7: 900, 9: 1200}


def test_align_seq_lifts_numbering_above_the_cursor(store):
    store.enqueue("session.started", {"n": 1})
    store.enqueue("session.ended", {"n": 2})
    moved = store.align_seq(6)
    assert moved == 2
    assert [e["seq"] for e in store.pending()] == [7, 8]
    assert store.enqueue("focus_block.completed", {"n": 3}) == 9


def test_align_seq_is_a_noop_when_already_above(store):
    store.enqueue("session.started", {"n": 1})
    store.trim(1)
    store.enqueue("session.started", {"n": 2})     # seq 2
    assert store.align_seq(1) == 0                  # pending already above
    assert [e["seq"] for e in store.pending()] == [2]


# --- the focus engine (the task-clock model) ---------------------------------


def test_switch_banks_the_previous_run(store):
    clock = Clock()
    engine = FocusEngine(store, clock=clock)
    engine.start(task_id=7, label="novel", target_minutes=25)
    clock.advance(minutes=10)
    state = engine.start(task_id=9, label="stems", target_minutes=25)

    assert state["running"] is True
    assert state["task_id"] == 9
    assert state["banked"][7] == 600           # ten minutes, kept
    types = [e["type"] for e in store.pending()]
    assert types == ["session.started", "session.ended", "session.started"]
    # the target rides on the start event (the day digest reads it)
    assert store.pending()[0]["payload"] == {"task_id": 7, "label": "novel",
                                             "target_minutes": 25.0}
    ended = store.pending()[1]
    assert ended["payload"]["reason"] == "switched"
    assert ended["payload"]["seconds"] == 600


def test_no_auto_finish_the_clock_counts_overtime(store):
    clock = Clock()
    engine = FocusEngine(store, clock=clock)
    engine.start(task_id=7, label="novel", target_minutes=25)

    assert engine.crossed_target() is False
    clock.advance(minutes=26)
    # the run is STILL going - crossing the target ends nothing
    state = engine.state()
    assert state["running"] is True
    assert state["over_target"] is True
    assert state["run_seconds"] == 26 * 60
    # the ding fires exactly once
    assert engine.crossed_target() is True
    assert engine.crossed_target() is False


def test_pause_banks_and_is_never_a_verdict(store):
    clock = Clock()
    engine = FocusEngine(store, clock=clock)
    assert engine.pause() is None              # idle: nothing to pause
    engine.start(task_id=7, label="novel")
    clock.advance(minutes=10)
    run = engine.pause()
    assert run["reason"] == "paused"
    assert run["seconds"] == 600
    assert engine.state()["running"] is False
    assert engine.state()["banked"][7] == 600
    # resume: same task, new run, bank keeps growing
    engine.start(task_id=7, label="novel")
    clock.advance(minutes=5)
    engine.pause()
    assert engine.state()["banked"][7] == 900


def test_finish_emits_the_celebration_event_with_the_days_bank(store):
    clock = Clock()
    engine = FocusEngine(store, clock=clock)
    engine.start(task_id=7, label="novel")
    clock.advance(minutes=10)
    engine.pause()
    engine.start(task_id=7, label="novel")
    clock.advance(minutes=15)
    summary = engine.finish()
    assert summary["reason"] == "finished"

    done = store.pending()[-1]
    assert done["type"] == "focus_block.completed"
    assert done["payload"]["task_id"] == 7
    assert done["payload"]["run_seconds"] == 15 * 60
    assert done["payload"]["banked_seconds_today"] == 25 * 60

    with pytest.raises(FocusError):
        engine.finish()                        # landing needs a running clock


def test_start_guards(store):
    clock = Clock()
    engine = FocusEngine(store, clock=clock)
    for bad in (0, -5, 500, "25", True):
        with pytest.raises(FocusError):
            engine.start(target_minutes=bad)
    with pytest.raises(FocusError):
        engine.start(task_id=0)
    engine.start(task_id=7, label="novel")
    with pytest.raises(FocusError):
        engine.start(task_id=7)                # that clock is already running


def test_run_survives_a_sidecar_restart(store):
    clock = Clock()
    FocusEngine(store, clock=clock).start(task_id=7, label="resumable")
    clock.advance(minutes=30)
    # a fresh engine over the same store IS the restarted sidecar: the run
    # is still going, deep in overtime, and the ding can still fire
    engine = FocusEngine(store, clock=clock)
    state = engine.state()
    assert state["running"] is True
    assert state["run_seconds"] == 30 * 60
    assert state["over_target"] is True
    assert engine.crossed_target() is True


# --- the gate stack ------------------------------------------------------------


def test_budget_caps_unprompted_lines_per_rolling_hour():
    from src.sidecar.gates import SpeechGates
    clock = Clock()
    gates = SpeechGates(clock=clock, lines_per_hour=2)
    assert gates.allow(blocked=False); gates.spend()
    assert gates.allow(blocked=False); gates.spend()
    assert gates.allow(blocked=False) is False        # budget spent
    clock.advance(minutes=61)
    assert gates.allow(blocked=False) is True         # window rolled


def test_blocked_hushes_everything():
    from src.sidecar.gates import SpeechGates
    gates = SpeechGates(clock=Clock(), lines_per_hour=10)
    assert gates.allow(blocked=True) is False


def test_quiet_hours_parse_and_wrap():
    from src.sidecar.gates import in_quiet_hours, parse_quiet_hours
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours("nope") is None
    assert parse_quiet_hours("25-3") is None
    assert parse_quiet_hours("9-9") is None            # all-day = malformed
    hours = parse_quiet_hours("22-8")                  # wraps midnight
    assert in_quiet_hours(hours, 23) is True
    assert in_quiet_hours(hours, 3) is True
    assert in_quiet_hours(hours, 12) is False
    daytime = parse_quiet_hours("13-15")
    assert in_quiet_hours(daytime, 13) is True
    assert in_quiet_hours(daytime, 15) is False
    assert in_quiet_hours(None, 3) is False


# --- activity & drift ------------------------------------------------------------


def _drift_rig(store, clock, threshold_minutes=5):
    from src.sidecar.drift import ActivityState, DriftWatch
    activity = ActivityState(clock=clock)
    watch = DriftWatch(store, activity, clock=clock,
                       threshold_minutes=threshold_minutes)
    return activity, watch


def test_drift_episode_speaks_once_then_trusts(store):
    clock = Clock()
    activity, watch = _drift_rig(store, clock)

    activity.observe("com.apple.dt.Xcode", 0)
    assert watch.tick(run_active=True) is None

    # the person goes quiet; the collector keeps heartbeating with a
    # growing idle (a fresh sample every ~10s is the production shape)
    clock.advance(seconds=290)
    activity.observe("com.apple.dt.Xcode", 290)
    assert watch.tick(run_active=True) is None         # not quite yet
    clock.advance(seconds=11)
    activity.observe("com.apple.dt.Xcode", 301)
    assert watch.tick(run_active=True) == "drift_checkin"
    assert watch.drifting is True
    # ...and holds its tongue for the rest of the episode
    clock.advance(minutes=10)
    activity.observe("com.apple.dt.Xcode", 900)
    assert watch.tick(run_active=True) is None

    # the person returns
    activity.observe("com.apple.dt.Xcode", 1)
    assert watch.tick(run_active=True) == "drift_return"
    assert watch.drifting is False
    assert watch.tick(run_active=True) is None

    events = [e["type"] for e in store.pending()]
    assert events == ["drift.detected", "return.detected"]
    returned = store.pending()[-1]
    assert returned["payload"]["away_seconds"] >= 15 * 60


def test_drift_disarms_without_a_run_and_with_stale_signals(store):
    clock = Clock()
    activity, watch = _drift_rig(store, clock)
    activity.observe(None, 600)
    assert watch.tick(run_active=False) is None        # no block, no watch
    # a dead collector must not fake a drift: staleness disarms
    clock.advance(minutes=5)
    assert activity.fresh is False
    assert watch.tick(run_active=True) is None
    assert store.pending() == []


def test_blocklisted_app_reads_blocked_only_while_fresh(store):
    clock = Clock()
    activity, _ = _drift_rig(store, clock)
    activity.observe("us.zoom.xos", 0)
    assert activity.blocked is True
    assert "bundle" not in str(activity.snapshot())    # ids never surface
    clock.advance(minutes=2)
    assert activity.blocked is False                   # stale = not trusted


# --- lines -------------------------------------------------------------------


def test_lines_never_repeat_and_unknown_moments_are_silent():
    last = pick_line("task_finished")
    for _ in range(50):
        line = pick_line("task_finished", last=last)
        assert line != last
        assert line in LINE_POOLS["task_finished"]
        last = line
    assert pick_line("no_such_moment") == ""


# --- the outbox pump: a real round trip into the sync contract ---------------


@pytest.fixture()
def server_env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="megan", timezone="UTC"))
        s.commit()
    yield TestSession
    engine.dispose()


def test_pump_round_trip_acks_and_trims(server_env, store):
    async def flow():
        web_service = WebService(user_resolver=lambda: U1)
        client = TestClient(TestServer(web_service.build_app()))
        await client.start_server()
        try:
            code = device_auth.mint_device_link_code(U1)
            _device_uuid, token = device_auth.link_device(code, "deer-test")
            store.put("device_token", token)
            store.put("server_url", str(client.make_url("")).rstrip("/"))

            store.enqueue("focus_block.completed",
                          {"task_id": 7, "label": "novel",
                           "run_seconds": 1500,
                           "banked_seconds_today": 1500})
            store.enqueue("not.a.real.type", {})   # tombstones, never pins

            pump = OutboxPump(store)
            try:
                result = await pump.push_once()
            finally:
                await pump.close()

            assert result["acked_seq"] == 2
            assert result["applied"] == 1
            assert len(result["rejected"]) == 1
            assert store.pending() == []           # trimmed at the cursor

            with db_mod.SessionLocal() as s:
                rows = s.query(DeviceEvent).order_by(DeviceEvent.seq).all()
                assert [r.rejected for r in rows] == [False, True]
                assert rows[0].event_type == "focus_block.completed"
                assert rows[0].payload["label"] == "novel"
        finally:
            await client.close()
    _run(flow())


def test_fresh_store_under_an_existing_device_loses_nothing(server_env, store,
                                                            tmp_path):
    """the wiped-sidecar hazard, reproduced then prevented: the device's
    server cursor is ahead, a fresh store restarts numbering at seq 1, and
    without cursor alignment every event would dedupe as already-applied -
    acked, trimmed, silently lost."""
    async def flow():
        web_service = WebService(user_resolver=lambda: U1)
        client = TestClient(TestServer(web_service.build_app()))
        await client.start_server()
        try:
            code = device_auth.mint_device_link_code(U1)
            _uuid, token = device_auth.link_device(code, "wiped-later")
            server_url = str(client.make_url("")).rstrip("/")

            # first life: two events land, cursor moves to 2
            store.put("device_token", token)
            store.put("server_url", server_url)
            store.enqueue("session.started", {"life": 1})
            store.enqueue("session.ended", {"life": 1, "seconds": 60})
            pump = OutboxPump(store)
            first = await pump.push_once()
            await pump.close()
            assert first["acked_seq"] == 2

            # second life: the sidecar db was wiped; numbering restarts at 1
            fresh = SidecarStore(tmp_path / "sidecar-reborn.db")
            try:
                fresh.put("device_token", token)
                fresh.put("server_url", server_url)
                fresh.enqueue("focus_block.completed",
                              {"task_id": 7, "life": 2})
                pump = OutboxPump(fresh)
                result = await pump.push_once()
                await pump.close()

                # aligned above the cursor: APPLIED, not swallowed
                assert result["applied"] == 1
                assert result["duplicates"] == 0
                assert result["acked_seq"] == 3
                assert fresh.pending() == []
                with db_mod.SessionLocal() as s:
                    newest = s.query(DeviceEvent).order_by(
                        DeviceEvent.seq.desc()).first()
                    assert newest.event_type == "focus_block.completed"
                    assert newest.payload["life"] == 2
                    assert newest.rejected is False
            finally:
                fresh.close()
        finally:
            await client.close()
    _run(flow())


def test_pump_parks_on_revoked_device(server_env, store):
    async def flow():
        web_service = WebService(user_resolver=lambda: U1)
        client = TestClient(TestServer(web_service.build_app()))
        await client.start_server()
        try:
            code = device_auth.mint_device_link_code(U1)
            device_uuid, token = device_auth.link_device(code, "revoked")
            device_auth.revoke_device(U1, device_uuid)
            store.put("device_token", token)
            store.put("server_url", str(client.make_url("")).rstrip("/"))
            store.enqueue("focus_block.completed", {})

            pump = OutboxPump(store)
            try:
                assert await pump.push_once() is None
            finally:
                await pump.close()
            assert store.get("sync_error")
            assert store.pending() != []           # nothing lost, just parked
        finally:
            await client.close()
    _run(flow())


# --- the sidecar's own api -----------------------------------------------------


def _sidecar_client_flow(store, clock, fn):
    async def flow():
        service = SidecarService(store,
                                 engine=FocusEngine(store, clock=clock))
        client = TestClient(TestServer(service.build_app()))
        await client.start_server()
        try:
            await fn(client, service)
        finally:
            await client.close()
    _run(flow())


def test_sidecar_focus_flow_and_ws_push(store):
    clock = Clock()

    async def flow(client, _service):
        ws = await client.ws_connect("/v1/ws")
        hello = await ws.receive_json()
        assert hello["type"] == "state"
        assert hello["focus"]["running"] is False

        resp = await client.post("/v1/focus/start",
                                 json={"task_id": 7, "label": "novel"})
        assert resp.status == 200
        body = await resp.json()
        assert body["focus"]["task_id"] == 7
        assert body["line"] in LINE_POOLS["focus_start"]

        line = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert line["type"] == "line" and line["moment"] == "focus_start"
        state = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert state["focus"]["running"] is True

        # clicking another task IS the switch - and its line says so
        clock.advance(minutes=10)
        resp = await client.post("/v1/focus/start",
                                 json={"task_id": 9, "label": "stems"})
        body = await resp.json()
        assert body["focus"]["task_id"] == 9
        assert body["focus"]["banked"]["7"] == 600
        assert body["line"] in LINE_POOLS["focus_switch"]

        resp = await client.post("/v1/focus/pause")
        body = await resp.json()
        assert body["run"]["reason"] == "paused"
        assert body["line"] in LINE_POOLS["focus_pause"]
        assert (await client.post("/v1/focus/pause")).status == 409

        assert (await client.post("/v1/focus/finish")).status == 409
        await client.post("/v1/focus/start",
                          json={"task_id": 9, "label": "stems"})
        clock.advance(minutes=25)
        resp = await client.post("/v1/focus/finish")
        body = await resp.json()
        assert body["run"]["reason"] == "finished"
        assert body["line"] in LINE_POOLS["task_finished"]
        assert store.pending()[-1]["type"] == "focus_block.completed"
        await ws.close()
    _sidecar_client_flow(store, clock, flow)


def test_sidecar_handshake_rebases_on_new_device(store):
    clock = Clock()
    store.enqueue("focus_block.completed", {"n": 1})
    store.trim(1)
    store.enqueue("focus_block.completed", {"n": 2})   # pending at seq 2

    async def flow(client, _service):
        bad = await client.post("/v1/handshake", json={"token": "nodot"})
        assert bad.status == 400

        resp = await client.post("/v1/handshake", json={
            "token": "device-a.secret", "server_url": "http://localhost:1"})
        assert resp.status == 200
        assert store.pending()[0]["seq"] == 2          # same device: untouched

        store.put("sync_error", "device token rejected (401)")
        resp = await client.post("/v1/handshake", json={
            "token": "device-b.secret", "server_url": "http://localhost:1"})
        assert resp.status == 200
        assert store.pending()[0]["seq"] == 1          # new device: rebased
        assert not store.get("sync_error")             # pump un-parked
    _sidecar_client_flow(store, clock, flow)


def test_activity_endpoint_validates_and_feeds_state(store):
    clock = Clock()

    async def flow(client, service):
        resp = await client.post("/v1/activity", json={
            "bundle_id": "com.apple.dt.Xcode", "idle_seconds": 3.5})
        assert resp.status == 200
        state = await (await client.get("/v1/state")).json()
        assert state["activity"]["fresh"] is True
        assert state["activity"]["blocked"] is False
        assert "bundle" not in str(state)              # ids never surface

        # a meeting app hushes; malformed bundle ids are treated as absent
        await client.post("/v1/activity", json={
            "bundle_id": "us.zoom.xos", "idle_seconds": 0})
        state = await (await client.get("/v1/state")).json()
        assert state["activity"]["blocked"] is True

        weird = await client.post("/v1/activity", json={
            "bundle_id": "<script>alert(1)</script>", "idle_seconds": 0})
        assert weird.status == 200
        state = await (await client.get("/v1/state")).json()
        assert state["activity"]["blocked"] is False   # junk id = no id

        for bad in ({}, {"idle_seconds": -1}, {"idle_seconds": "3"},
                    {"idle_seconds": True}):
            resp = await client.post("/v1/activity", json=bad)
            assert resp.status == 400, bad
    _sidecar_client_flow(store, clock, flow)


def test_time_based_expiry_broadcasts_posture(store):
    """blocked and drifting can expire by TIME alone (a collector going
    stale) - no line rides along, so the ticker itself must push the
    state or the window stays hushed forever."""
    from src.sidecar.drift import ActivityState, DriftWatch
    clock = Clock()
    activity = ActivityState(clock=clock)
    drift = DriftWatch(store, activity, clock=clock)

    async def flow(client, service):
        ws = await client.ws_connect("/v1/ws")
        hello = await ws.receive_json()
        assert hello["activity"]["blocked"] is False

        # zoom frontmost: the handler broadcasts the hush
        await client.post("/v1/activity", json={
            "bundle_id": "us.zoom.xos", "idle_seconds": 0})
        hushed = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert hushed["activity"]["blocked"] is True

        # the collector dies; staleness lifts the hush with no line - the
        # ticker must broadcast the change on its own
        clock.advance(minutes=1)
        await service._tick_once()
        lifted = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert lifted["type"] == "state"
        assert lifted["activity"]["blocked"] is False
        assert lifted["activity"]["fresh"] is False

        # and a quiet tick with nothing changed broadcasts nothing
        await service._tick_once()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.receive_json(), timeout=0.2)
        await ws.close()

    async def run_flow():
        service = SidecarService(
            store, engine=FocusEngine(store, clock=clock),
            activity=activity, drift=drift)
        client = TestClient(TestServer(service.build_app()))
        await client.start_server()
        try:
            await flow(client, service)
        finally:
            await client.close()
    _run(run_flow())


def test_sidecar_cors_refuses_strangers(store):
    clock = Clock()

    async def flow(client, _service):
        evil = await client.get("/v1/state",
                                headers={"Origin": "https://evil.example"})
        assert evil.status == 403
        # the webview's origin on every platform: tauri://localhost
        # (macOS/linux), http://tauri.localhost (windows), vite (dev)
        for origin in ("tauri://localhost", "http://tauri.localhost",
                       "http://localhost:1420"):
            ok = await client.get("/v1/state", headers={"Origin": origin})
            assert ok.status == 200
            assert ok.headers["Access-Control-Allow-Origin"] == origin
        # no Origin header = not a browser = the tauri shell / curl
        bare = await client.get("/v1/state")
        assert bare.status == 200
    _sidecar_client_flow(store, clock, flow)
