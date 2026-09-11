"""the attention seam and the away episode (docs/STUCK_MODE_DESIGN.md
section 4.1, build slice 4): the promises the doc names - a paused run can
still produce an away -> return; stale samples produce neither; a mouse
twitch is debounced; restart preserves an open away episode; ordinary idle
with no block or explicit intent manufactures no return - plus the routes
and the outcome fold."""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sidecar import attention as att  # noqa: E402
from src.sidecar.attention import AttentionState, AwayEpisodeWatch  # noqa: E402
from src.sidecar.drift import ActivityState  # noqa: E402
from src.sidecar.focus import FocusEngine  # noqa: E402
from src.sidecar.lines import LINE_POOLS  # noqa: E402
from src.sidecar.server import SidecarService  # noqa: E402
from src.sidecar.store import SidecarStore  # noqa: E402

HANDOFF = {"execution_id": "exec-away", "episode_id": "ep-1"}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)

    def advance(self, **kw):
        self.now += timedelta(**kw)

    def __call__(self):
        return self.now


@pytest.fixture()
def store(tmp_path):
    s = SidecarStore(tmp_path / "sidecar.db")
    yield s
    s.close()


def rig(store):
    clock = Clock()
    activity = ActivityState(clock=clock)
    attention = AttentionState(activity, clock=clock)
    watch = AwayEpisodeWatch(store, attention, clock=clock)
    return clock, activity, attention, watch


def sample(activity, idle):
    activity.observe(None, idle)


def open_away(watch, **over):
    args = dict(task_id=7, label="stuck-mode design: one sentence",
                next_action="one sentence", minutes=8)
    args.update(over)
    return watch.open(HANDOFF, **args)


def events(store, kind):
    return [e for e in store.pending() if e["type"] == kind]


# --- the machine ---------------------------------------------------------------

def test_a_paused_run_can_still_produce_an_away_and_a_return(store):
    """the drift detector is armed only by a running block; the away
    episode needs no block at all."""
    clock, activity, attention, watch = rig(store)
    away, created = open_away(watch)
    assert created and away["departed_at"] is None
    assert events(store, "attention.away")[0]["payload"]["execution_id"] == "exec-away"
    # they leave: sustained idle establishes departure quickly
    for _ in range(4):
        clock.advance(seconds=30)
        sample(activity, 30 * (_ + 1))
        watch.tick(run_active=False)
    assert watch.payload()["departed_at"] is not None
    assert attention.phase == att.AWAY
    # they come back: fresh input, held past the debounce
    clock.advance(minutes=5)
    sample(activity, 0)
    assert watch.tick(run_active=False) is None       # the first sample only opens the window
    clock.advance(seconds=2)
    sample(activity, 0)
    assert watch.tick(run_active=False) is None       # not held long enough yet
    clock.advance(seconds=2)
    sample(activity, 0)
    assert watch.tick(run_active=False) == att.MOMENT_RETURN
    current = watch.payload()
    assert current["returned_at"] is not None
    returned = events(store, "attention.returned")
    assert len(returned) == 1
    assert returned[0]["payload"]["episode_id"] == "ep-1"
    assert returned[0]["payload"]["away_seconds"] >= 300
    # once back, ticking produces nothing more: one return
    clock.advance(seconds=5)
    sample(activity, 0)
    assert watch.tick(run_active=False) is None
    assert len(events(store, "attention.returned")) == 1


def test_stale_samples_produce_neither_departure_nor_return(store):
    clock, activity, attention, watch = rig(store)
    open_away(watch)
    sample(activity, 0)
    clock.advance(minutes=10)          # the collector died: no fresh sample
    assert watch.tick(run_active=False) is None
    assert watch.payload()["departed_at"] is None
    assert attention.phase == att.PRESENT
    attention.hydrate_away()
    assert watch.tick(run_active=False) is None       # and no return either
    assert events(store, "attention.returned") == []


def test_a_mouse_twitch_is_debounced(store):
    clock, activity, attention, watch = rig(store)
    open_away(watch)
    attention.hydrate_away()
    store.update_away(store.open_away()["id"], departed_at=clock().isoformat())
    # one active sample, then idle again: a twitch, not a return
    clock.advance(minutes=3)
    sample(activity, 0)
    assert watch.tick(run_active=False) is None
    clock.advance(seconds=1)
    sample(activity, 15)
    assert watch.tick(run_active=False) is None
    assert attention.phase == att.AWAY
    assert events(store, "attention.returned") == []
    # a real return: input held across the debounce
    for i in range(3):
        clock.advance(seconds=2)
        sample(activity, 0)
        result = watch.tick(run_active=False)
    assert result == att.MOMENT_RETURN


def test_the_surface_coming_into_view_is_a_hard_return(store):
    clock, activity, attention, watch = rig(store)
    open_away(watch)
    attention.hydrate_away()
    store.update_away(store.open_away()["id"], departed_at=clock().isoformat())
    clock.advance(minutes=2)
    attention.observe_surface(True)
    assert watch.tick(run_active=False) == att.MOMENT_RETURN


def test_restart_preserves_an_open_away_episode(store):
    clock, activity, attention, watch = rig(store)
    open_away(watch)
    for i in range(4):
        clock.advance(seconds=30)
        sample(activity, 30 * (i + 1))
        watch.tick(run_active=False)
    assert watch.payload()["departed_at"] is not None
    # a new sidecar on the same db: the episode is there, the machine is away
    activity2 = ActivityState(clock=clock)
    attention2 = AttentionState(activity2, clock=clock)
    watch2 = AwayEpisodeWatch(store, attention2, clock=clock)
    assert watch2.payload()["execution_id"] == "exec-away"
    assert attention2.phase == att.AWAY
    for _ in range(3):
        clock.advance(seconds=2)
        sample(activity2, 0)
        result = watch2.tick(run_active=False)
    assert result == att.MOMENT_RETURN


def test_ordinary_idle_manufactures_nothing(store):
    """no block, no explicit intent: an idle desk is nobody's business."""
    clock, activity, attention, watch = rig(store)
    for i in range(20):
        clock.advance(minutes=1)
        sample(activity, 60 * (i + 1))
        assert watch.tick(run_active=False) is None
    assert attention.phase == att.PRESENT
    sample(activity, 0)
    assert watch.tick(run_active=False) is None
    assert store.pending() == []
    # nor during a running block: that idleness is drift's
    for i in range(20):
        clock.advance(minutes=1)
        sample(activity, 60 * (i + 1))
        assert watch.tick(run_active=True) is None
    assert attention.phase == att.PRESENT


def test_open_dedupes_supersedes_and_expires(store):
    clock, activity, attention, watch = rig(store)
    first, created = open_away(watch)
    again, created2 = open_away(watch)
    assert created and not created2 and again["execution_id"] == first["execution_id"]
    assert len(events(store, "attention.away")) == 1
    other, _ = watch.open({"execution_id": "exec-2", "episode_id": "ep-2"},
                          task_id=None, label=None, next_action=None, minutes=5)
    assert store.away_by_execution("exec-away")["closed_reason"] == "superseded"
    assert watch.payload()["execution_id"] == "exec-2"
    with pytest.raises(ValueError, match="already happened"):
        open_away(watch)
    clock.advance(hours=4)
    assert watch.tick(run_active=False) is None
    assert watch.payload() is None
    assert store.away_by_execution("exec-2")["closed_reason"] == "expired"


def test_resolve_closes_with_its_reason(store):
    clock, activity, attention, watch = rig(store)
    open_away(watch)
    with pytest.raises(ValueError, match="choice must be"):
        watch.resolve("maybe")
    assert watch.resolve("not_now")["execution_id"] == "exec-away"
    assert store.away_by_execution("exec-away")["closed_reason"] == "declined"
    assert watch.resolve("start") is None


# --- the routes ----------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _client_flow(store, clock, fn):
    async def flow():
        activity = ActivityState(clock=clock)
        service = SidecarService(store, engine=FocusEngine(store, clock=clock),
                                 activity=activity)
        client = TestClient(TestServer(service.build_app()))
        await client.start_server()
        try:
            await fn(client, service, activity)
        finally:
            await client.close()
    _run(flow())


def test_away_routes_pause_the_clock_and_start_the_waiting_step(store):
    clock = Clock()

    async def flow(client, service, activity):
        ws = await client.ws_connect("/v1/ws")
        hello = await ws.receive_json()
        assert hello["away"] is None
        # a clock is running when the body proposal is accepted
        await client.post("/v1/focus/start", json={"task_id": 9, "label": "other"})
        await ws.receive_json(); await ws.receive_json()
        clock.advance(minutes=5)
        body = {"execution": HANDOFF, "task_id": 7,
                "label": "stuck-mode design: one sentence",
                "next_action": "one sentence", "minutes": 8}
        resp = await client.post("/v1/away/start", json=body)
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["replayed"] is False
        assert data["focus"]["running"] is False           # paused, banked
        assert data["paused"]["seconds"] == 300
        assert data["away"]["execution_id"] == "exec-away"
        assert data["line"] in LINE_POOLS["stuck_away"]
        # the retry replays
        resp = await client.post("/v1/away/start", json=body)
        assert (await resp.json())["replayed"] is True
        # the surface signal
        resp = await client.post("/v1/attention", json={"visible": "yes"})
        assert resp.status == 400
        assert (await client.post("/v1/attention", json={"visible": True})).status == 200
        # they leave, then come back: the ticker announces the one return
        for i in range(4):
            clock.advance(seconds=30)
            activity.observe(None, 30 * (i + 1))
            await service._tick_once()
        clock.advance(minutes=3)
        for _ in range(3):
            clock.advance(seconds=2)
            activity.observe(None, 0)
            await service._tick_once()
        pushes = []
        while True:
            try:
                pushes.append(await asyncio.wait_for(ws.receive_json(), timeout=0.3))
            except asyncio.TimeoutError:
                break
        moments = [p["moment"] for p in pushes if p["type"] == "line"]
        assert moments[-1] == "stuck_return"
        assert pushes[-1]["type"] == "state" and pushes[-1]["away"]["returned_at"]
        # the re-offer answered: the waiting step starts, dedupe id derived
        resp = await client.post("/v1/away/step", json={"choice": "start"})
        data = await resp.json()
        assert data["away"] is None
        assert data["focus"]["running"] is True
        assert data["focus"]["execution_id"] == "exec-away:step"
        assert data["focus"]["label"] == "stuck-mode design: one sentence"
        assert data["focus"]["target_minutes"] == 8
        assert (await client.post("/v1/away/step", json={"choice": "start"})).status == 409
        await ws.close()
    _client_flow(store, clock, flow)


def test_away_start_validates_and_not_now_lets_it_go(store):
    clock = Clock()

    async def flow(client, service, activity):
        resp = await client.post("/v1/away/start", json={"task_id": 7})
        assert resp.status == 400
        resp = await client.post("/v1/away/start", json={"execution": HANDOFF, "minutes": -1})
        assert resp.status == 400
        resp = await client.post("/v1/away/start", json={"execution": HANDOFF})
        assert resp.status == 200
        resp = await client.post("/v1/away/step", json={"choice": "nope"})
        assert resp.status == 400
        resp = await client.post("/v1/away/step", json={"choice": "not_now"})
        assert (await resp.json())["away"] is None
        assert store.away_by_execution("exec-away")["closed_reason"] == "declined"
        # a fresh run supersedes a waiting step
        await client.post("/v1/away/start", json={"execution": {"execution_id": "e2", "episode_id": "ep"}})
        await client.post("/v1/focus/start", json={"task_id": 3, "label": "x"})
        assert store.away_by_execution("e2")["closed_reason"] == "superseded"
    _client_flow(store, clock, flow)
