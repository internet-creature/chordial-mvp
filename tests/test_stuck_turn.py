"""the stuck turn's seams (docs/STUCK_MODE_DESIGN.md 5.2 + 5.3): the tool
writes through its context and refuses outside a turn; the briefer builds
a `stuck` briefing whose ambient carries the brief."""
import asyncio
import sys
import tempfile
import uuid as uuid_mod
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dainframe.tools.context import ToolContext  # noqa: E402

import src.database.database as db_mod  # noqa: E402
from src.database.models import Base, PlatformIdentity, Task, User  # noqa: E402
from src.services import focus_day, stuck  # noqa: E402
from src.services.tools.stuck_tools import PROPOSE_UNSTUCK, _propose_unstuck  # noqa: E402
from src.services.workspace import agenda as agenda_mod  # noqa: E402

U1 = "u1"
LOCAL_MIDNIGHT = datetime(2026, 6, 16, 7, 0)
TODAY = date(2026, 6, 16)
NOW = LOCAL_MIDNIGHT + timedelta(hours=15, minutes=32)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    monkeypatch.setattr(focus_day, "utc_now", lambda: NOW)
    monkeypatch.setattr(agenda_mod, "utc_now", lambda: NOW)
    monkeypatch.setattr(stuck, "utc_now", lambda: NOW)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="megan", timezone="US/Pacific"))
        s.add(PlatformIdentity(user_uuid=U1, platform="app", platform_user_id=U1))
        s.commit()
    yield TestSession
    engine.dispose()


def task(db, title, **cols):
    with db() as s:
        row = Task(user_uuid=U1, title=title, status="todo", scheduled=TODAY, **cols)
        s.add(row)
        s.commit()
        return row.id


def context(episode, generation=None, rejected=()):
    return ToolContext(
        stream_id="room-1", activation_id="act-1", actor="pip",
        metadata={"scope": "dm", "user_id": U1,
                  "stuck_episode_id": episode["episode_id"],
                  "stuck_generation": generation or episode["generation"],
                  "stuck_rejected_kinds": list(rejected)})


def proposals(tid):
    return {"proposals": [
        {"kind": "step", "action": "start_task",
         "line": "open the doc and write one sentence.",
         "enough": "one sentence, any sentence",
         "thing": {"task_id": tid},
         "prepared_step": {"task_id": tid, "next_action": "write one sentence"},
         "why": "you wanted the next stuck afternoon to have a door"},
        {"kind": "body", "action": "pause_and_away",
         "line": "water, then eight minutes outside.", "enough": "you came back",
         "minutes": 8},
        {"kind": "rest", "action": "rest_here",
         "line": "close the list for now.", "enough": "you closed this"},
    ]}


def test_the_tool_is_terminal_and_pip_may_call_it():
    assert PROPOSE_UNSTUCK.terminal is True
    assert PROPOSE_UNSTUCK.definition.name == "propose_unstuck"
    import yaml
    card = yaml.safe_load(open(Path(__file__).resolve().parents[1]
                               / "src/personas/pip.yaml"))
    assert "propose_unstuck" in card["tools"]


def test_the_tool_writes_the_card_through_its_context(db):
    tid = task(db, "stuck-mode design")
    store = stuck.StuckStore()
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    reply = run(_propose_unstuck(proposals(tid), context(ep)))
    assert reply.startswith("recorded three proposals")
    after = store.get(U1, ep["episode_id"])
    assert after["status"] == stuck.READY and after["source"] == "model"
    assert after["proposals"][0]["why"].startswith("you wanted")
    # proposing wrote nothing onto the task
    with db() as s:
        assert s.get(Task, tid).next_action is None


def test_the_tool_refuses_bad_proposals_with_the_reason(db):
    tid = task(db, "stuck-mode design")
    store = stuck.StuckStore()
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    bad = proposals(tid)
    bad["proposals"][1]["kind"] = "step"
    reply = run(_propose_unstuck(bad, context(ep)))
    assert reply.startswith("refused:") and "repeats" in reply
    assert store.get(U1, ep["episode_id"])["status"] == stuck.THINKING


def test_the_tool_is_inert_outside_a_turn_and_after_settling(db):
    tid = task(db, "stuck-mode design")
    bare = ToolContext(stream_id="room-1", activation_id="a", actor="pip",
                       metadata={"scope": "dm", "user_id": U1})
    assert run(_propose_unstuck(proposals(tid), bare)).startswith("error:")
    store = stuck.StuckStore()
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    store.settle(ep["episode_id"], 1, stuck.fallback(stuck.gather(U1))[0], "fallback")
    reply = run(_propose_unstuck(proposals(tid), context(ep)))
    assert "already settled" in reply
    assert store.get(U1, ep["episode_id"])["status"] == stuck.FALLBACK


def test_the_tool_honors_turned_down_kinds(db):
    tid = task(db, "stuck-mode design")
    store = stuck.StuckStore()
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    reply = run(_propose_unstuck(proposals(tid), context(ep, rejected=["body"])))
    assert reply.startswith("refused:") and "turned down" in reply


# --- the director and the briefer ------------------------------------------------

from dainframe.core import ScriptLine, Stimulus  # noqa: E402


from src.services.orchestration import ChordialContext, ChordialDirector  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.services.workspace.agenda import WorkspaceAgenda  # noqa: E402


class FakeView:
    def __init__(self, helper_id):
        self.helper_id, self.is_active = helper_id, True


class FakeHSM:
    async def active_helpers(self, user_uuid):
        return [FakeView("vel")]        # pip NOT met - the turn still runs


def stuck_stimulus(**extras):
    return Stimulus(kind="stuck", stream_id="room-1", platform="app",
                    scope="dm", audience="pip", addressed=("pip",),
                    record_inbound=False,
                    extras={"user_id": U1, "stuck_episode_id": "ep-1",
                            "stuck_generation": 1, "stuck_surface": "companion",
                            **extras})


def test_the_director_casts_pip_silently_met_or_not():
    d = ChordialDirector(["vel", "pip"], helper_state_manager=FakeHSM())
    script = run(d.direct(stuck_stimulus(), None))
    assert [l.speaker for l in script.lines] == ["pip"]
    line = script.lines[0]
    assert line.response == "silent" and line.delivery == "none"


def test_the_director_falls_back_to_the_chair_without_pip():
    d = ChordialDirector(["vel"], helper_state_manager=FakeHSM())
    script = run(d.direct(stuck_stimulus(), None))
    assert [l.speaker for l in script.lines] == ["vel"]   # the ladder will stand in


def test_the_briefer_builds_a_stuck_briefing_with_the_brief(db):
    tid = task(db, "stuck-mode design", next_action="one sentence")
    ctx = ChordialContext(user_manager=UserManager(), agenda_service=WorkspaceAgenda())
    briefing = run(ctx.enrich(stuck_stimulus(stuck_rejected_kinds=["body"],
                                             stuck_task_id=tid),
                              ScriptLine(speaker="pip")))
    assert briefing.kind == "stuck"
    assert briefing.extras["stuck_episode_id"] == "ep-1"
    assert briefing.extras["stuck_generation"] == 1
    assert briefing.extras["stuck_rejected_kinds"] == ["body"]
    ambient = briefing.ambient_context
    assert 'they pressed "i\'m stuck" from the companion' in ambient
    assert f'#{tid} "stuck-mode design"' in ambient
    assert f'pressed it from the row of #{tid}' in ambient
    assert "(do not offer these kinds again): body" in ambient
    assert "checkin_posture" not in briefing.extras
