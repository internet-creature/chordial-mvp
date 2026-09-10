"""the stuck routes (docs/STUCK_MODE_DESIGN.md 5.1) through the aiohttp
client: open is idempotent per device, the page can poll, reactions move
the episode, accepting hands back a typed execution, and a stranger's
token sees nothing."""
import asyncio
import sys
import tempfile
import uuid as uuid_mod
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, Task, User  # noqa: E402
from src.services import stuck  # noqa: E402
from src.services.workspace.store import WorkspaceStore  # noqa: E402
from src.web import device_auth  # noqa: E402
from src.web.server import WebService  # noqa: E402

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
        return await fn(client, service)
    finally:
        await client.close()


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


async def _open(client, token, **body):
    body.setdefault("request_id", str(uuid_mod.uuid4()))
    body.setdefault("surface", "companion")
    return await client.post("/api/v1/stuck", json=body, headers=_headers(token))


async def _settled(client, token, episode_id, tries=40):
    """poll like the page does until the card exists."""
    for _ in range(tries):
        resp = await client.get(f"/api/v1/stuck/{episode_id}", headers=_headers(token))
        data = await resp.json()
        if data["episode"]["status"] != stuck.THINKING:
            return data["episode"]
        await asyncio.sleep(0.02)
    raise AssertionError("the episode never left thinking")


def test_open_poll_and_the_fallback_card(env):
    env.create_task(U1, "small thing", scheduled="2026-06-16", pom_estimate=1)
    token = _token()

    async def scenario(client, service):
        resp = await _open(client, token)
        assert resp.status == 201, await resp.text()
        data = await resp.json()
        assert data["ok"] and data["replayed"] is False
        ep = data["episode"]
        assert ep["status"] == stuck.THINKING and ep["proposals"] == []
        # no chat engine on this deployment -> the ladder is the card
        card = await _settled(client, token, ep["episode_id"])
        assert card["status"] == stuck.FALLBACK and card["source"] == "fallback"
        assert [p["kind"] for p in card["proposals"]] == ["step", "body", "rest"]
        assert card["proposals"][0]["thing"]["task_id"]
        assert card["execution"] is None
    _run(_with_client(scenario))


def test_open_replays_the_same_episode_for_the_same_request(env):
    token = _token()

    async def scenario(client, service):
        req = str(uuid_mod.uuid4())
        first = await (await _open(client, token, request_id=req)).json()
        resp = await _open(client, token, request_id=req)
        assert resp.status == 200
        again = await resp.json()
        assert again["replayed"] is True
        assert again["episode"]["episode_id"] == first["episode"]["episode_id"]
    _run(_with_client(scenario))


def test_open_validates_its_body(env):
    token = _token()

    async def scenario(client, service):
        resp = await client.post("/api/v1/stuck", json={"surface": "companion"},
                                 headers=_headers(token))
        assert resp.status == 400 and "request_id" in (await resp.json())["error"]
        resp = await _open(client, token, surface="fridge")
        assert resp.status == 400 and "surface" in (await resp.json())["error"]
        resp = await _open(client, token, extra=1)
        assert resp.status == 400 and "unknown field" in (await resp.json())["error"]
        resp = await _open(client, token, task_id=999)
        assert resp.status == 404
        resp = await client.post("/api/v1/stuck", json={})
        assert resp.status == 401
    _run(_with_client(scenario))


def test_a_strangers_token_sees_nothing(env):
    mine, theirs = _token(U1), _token(U2)

    async def scenario(client, service):
        ep = (await (await _open(client, mine)).json())["episode"]
        resp = await client.get(f"/api/v1/stuck/{ep['episode_id']}",
                                headers=_headers(theirs))
        assert resp.status == 404
        resp = await client.post(
            f"/api/v1/stuck/{ep['episode_id']}/react",
            json={"reaction": "closed", "generation": 1,
                  "request_id": str(uuid_mod.uuid4())},
            headers=_headers(theirs))
        assert resp.status == 404
        resp = await client.get("/api/v1/stuck/not-a-uuid", headers=_headers(mine))
        assert resp.status == 400
    _run(_with_client(scenario))


def test_accept_writes_the_step_and_returns_the_execution(env):
    tid = env.create_task(U1, "the thing", scheduled="2026-06-16", pom_estimate=1)["id"]
    token = _token()

    async def scenario(client, service):
        ep = (await (await _open(client, token)).json())["episode"]
        card = await _settled(client, token, ep["episode_id"])
        step = card["proposals"][0]
        assert step["kind"] == "step"
        req = str(uuid_mod.uuid4())
        body = {"reaction": "accepted", "generation": 1, "request_id": req,
                "proposal_id": step["proposal_id"]}
        resp = await client.post(f"/api/v1/stuck/{ep['episode_id']}/react",
                                 json=body, headers=_headers(token))
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["outcome"] == "accepted"
        ex = data["execution"]
        assert ex["action"] == "start_task" and ex["task_id"] == tid
        assert ex["execution_id"] and ex["minutes"] == 2
        assert ex["next_action"] == step["prepared_step"]["next_action"]
        with db_mod.get_db() as s:
            assert s.get(Task, tid).next_action == ex["next_action"]
        # the retry replays the same execution
        resp = await client.post(f"/api/v1/stuck/{ep['episode_id']}/react",
                                 json=body, headers=_headers(token))
        replay = await resp.json()
        assert replay["outcome"] == "replay"
        assert replay["execution"]["execution_id"] == ex["execution_id"]
        # a fresh reaction on a settled episode is a conflict
        body["request_id"] = str(uuid_mod.uuid4())
        resp = await client.post(f"/api/v1/stuck/{ep['episode_id']}/react",
                                 json=body, headers=_headers(token))
        assert resp.status == 409
    _run(_with_client(scenario))


def test_too_much_changes_nothing_and_react_validates(env):
    tid = env.create_task(U1, "the thing", scheduled="2026-06-16",
                          next_action="old scope")["id"]
    token = _token()

    async def scenario(client, service):
        ep = (await (await _open(client, token)).json())["episode"]
        await _settled(client, token, ep["episode_id"])
        path = f"/api/v1/stuck/{ep['episode_id']}/react"
        resp = await client.post(path, json={"reaction": "different", "generation": 1,
                                             "request_id": str(uuid_mod.uuid4())},
                                 headers=_headers(token))
        assert resp.status == 400 and "proposal_id" in (await resp.json())["error"]
        resp = await client.post(path, json={"reaction": "closed", "generation": 2,
                                             "request_id": str(uuid_mod.uuid4())},
                                 headers=_headers(token))
        assert resp.status == 409 and "stale" in (await resp.json())["error"]
        resp = await client.post(path, json={"reaction": "too_much", "generation": 1,
                                             "request_id": str(uuid_mod.uuid4())},
                                 headers=_headers(token))
        data = await resp.json()
        assert data["outcome"] == "rested" and data["episode"]["status"] == stuck.RESTED
        assert "execution" not in data
        with db_mod.get_db() as s:
            row = s.get(Task, tid)
            assert row.next_action == "old scope" and row.set_aside_on is None
    _run(_with_client(scenario))


def test_third_different_spawns_a_second_generation(env):
    env.create_task(U1, "the thing", scheduled="2026-06-16")
    token = _token()

    async def scenario(client, service):
        ep = (await (await _open(client, token)).json())["episode"]
        card = await _settled(client, token, ep["episode_id"])
        path = f"/api/v1/stuck/{ep['episode_id']}/react"
        outcomes = []
        for p in card["proposals"]:
            resp = await client.post(path, json={
                "reaction": "different", "generation": 1,
                "request_id": str(uuid_mod.uuid4()),
                "proposal_id": p["proposal_id"]}, headers=_headers(token))
            outcomes.append((await resp.json())["outcome"])
        assert outcomes == ["recorded", "recorded", "regenerate"]
        second = await _settled(client, token, ep["episode_id"])
        assert second["generation"] == 2
        assert second["rejected_kinds"] == ["step", "body", "rest"]
        assert all(p["kind"] not in ("step", "body") for p in second["proposals"])
    _run(_with_client(scenario))
