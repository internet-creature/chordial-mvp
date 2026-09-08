"""the focus dogfood round's server spine (docs/FOCUS_DOGFOOD_DESIGN.md
sections 2-4): the scope line (`next_action`), set aside (`set_aside_on`
+ the distinct-days counter), the today payload's `set_aside` bucket, the
agenda's parked line, and PATCH /api/v1/tasks/{id} end to end over the
device api - including the three parked-row actions (tomorrow / bring
back / let it go) exactly as the companion window will issue them.
"""
import asyncio
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
import src.services.workspace.agenda as agenda_mod
from src.database.models import Base, User
from src.services.workspace import vocab
from src.services.workspace.agenda import WorkspaceAgenda, user_today
from src.services.workspace.store import NEXT_ACTION_CAP, WorkspaceStore
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
    yield WorkspaceStore()
    engine.dispose()


def _token(user=U1):
    code = device_auth.mint_device_link_code(user)
    result = device_auth.link_device(code, "test-device")
    assert result is not None
    return result[1]


def _run(coro):
    return asyncio.run(coro)


async def _with_client(fn):
    service = WebService(user_resolver=lambda: U1)
    client = TestClient(TestServer(service.build_app()))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


# --- the store -----------------------------------------------------------------


def test_scope_is_stripped_capped_and_blank_means_unscoped(env):
    store = env
    t = store.create_task(U1, "outline", next_action="  section two  ")
    assert t["next_action"] == "section two"
    t = store.update_task(U1, t["id"], next_action="x" * (NEXT_ACTION_CAP + 40))
    assert len(t["next_action"]) == NEXT_ACTION_CAP
    t = store.update_task(U1, t["id"], next_action="   ")
    assert t["next_action"] is None
    t = store.update_task(U1, t["id"], next_action=None)
    assert t["next_action"] is None
    # the council's task line carries the scope once set
    t = store.update_task(U1, t["id"], next_action="first para")
    assert 'next="first para"' in vocab.format_task(t)


def test_set_aside_counts_distinct_days_only(env):
    store = env
    t = store.create_task(U1, "the dreaded one", scheduled="2026-09-04")
    assert t["set_aside_count"] == 0
    t = store.update_task(U1, t["id"], set_aside_on="2026-09-04")
    assert t["set_aside_on"] == "2026-09-04" and t["set_aside_count"] == 1
    # re-stamping the same day and clearing count nothing
    t = store.update_task(U1, t["id"], set_aside_on=date(2026, 9, 4))
    assert t["set_aside_count"] == 1
    t = store.update_task(U1, t["id"], set_aside_on=None)
    assert t["set_aside_on"] is None and t["set_aside_count"] == 1
    # a new day is the signal
    t = store.update_task(U1, t["id"], set_aside_on="2026-09-05")
    assert t["set_aside_count"] == 2 and t["set_aside_counted_on"] == "2026-09-05"
    # set aside is a today decision, never a lifecycle change
    assert t["status"] == "todo" and t["closed_at"] is None


def test_clear_then_repark_the_same_day_counts_once(env):
    """sol, #86: "bring back" clears set_aside_on, so a same-day set aside
    -> bring back -> set aside must not read as a second distinct day."""
    store = env
    t = store.create_task(U1, "flip-flopper", scheduled="2026-09-04")
    for _ in range(3):
        t = store.update_task(U1, t["id"], set_aside_on="2026-09-04")
        t = store.update_task(U1, t["id"], set_aside_on=None)
    assert t["set_aside_on"] is None
    assert t["set_aside_count"] == 1
    assert t["set_aside_counted_on"] == "2026-09-04"
    # the next day still counts exactly once, however many flips
    t = store.update_task(U1, t["id"], set_aside_on="2026-09-05")
    t = store.update_task(U1, t["id"], set_aside_on=None)
    t = store.update_task(U1, t["id"], set_aside_on="2026-09-05")
    assert t["set_aside_count"] == 2


def test_unknown_task_keys_are_still_rejected(env):
    with pytest.raises(ValueError):
        env.update_task(U1, env.create_task(U1, "x")["id"], set_aside=True)


# --- the agenda (the council's view) ------------------------------------------


def test_parked_tasks_leave_the_live_buckets_and_get_their_own_line(env):
    store = env
    today = user_today(U1).isoformat()
    store.create_task(U1, "live one", scheduled=today, next_action="the intro")
    parked = store.create_task(U1, "parked one", scheduled=today)
    store.update_task(U1, parked["id"], set_aside_on=today)
    running = store.create_task(U1, "parked in motion", status="in_progress")
    store.update_task(U1, running["id"], set_aside_on=today)

    agenda = WorkspaceAgenda()
    payload = agenda.get_payload(U1)
    assert [r["title"] for r in payload["tasks_today"]] == ["live one"]
    assert payload["tasks_in_progress"] == []
    assert sorted(r["title"] for r in payload["tasks_set_aside"]) == \
        ["parked in motion", "parked one"]

    digest = agenda.get_digest(U1)
    assert "today (1):" in digest
    assert '"live one"' in digest and "next: the intro" in digest
    assert 'set aside today (their call, don\'t nudge): "parked one", "parked in motion"' \
        in digest


def test_a_day_of_only_parked_tasks_still_yields_a_digest(env):
    today = user_today(U1).isoformat()
    t = env.create_task(U1, "parked", scheduled=today)
    env.update_task(U1, t["id"], set_aside_on=today)
    digest = WorkspaceAgenda().get_digest(U1)
    assert digest is not None and "set aside today" in digest
    assert "\ntoday (" not in digest


# --- PATCH /api/v1/tasks/{id} ------------------------------------------------


def test_patch_scope_and_reschedule(env):
    async def flow(client):
        headers = {"Authorization": f"Bearer {_token(U1)}"}
        resp = await client.post("/api/v1/tasks", json={"title": "outline"},
                                 headers=headers)
        task = (await resp.json())["task"]
        assert task["next_action"] is None and task["set_aside_on"] is None

        resp = await client.patch(f"/api/v1/tasks/{task['id']}",
                                  json={"next_action": "section two"},
                                  headers=headers)
        assert resp.status == 200
        row = (await resp.json())["task"]
        assert row["next_action"] == "section two"

        # the today payload's rows carry it too
        today = await client.get("/api/v1/today", headers=headers)
        rows = (await today.json())["buckets"]["today"]
        assert rows[0]["next_action"] == "section two"

        # clearing, and a reschedule in the same body
        resp = await client.patch(f"/api/v1/tasks/{task['id']}",
                                  json={"next_action": None,
                                        "scheduled": "2030-01-01"},
                                  headers=headers)
        row = (await resp.json())["task"]
        assert row["next_action"] is None and row["scheduled"] == "2030-01-01"
    _run(_with_client(flow))


def test_patch_set_aside_and_the_three_parked_actions(env):
    async def flow(client):
        headers = {"Authorization": f"Bearer {_token(U1)}"}
        today = user_today(U1)

        async def make(title):
            resp = await client.post("/api/v1/tasks", json={"title": title},
                                     headers=headers)
            return (await resp.json())["task"]

        async def buckets():
            today_resp = await client.get("/api/v1/today", headers=headers)
            b = (await today_resp.json())["buckets"]
            return {k: [t["title"] for t in v] for k, v in b.items()}

        a, b, c = await make("tomorrow-bound"), await make("coming back"), \
            await make("letting go")
        # c is the running one: set aside moves it out of in_progress too
        await client.post(f"/api/v1/tasks/{c['id']}/status",
                          json={"status": "in_progress"}, headers=headers)
        for t in (a, b, c):
            resp = await client.patch(f"/api/v1/tasks/{t['id']}",
                                      json={"set_aside": True}, headers=headers)
            assert resp.status == 200
            assert (await resp.json())["task"]["set_aside_on"] == today.isoformat()

        got = await buckets()
        assert got["today"] == [] and got["in_progress"] == []
        assert sorted(got["set_aside"]) == \
            ["coming back", "letting go", "tomorrow-bound"]

        # tomorrow: scheduled + 1, unparked, back to todo if it was running
        resp = await client.patch(
            f"/api/v1/tasks/{a['id']}",
            json={"scheduled": (today + timedelta(days=1)).isoformat(),
                  "set_aside": False, "status": "todo"},
            headers=headers)
        row = (await resp.json())["task"]
        assert row["set_aside_on"] is None
        assert row["scheduled"] == (today + timedelta(days=1)).isoformat()

        # bring back: just the stamp - and a change of heart the same day
        # is still one parked day underneath (the breakdown signal)
        resp = await client.patch(f"/api/v1/tasks/{b['id']}",
                                  json={"set_aside": False}, headers=headers)
        assert (await resp.json())["task"]["set_aside_on"] is None
        await client.patch(f"/api/v1/tasks/{b['id']}",
                           json={"set_aside": True}, headers=headers)
        resp = await client.patch(f"/api/v1/tasks/{b['id']}",
                                  json={"set_aside": False}, headers=headers)
        parked_days = {r["title"]: r["set_aside_count"]
                       for r in env.list_tasks(U1, include_closed=True)}
        assert parked_days["coming back"] == 1

        # let it go: closes it (closed_at stamped), unparked
        resp = await client.patch(f"/api/v1/tasks/{c['id']}",
                                  json={"status": "deprioritized",
                                        "set_aside": False},
                                  headers=headers)
        row = (await resp.json())["task"]
        assert row["status"] == "deprioritized" and row["set_aside_on"] is None

        got = await buckets()
        assert got["set_aside"] == []
        assert got["today"] == ["coming back"]      # a's tomorrow, c's closed
        assert got["done"] == []                    # deprioritized isn't a win
    _run(_with_client(flow))


def test_patch_validates_and_scopes(env):
    async def flow(client):
        headers = {"Authorization": f"Bearer {_token(U1)}"}
        resp = await client.post("/api/v1/tasks", json={"title": "mine"},
                                 headers=headers)
        task = (await resp.json())["task"]
        url = f"/api/v1/tasks/{task['id']}"

        for bad in ({}, [], "nope",
                    {"bogus": 1},                       # unknown key
                    {"next_action": 5},
                    {"scheduled": "sometime"},
                    {"scheduled": 20260904},
                    {"status": "someday"},
                    {"set_aside": "yes"}):
            resp = await client.patch(url, json=bad, headers=headers)
            assert resp.status == 400, bad
            assert "error" in await resp.json()

        resp = await client.patch(url, json={"bogus": 1, "set_aside": True},
                                  headers=headers)
        assert "bogus" in (await resp.json())["error"]

        resp = await client.patch("/api/v1/tasks/abc", json={"set_aside": True},
                                  headers=headers)
        assert resp.status == 400

        # another tenant can't touch it
        other = {"Authorization": f"Bearer {_token(U2)}"}
        resp = await client.patch(url, json={"set_aside": True}, headers=other)
        assert resp.status == 404

        # no bearer at all
        resp = await client.patch(url, json={"set_aside": True})
        assert resp.status == 401

        # the app's preflight must allow PATCH or the webview never sends it
        resp = await client.options(url, headers={
            "Origin": "http://localhost:1420",
            "Access-Control-Request-Method": "PATCH"})
        assert resp.status == 204
        assert "PATCH" in resp.headers["Access-Control-Allow-Methods"]
    _run(_with_client(flow))
