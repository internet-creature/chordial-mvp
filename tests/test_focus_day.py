"""vel's sense of the day (docs/FOCUS_DOGFOOD_DESIGN.md §5, §10.2): the
focus day snapshot and digest, the six check-in postures, the evidence
flags behind `needs_breakdown`, and the plumbing that gets all of it into
a briefing - the digest on ticks AND user turns, yesterday's digest under
the morning brief, presence riding along from the plan, and the prompt
rendering exactly one posture (the plain check-in keeps its bytes).
"""
import asyncio
import sys
import tempfile
import uuid as uuid_mod
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.database.database as db_mod  # noqa: E402
from src.database.models import (  # noqa: E402
    Base,
    Device,
    DeviceEvent,
    PlatformIdentity,
    Task,
    TaskSetAside,
    User,
)
from src.managers.user_manager import UserManager  # noqa: E402
from src.personas import load_personas  # noqa: E402
from src.services import focus_day  # noqa: E402
from src.services.focus_day import (  # noqa: E402
    checkin_posture,
    first_block,
    render,
    snapshot,
)
from src.services.orchestration import ChordialContext, presence_line  # noqa: E402
from src.services.prompt_service import PromptService  # noqa: E402
from src.services.workspace import agenda as agenda_mod  # noqa: E402
from src.services.workspace.agenda import WorkspaceAgenda  # noqa: E402
from src.services.workspace.store import WorkspaceStore  # noqa: E402
from src.services.pulse_wiring import (  # noqa: E402
    ChordialStimulusFactory,
    checkin_rhythm,
)
from src.web import device_auth  # noqa: E402
from src.web.server import WebService  # noqa: E402
from dainframe.core import ScriptLine, Stimulus  # noqa: E402
from dainframe.pulse import RhythmDecision  # noqa: E402

U1 = "u1"
# a june day in US/Pacific (PDT, utc-7): local midnight is 07:00 utc
LOCAL_MIDNIGHT = datetime(2026, 6, 16, 7, 0)
TODAY = date(2026, 6, 16)
NOW = LOCAL_MIDNIGHT + timedelta(hours=15, minutes=32)   # 3:32pm local


def local(hour, minute=0, days=0):
    """naive utc for a user-local wall time on TODAY (+days)."""
    return LOCAL_MIDNIGHT + timedelta(days=days, hours=hour, minutes=minute)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    monkeypatch.setattr(focus_day, "utc_now", lambda: NOW)
    monkeypatch.setattr(agenda_mod, "utc_now", lambda: NOW)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="megan", timezone="US/Pacific"))
        s.add(PlatformIdentity(user_uuid=U1, platform="app",
                               platform_user_id=U1))
        s.commit()
    yield TestSession
    engine.dispose()


@pytest.fixture()
def device(db):
    code = device_auth.mint_device_link_code(U1)
    device_uuid, token = device_auth.link_device(code, "desk")
    with db() as s:
        pk = s.query(Device.id).filter(
            Device.device_uuid == device_uuid).scalar()
    return {"pk": pk, "token": token, "seq": 0}


def event(db, device, event_type, when, **payload):
    device["seq"] += 1
    with db() as s:
        s.add(DeviceEvent(
            event_uuid=str(uuid_mod.uuid4()), device_id=device["pk"],
            user_uuid=U1, seq=device["seq"], event_type=event_type,
            payload=payload, occurred_at=when, rejected=False))
        s.commit()


def ran(db, device, task_id, label, minutes, ended, reason="paused"):
    event(db, device, "session.ended", ended, task_id=task_id, label=label,
          seconds=minutes * 60, reason=reason)


def task(db, title, *, scheduled=TODAY, status="todo", **cols):
    with db() as s:
        row = Task(user_uuid=U1, title=title, status=status,
                   scheduled=scheduled, **cols)
        s.add(row)
        s.commit()
        return row.id


# --- the snapshot and the digest ------------------------------------------------


def test_the_digest_reads_like_the_design(db, device):
    """§5.1's example, as the code renders it: banked runs grouped by task
    (canonical title, not the run label), finishes, the running clock with
    its target and start time, untouched planned tasks, the parked one,
    and the drift line."""
    case = task(db, "portfolio case study")
    piano = task(db, "practice piano")
    cover = task(db, "write cover letter")
    mom = task(db, "call mom", scheduled=TODAY - timedelta(days=1))
    task(db, "clean desk", set_aside_on=TODAY)
    task(db, "email the landlord", status="done", closed_at=local(11, 5))
    task(db, "next week", scheduled=TODAY + timedelta(days=3))

    ran(db, device, case, "portfolio case study: intro", 25, local(10, 0),
        reason="finished")
    event(db, device, "focus_block.completed", local(10, 0), task_id=case,
          label="portfolio case study: intro", run_seconds=1500,
          banked_seconds_today=1500)
    ran(db, device, piano, "practice piano", 16, local(11, 30))
    ran(db, device, case, "portfolio case study: intro", 17, local(13, 0))
    event(db, device, "drift.detected", local(12, 50), seconds=120)
    event(db, device, "return.detected", local(12, 55))
    event(db, device, "drift.detected", local(12, 57), seconds=90)
    event(db, device, "return.detected", local(12, 59))
    event(db, device, "session.started", local(15, 14), task_id=case,
          label="portfolio case study: outline section two",
          target_minutes=25.0)

    fd = snapshot(U1)
    assert fd.live and fd.day == TODAY
    assert fd.run_count == 3 and fd.banked_seconds == 58 * 60
    assert fd.running.label == "portfolio case study: outline section two"
    assert fd.running.minutes_in == 18 and fd.running.target_minutes == 25
    assert fd.untouched == ["call mom", "write cover letter"]
    assert [t["id"] for t in fd.planned] == [mom, case, piano, cover]
    assert fd.blocks == 1 and fd.drifts == 2 and fd.returns == 2

    text = render(fd)
    assert text.splitlines() == [
        "today so far (from their desk - background awareness, they "
        "haven't seen this):",
        'banked: 58 min in 3 runs - "portfolio case study" 42 min (2 runs) '
        '/ "practice piano" 16 min',
        'finished today: "email the landlord"',
        'right now: clock running on "portfolio case study: outline section '
        'two" - 18 min in, target 25 (since 3:14pm)',
        'untouched today: "call mom" / "write cover letter"',
        'set aside today: "clean desk"',
        "drifted twice mid-block, came back both times",
    ]


def test_an_idle_clock_says_when_the_last_run_ended(db, device):
    piano = task(db, "practice piano")
    event(db, device, "session.started", local(14, 20), task_id=piano,
          label="practice piano")
    ran(db, device, piano, "practice piano", 16, local(14, 45))
    # seq order on the device is what counts: the ended closes the started
    fd = snapshot(U1)
    assert fd.running is None
    assert fd.idle_minutes == 47
    assert 'last run ended 47 min ago (paused "practice piano" at 16 min)' \
        in render(fd)


def test_a_planned_day_with_no_runs_says_so_and_an_empty_day_says_nothing(db):
    assert render(snapshot(U1)) is None
    task(db, "write cover letter")
    text = render(snapshot(U1))
    assert "no runs yet today" in text
    assert 'untouched today: "write cover letter"' in text
    assert "banked:" not in text


def test_the_day_is_the_users_local_day(db, device):
    """an event at 23:30 local yesterday is yesterday's, even though it is
    06:30 utc today; today's snapshot never sees it, yesterday's does."""
    t = task(db, "late night thing")
    ran(db, device, t, "late night thing", 10, local(23, 30, days=-1))
    ran(db, device, t, "late night thing", 5, local(9, 0))
    today = snapshot(U1)
    assert today.banked_seconds == 5 * 60
    yesterday = snapshot(U1, TODAY - timedelta(days=1))
    assert not yesterday.live
    assert yesterday.banked_seconds == 10 * 60
    assert yesterday.running is None
    text = render(yesterday)
    assert text.startswith("yesterday, Mon Jun 15 (from their desk):")
    assert "right now" not in text and "no runs yet" not in text


def test_a_clock_left_open_before_yesterday_is_not_running(db, device):
    t = task(db, "ghost")
    event(db, device, "session.started", local(9, 0, days=-2), task_id=t,
          label="ghost")
    assert snapshot(U1).running is None
    # but one that started late last night and never ended still is
    event(db, device, "session.started", local(23, 50, days=-1), task_id=t,
          label="ghost")
    assert snapshot(U1).running is not None


def test_bad_payloads_are_skipped_not_fatal(db, device):
    t = task(db, "x")
    event(db, device, "session.ended", local(9, 0), task_id=t, label="x",
          seconds="lots")
    event(db, device, "session.ended", local(9, 5), task_id=t, label="x",
          seconds=True)
    event(db, device, "session.ended", local(9, 10), task_id="t7",
          label="orphan", seconds=300)
    fd = snapshot(U1)
    assert fd.run_count == 1 and fd.runs[0].task_id is None
    assert '"orphan" 5 min' in render(fd)


def test_digest_is_guarded(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(focus_day, "snapshot", boom)
    assert focus_day.digest(U1) is None
    assert focus_day.breakdown_flags(U1) == {}


def test_a_frozen_run_is_a_stopped_clock_not_a_running_one(db, device):
    """sol, #88: pause/finish/switch under an open rewind question freezes
    the sidecar run without banking. the started event alone would read
    as mid-block all afternoon; the frozen transition says the clock
    stopped, and the ended that follows on resolution closes it."""
    t = task(db, "practice piano")
    event(db, device, "session.started", local(14, 0), task_id=t,
          label="practice piano", target_minutes=25)
    event(db, device, "session.frozen", local(14, 24), task_id=t,
          label="practice piano", reason="paused")
    fd = snapshot(U1)
    assert fd.running is None
    assert fd.frozen.label == "practice piano" and fd.frozen.minutes_in == 24
    assert checkin_posture(fd)[0] == "untouched"
    assert ('right now: clock stopped on "practice piano" at 24 min, '
            "unbanked - a rewind question is waiting") in render(fd)
    # the answer arrives: the ended banks at the freeze instant
    ran(db, device, t, "practice piano", 14, local(14, 24))
    fd = snapshot(U1)
    assert fd.frozen is None and fd.running is None
    assert fd.banked_seconds == 14 * 60
    assert checkin_posture(fd)[0] == "between"
    # a new start after the freeze is running again
    event(db, device, "session.started", local(15, 0), task_id=t,
          label="practice piano")
    assert snapshot(U1).running is not None


def test_yesterdays_set_aside_survives_tomorrow_and_bring_back(db):
    """sol, #88: the parked stamp is mutable - "tomorrow" and "bring back"
    clear it - so a past day reads the ledger the store writes."""
    store = WorkspaceStore()
    t = store.create_task(U1, "clean desk", scheduled=TODAY)
    yesterday = TODAY - timedelta(days=1)
    store.update_task(U1, t["id"], set_aside_on=yesterday)
    # "bring back" clears the stamp; a later "tomorrow" moves the date
    store.update_task(U1, t["id"], set_aside_on=None)
    store.update_task(U1, t["id"], scheduled=TODAY + timedelta(days=1))
    assert snapshot(U1, yesterday).set_aside == ["clean desk"]
    assert snapshot(U1).set_aside == []
    # and a same-day repark is one ledger row
    store.update_task(U1, t["id"], set_aside_on=TODAY)
    store.update_task(U1, t["id"], set_aside_on=None)
    store.update_task(U1, t["id"], set_aside_on=TODAY)
    with db() as s:
        days = [r.day for r in s.query(TaskSetAside).filter(
            TaskSetAside.task_id == t["id"]).order_by(TaskSetAside.day)]
    assert days == [yesterday, TODAY]
    assert snapshot(U1).set_aside == ["clean desk"]


# --- the flags (§10.2) -------------------------------------------------------------


def test_each_signal_flags_and_dismissed_or_parked_rows_never_do(db, device):
    moved = task(db, "moved", reschedules=2)
    parked = task(db, "parked twice", set_aside_count=2)
    unscoped = task(db, "unscoped false starts")
    scoped = task(db, "scoped false starts", next_action="first para")
    stale = task(db, "untouched two days", scheduled=TODAY - timedelta(days=1),
                 created_at=local(9, 0, days=-1))
    fresh = task(db, "new overdue", scheduled=TODAY - timedelta(days=1),
                 created_at=local(9, 0, days=-1))
    # due yesterday but created today: it was never on yesterday's list
    backdated = task(db, "backdated today", scheduled=TODAY - timedelta(days=1),
                     created_at=local(8, 0))
    # consciously parked yesterday: not waiting on that list either
    was_parked = task(db, "parked yesterday", scheduled=TODAY - timedelta(days=2),
                      created_at=local(9, 0, days=-2))
    with db() as s:
        s.add(TaskSetAside(user_uuid=U1, task_id=was_parked,
                           day=TODAY - timedelta(days=1),
                           created_at=local(9, 0, days=-1)))
        s.commit()
    dismissed = task(db, "dismissed", reschedules=3,
                     breakdown_offer_dismissed_at=local(8, 0))
    today_parked = task(db, "parked today", reschedules=3, set_aside_on=TODAY)
    for t in (unscoped, scoped):
        ran(db, device, t, "x", 2, local(9, 0))
        ran(db, device, t, "x", 3, local(9, 30))
    ran(db, device, fresh, "new overdue", 20, local(10, 0, days=-1))

    flags = snapshot(U1).flags
    assert flags[moved] == ["moved 2 times"]
    assert flags[parked] == ["set aside on 2 days"]
    assert flags[unscoped] == ["2 short starts today, no first piece named"]
    assert flags[stale] == ["on the list two days, untouched"]
    for calm in (scoped, fresh, dismissed, today_parked, backdated,
                 was_parked):
        assert calm not in flags


def test_first_block_prefers_carried_over_then_smallest_and_skips_stuck(
        db, device):
    big = task(db, "big", pom_estimate=4)
    small = task(db, "small", pom_estimate=1, next_action="open the doc")
    fd = snapshot(U1)
    assert first_block(fd) == {"title": "small", "next_action": "open the doc"}
    stuck = task(db, "stuck old", scheduled=TODAY - timedelta(days=3),
                 reschedules=2)
    old = task(db, "old", scheduled=TODAY - timedelta(days=1))
    # touched yesterday, so "old" is carried over but not stuck
    ran(db, device, old, "old", 10, local(16, 0, days=-1))
    fd = snapshot(U1)
    assert stuck in fd.flags and old not in fd.flags
    assert first_block(fd)["title"] == "old"
    assert big and small


# --- the posture (§5.3) -----------------------------------------------------------


def test_posture_order_mid_block_wrapped_stuck_untouched_between_quiet(
        db, device, monkeypatch):
    assert checkin_posture(snapshot(U1)) == ("quiet_day", {})

    t = task(db, "write cover letter", pom_estimate=1)
    posture, detail = checkin_posture(snapshot(U1))
    assert posture == "untouched"
    assert detail["first"]["title"] == "write cover letter"

    ran(db, device, t, "write cover letter", 12, local(14, 0))
    posture, detail = checkin_posture(snapshot(U1))
    assert posture == "between"
    assert detail["idle_minutes"] == 92 and detail["banked_minutes"] == 12

    stuck = task(db, "the big one", reschedules=2)
    posture, detail = checkin_posture(snapshot(U1))
    assert posture == "stuck"
    assert detail == {"title": "the big one", "signals": ["moved 2 times"]}

    event(db, device, "session.started", local(15, 20), task_id=t,
          label="write cover letter: address", target_minutes=10)
    posture, detail = checkin_posture(snapshot(U1))
    assert posture == "mid_block"
    assert detail == {"label": "write cover letter: address",
                      "minutes_in": 12, "target_minutes": 10}

    # the clock stops and every planned task closes: wrapped, even though
    # the stuck one is still open elsewhere in the list
    ran(db, device, t, "write cover letter: address", 10, local(15, 30))
    with db() as s:
        for row in s.query(Task).filter(Task.status == "todo"):
            row.status = "done"
            row.closed_at = local(15, 31)
        s.commit()
    posture, detail = checkin_posture(snapshot(U1))
    assert posture == "wrapped" and detail["evening"] is False
    assert detail["finished"] == ["write cover letter", "the big one"]


def test_an_evening_that_banked_something_wraps_even_with_a_plan_left(
        db, device, monkeypatch):
    t = task(db, "a")
    task(db, "b")
    ran(db, device, t, "a", 25, local(18, 0))
    monkeypatch.setattr(focus_day, "utc_now", lambda: local(20, 15))
    posture, detail = checkin_posture(snapshot(U1))
    assert posture == "wrapped" and detail["evening"] is True
    # an evening with nothing banked has nothing to settle: the plan
    # still invites a first block (the quiet-hours gate decides delivery)
    with db() as s:
        s.query(DeviceEvent).delete()
        s.commit()
    assert checkin_posture(snapshot(U1))[0] == "untouched"


# --- into the briefing --------------------------------------------------------------


def stimulus(kind, **extras):
    return Stimulus(kind=kind, stream_id="room-1", platform="app",
                    scope="dm", audience="vel", addressed=("vel",),
                    extras={"user_id": U1, **extras})


def enrich(kind, **extras):
    ctx = ChordialContext(user_manager=UserManager(),
                          agenda_service=WorkspaceAgenda())
    return run(ctx.enrich(stimulus(kind, **extras), ScriptLine(speaker="vel")))


def test_a_user_turn_sees_the_day_but_no_posture_or_presence(db, device):
    t = task(db, "practice piano")
    ran(db, device, t, "practice piano", 16, local(11, 30))
    briefing = enrich("user_message", presence="active")
    assert briefing.kind == "user_message"
    assert "today so far" in briefing.ambient_context
    assert '"practice piano" 16 min' in briefing.ambient_context
    assert "at the desk" not in briefing.ambient_context
    assert "checkin_posture" not in briefing.extras


def test_a_follow_through_tick_carries_the_posture_and_the_presence_line(
        db, device):
    t = task(db, "practice piano")
    event(db, device, "session.started", local(15, 14), task_id=t,
          label="practice piano: scales", target_minutes=25)
    briefing = enrich("scheduled_tick", beat="checkin", presence="idle",
                      idle_minutes=12)
    assert briefing.kind == "scheduled_checkin"
    assert briefing.extras["checkin_posture"] == "mid_block"
    assert briefing.extras["posture_detail"]["label"] == \
        "practice piano: scales"
    assert briefing.ambient_context.endswith(
        "they're connected but idle at the desk (12 min); this lands on app.")
    assert "right now: clock running" in briefing.ambient_context


def test_presence_lines():
    assert presence_line("active", "app") == \
        "they're at the desk right now; this lands in the app."
    assert presence_line("idle", "telegram", None) == \
        "they're connected but idle at the desk; this lands on telegram."
    assert presence_line("absent", "telegram") == \
        "they're away from the desk; this lands on telegram, phone-sized."
    assert presence_line(None, "app") is None
    assert presence_line("weird", "app") is None


def test_the_morning_brief_wraps_yesterday(db, device):
    t = task(db, "practice piano")
    ran(db, device, t, "practice piano", 16, local(19, 30, days=-1))
    ran(db, device, t, "practice piano", 5, local(9, 0))
    briefing = enrich("scheduled_tick", beat="morning", presence="absent")
    assert briefing.extras["checkin_posture"] == "morning_first"
    ambient = briefing.ambient_context
    assert "yesterday, Mon Jun 15 (from their desk):" in ambient
    assert '"practice piano" 16 min' in ambient
    # today's digest rides too, after yesterday's
    assert ambient.index("yesterday, Mon") < ambient.index("today so far")
    assert '"practice piano" 5 min' in ambient
    assert ambient.endswith("phone-sized.")


def test_a_tick_reads_one_snapshot_for_posture_and_digest(db, device,
                                                          monkeypatch):
    """sol, #88: a start between two reads would make the posture say
    mid-block while the ambient block says idle. one read, both outputs."""
    t = task(db, "practice piano")
    event(db, device, "session.started", local(15, 14), task_id=t,
          label="practice piano: scales", target_minutes=25)
    real = focus_day.snapshot
    calls = []

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)
    monkeypatch.setattr(focus_day, "snapshot", counting)
    briefing = enrich("scheduled_tick", beat="checkin", presence="active")
    assert len(calls) == 1
    assert briefing.extras["checkin_posture"] == "mid_block"
    assert "right now: clock running" in briefing.ambient_context
    calls.clear()
    enrich("user_message")
    assert len(calls) == 1


def test_a_failing_snapshot_costs_the_posture_never_the_tick(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(focus_day, "snapshot", boom)
    briefing = enrich("scheduled_tick", beat="checkin", presence="active")
    assert briefing.kind == "scheduled_checkin"
    assert "checkin_posture" not in briefing.extras
    assert briefing.ambient_context == \
        "they're at the desk right now; this lands in the app."


# --- the plan carries presence -------------------------------------------------------


def test_the_plan_and_stimulus_carry_presence_and_idle_minutes(db):
    factory = ChordialStimulusFactory(
        UserManager(), platforms=["app"],
        presence=lambda u: "idle", idle_of=lambda u: 754.0)
    plan = run(factory.plan(U1, checkin_rhythm(),
                            RhythmDecision(due_at=NOW)))
    assert plan.extras["presence"] == "idle"
    assert plan.extras["idle_minutes"] == 12
    stim = run(factory.build(plan))
    assert stim.extras["presence"] == "idle"
    assert stim.extras["idle_minutes"] == 12

    active = ChordialStimulusFactory(
        UserManager(), platforms=["app"], presence=lambda u: "active",
        idle_of=lambda u: 754.0)
    plan = run(active.plan(U1, checkin_rhythm(), RhythmDecision(due_at=NOW)))
    assert plan.extras["presence"] == "active"
    assert plan.extras["idle_minutes"] is None

    broken = ChordialStimulusFactory(
        UserManager(), platforms=["app"], presence=lambda u: "idle",
        idle_of=lambda u: (_ for _ in ()).throw(RuntimeError("x")))
    plan = run(broken.plan(U1, checkin_rhythm(), RhythmDecision(due_at=NOW)))
    assert plan.extras["idle_minutes"] is None


# --- the prompt renders one posture ---------------------------------------------------


def _scheduled(posture=None, detail=None):
    svc = PromptService(persona=load_personas()["vel"],
                        enable_prompt_logging=False)
    req = run(svc.build_scheduled_request(
        conversation_history=[], user_name="megan", user_uuid=None,
        user_timezone="US/Pacific", posture=posture, posture_detail=detail))
    return req.messages[-1].content


def test_each_day_posture_renders_its_instruction():
    plain = _scheduled()
    assert "scheduled check-in" in plain

    mid = _scheduled("mid_block", {"label": "piano: scales",
                                   "minutes_in": 18, "target_minutes": 25})
    assert 'mid-block right now: "piano: scales", 18 min in of a 25-min ' \
        "target" in mid
    assert "reply with an empty message and it stays unsent" in mid
    assert "scheduled check-in" not in mid

    stuck = _scheduled("stuck", {"title": "the big one",
                                 "signals": ["moved 3 times",
                                             "set aside on 2 days"]})
    assert '"the big one" keeps waiting (moved 3 times, set aside on 2 ' \
        "days)" in stuck
    assert "offer pip to shrink it" in stuck

    untouched = _scheduled("untouched", {"first": {
        "title": "cover letter", "next_action": "address block"}})
    assert 'offer ONE first block - "cover letter" (first piece: "address ' \
        'block")' in untouched
    assert "don't list the day" in untouched
    bare = _scheduled("untouched", {"first": None})
    assert "offer ONE first block from what's planned" in bare

    between = _scheduled("between", {"idle_minutes": 47,
                                     "banked_minutes": 30})
    assert "the clock has been idle 47 min" in between
    assert "name what actually landed, specifically" in between

    wrapped = _scheduled("wrapped", {"evening": True})
    assert "it's evening, and today banked something" in wrapped
    assert "no next-thing pressure" in wrapped
    done = _scheduled("wrapped", {"evening": False})
    assert "the day's plan is done" in done

    quiet = _scheduled("quiet_day", {})
    assert "nothing is planned and nothing has been banked today" in quiet
    assert "grounded in the day so far" in quiet

    for body in (mid, stuck, untouched, between, wrapped, quiet):
        assert "the numbers are context, not content" in body


# --- the today payload flags rows ---------------------------------------------------


def test_today_rows_carry_needs_breakdown(db, device):
    task(db, "calm")
    task(db, "moved a lot", reschedules=3)

    async def go(client):
        headers = {"Authorization": f"Bearer {device['token']}"}
        resp = await client.get("/api/v1/today", headers=headers)
        assert resp.status == 200
        return await resp.json()

    async def with_client():
        service = WebService(user_resolver=lambda: U1)
        client = TestClient(TestServer(service.build_app()))
        await client.start_server()
        try:
            return await go(client)
        finally:
            await client.close()

    payload = run(with_client())
    flags = {row["title"]: row["needs_breakdown"]
             for row in payload["buckets"]["today"]}
    assert flags == {"calm": False, "moved a lot": True}
