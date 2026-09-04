"""the day's beats (docs/FOCUS_DOGFOOD_DESIGN.md §13): the morning brief
and the gates that shape a day.

what's chordial's to lock down here is the composition on top of the
dainframe's calendar rhythm: who carries the morning beat and at what
time, that every plan names its beat, that the brief's gates read the
right things (presence within days, a message already today, the day's
cap), that the ladder holds the follow-through and NOT the brief, and -
end to end - that an unanswered evening check-in still gets a brief the
next morning, while a reply that morning quietly skips it.
"""

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import (  # noqa: E402
    Base,
    ConversationEvent,
    PlatformIdentity,
    User,
)
from src.agents import AgentOutcome  # noqa: E402
from src.managers.event_log import EventLog  # noqa: E402
from src.managers.event_store_adapter import SqlUserEvents  # noqa: E402
from src.managers.user_manager import UserManager  # noqa: E402
from src.services import beats  # noqa: E402
from src.services.beats import (  # noqa: E402
    AlreadyTalkedTodayGate,
    DayCapGate,
    ForBeats,
    MorningSlotGate,
    RecencyGate,
    morning_cron,
    morning_posture,
    morning_time_allowed,
    parse_morning_time,
    pick_first_thing,
)
from src.services.orchestration import (  # noqa: E402
    build_orchestrator,
    chordial_visibility,
)
from src.services.prompt_service import PromptService  # noqa: E402
from src.services.pulse_wiring import (  # noqa: E402
    ChordialPulseSource,
    ChordialStimulusFactory,
    build_pulse,
    checkin_rhythm,
    morning_rhythm,
)
from src.personas import load_personas  # noqa: E402
from dainframe.core import ReadOnlyEventReader  # noqa: E402
from dainframe.pulse import (  # noqa: E402
    Calendar,
    FiringPlan,
    GateDecision,
    RhythmDecision,
    RhythmKey,
)

# june in US/Pacific (PDT, utc-7): local midnight is 07:00 utc
LOCAL_MIDNIGHT = datetime(2026, 6, 16, 7, 0, tzinfo=timezone.utc)
# 08:31 local: one minute past the house brief time
NOW = LOCAL_MIDNIGHT + timedelta(hours=8, minutes=31)


def local(hour, minute=0, days=0):
    return LOCAL_MIDNIGHT + timedelta(days=days, hours=hour, minutes=minute)


def run(coro):
    return asyncio.run(coro)


def naive(dt):
    return dt.replace(tzinfo=None)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid="u1", preferred_name="megan", timezone="US/Pacific"))
        s.add(PlatformIdentity(
            user_uuid="u1", platform="discord", platform_user_id="42",
        ))
        s.commit()
    yield TestSession
    engine.dispose()


def seed(db, author_type, author, content, at, *, kind="message",
         message_type="conversation"):
    with db() as s:
        s.add(ConversationEvent(
            user_uuid="u1", platform="discord", author_type=author_type,
            author=author, kind=kind, content=content,
            message_type=message_type if kind == "message" else None,
            created_at=naive(at),
        ))
        s.commit()


def reader():
    return ReadOnlyEventReader(SqlUserEvents("u1", visibility=chordial_visibility))


def plan_for(beat):
    return FiringPlan(
        key=RhythmKey(stream_id="u1", rhythm_id=beat), kind="scheduled_tick",
        due_at=NOW, extras={"beat": beat},
    )


TZ = UserManager().get_user_timezone


# --- the time, and the beat ----------------------------------------------------


def test_morning_time_parses_to_a_local_cron():
    assert parse_morning_time("08:30") == (8, 30)
    assert morning_cron("08:30") == "30 8 * * *"
    assert morning_cron("7:05") == "5 7 * * *"
    assert beats.canonical_morning_time("7:05") == "07:05"
    for bad in ("8", "25:00", "08:60", "morning", ""):
        with pytest.raises(ValueError):
            parse_morning_time(bad)


def test_beat_of_strips_the_rhythm_ids_suffix():
    assert beats.beat_of("morning") == "morning"
    assert beats.beat_of("morning@07:15") == "morning"
    assert beats.beat_of("checkin@120") == "checkin"


def test_source_carries_the_morning_beat_per_user(db):
    async def tz(_):
        return "US/Pacific"

    def ids(source):
        return [r.rhythm_id for _, rhythms in run(source.streams()) for r in rhythms]

    # no reader = no brief (dev rigs, older compositions)
    assert ids(ChordialPulseSource(UserManager())) == ["checkin"]
    # the house time keeps the bare id; a custom time rides in the id
    house = ChordialPulseSource(UserManager(), morning_time=lambda u: "08:30")
    assert ids(house) == ["checkin", "morning"]
    custom = ChordialPulseSource(UserManager(), morning_time=lambda u: "07:15")
    assert ids(custom) == ["checkin", "morning@07:15"]
    # off = no morning beat, the check-in untouched
    off = ChordialPulseSource(UserManager(), morning_time=lambda u: None)
    assert ids(off) == ["checkin"]

    rhythm = morning_rhythm("07:15", tz)
    assert isinstance(rhythm.rhythm, Calendar)
    assert rhythm.rhythm.cron == "15 7 * * *"
    assert rhythm.rhythm.misfire == "skip"


def test_a_failing_morning_read_costs_the_brief_never_the_checkin(db):
    def boom(_):
        raise RuntimeError("db hiccup")

    source = ChordialPulseSource(UserManager(), morning_time=boom)
    ids = [r.rhythm_id for _, rhythms in run(source.streams()) for r in rhythms]
    assert ids == ["checkin"]


def test_morning_time_preference_reads_canonical_off_and_default(db):
    users = UserManager()
    assert beats.morning_time_of("u1", "08:30") == "08:30"
    run(users.merge_schedule_preferences("u1", {"morning_time": "7:15"}))
    assert beats.morning_time_of("u1", "08:30") == "07:15"
    run(users.merge_schedule_preferences("u1", {"morning_time": "off"}))
    assert beats.morning_time_of("u1", "08:30") is None
    run(users.merge_schedule_preferences("u1", {"morning_time": "garbage"}))
    assert beats.morning_time_of("u1", "08:30") == "08:30"
    run(users.merge_schedule_preferences("u1", {"morning_time": None}))
    assert beats.morning_time_of("u1", "08:30") == "08:30"
    # a stored time quiet hours can't honor falls back to the house time
    run(users.merge_schedule_preferences("u1", {"morning_time": "07:15"}))
    assert beats.morning_time_of("u1", "08:30", quiet_hours=(21, 8)) == "08:30"
    assert beats.morning_time_of("u1", "08:30", quiet_hours=(21, 7)) == "07:15"


def test_morning_time_must_lie_outside_quiet_hours():
    assert morning_time_allowed("08:00", 21, 8)
    assert morning_time_allowed("08:30", 21, 8)
    assert not morning_time_allowed("07:59", 21, 8)
    assert not morning_time_allowed("23:00", 21, 8)
    # non-wrapping quiet hours too
    assert not morning_time_allowed("03:00", 1, 6)
    assert morning_time_allowed("06:00", 1, 6)


def test_the_preference_tool_refuses_a_quiet_hours_time(db):
    from dainframe.tools.context import ToolContext
    from src.services.tools.preference_tools import SET_PREFERENCE

    ctx = ToolContext(stream_id="u1", activation_id="a1", actor="vel",
                      metadata={"user_id": "u1"})
    reply = run(SET_PREFERENCE.handler({"morning_time": "07:15"}, ctx))
    assert "inside quiet hours" in reply
    assert beats.morning_time_of("u1", "08:30") == "08:30"
    reply = run(SET_PREFERENCE.handler({"morning_time": "9:00"}, ctx))
    assert "09:00" in reply
    assert beats.morning_time_of("u1", "08:30") == "09:00"


def test_every_plan_and_stimulus_names_its_beat(db):
    factory = ChordialStimulusFactory(
        UserManager(), platforms=["discord"], now=lambda: NOW)
    decision = RhythmDecision(due_at=NOW)
    morning = run(factory.plan("u1", morning_rhythm("08:30", TZ), decision))
    checkin = run(factory.plan("u1", checkin_rhythm(), decision))
    assert morning.extras["beat"] == "morning"
    assert checkin.extras["beat"] == "checkin"
    stimulus = run(factory.build(morning))
    assert stimulus.extras["beat"] == "morning"
    assert stimulus.kind == "scheduled_tick"


# --- the gates -------------------------------------------------------------------


def test_recency_gate_wants_presence_within_the_window(db):
    gate = RecencyGate(days=4)
    # never seen: the brief is never the first thing they hear
    verdict = run(gate.check(plan_for("morning"), reader(), NOW))
    assert not verdict.allowed and verdict.retry_at == NOW + timedelta(hours=12)
    # seen five days ago: the ladder owns them
    seed(db, "user", "user", "hi", NOW - timedelta(days=5))
    assert not run(gate.check(plan_for("morning"), reader(), NOW)).allowed
    # a banked block two days ago is presence, exactly like a reply
    seed(db, "user", "user", "landed piano - 25 min", NOW - timedelta(days=2),
         kind="action")
    assert run(gate.check(plan_for("morning"), reader(), NOW)).allowed


def test_already_talked_today_gate_reads_the_local_day(db):
    gate = AlreadyTalkedTodayGate(TZ)
    # nothing yet: clear
    assert run(gate.check(plan_for("morning"), reader(), NOW)).allowed
    # 23:00 local yesterday is yesterday
    seed(db, "user", "user", "night", local(23, days=-1))
    assert run(gate.check(plan_for("morning"), reader(), NOW)).allowed
    # 07:50 local today: they're up and talking
    seed(db, "user", "user", "morning!", local(7, 50))
    verdict = run(gate.check(plan_for("morning"), reader(), NOW))
    assert not verdict.allowed and "already messaged today" in verdict.reason
    # our own message this morning is not theirs
    seed(db, "agent", "vel", "hi", local(8, 0), message_type="scheduled")
    assert not run(gate.check(plan_for("morning"), reader(), NOW)).allowed


def test_day_cap_gate_bounds_the_day_and_spaces_the_sends(db):
    gate = DayCapGate(3, timedelta(hours=3), TZ)
    assert run(gate.check(plan_for("checkin"), reader(), NOW)).allowed
    # yesterday's sends are yesterday's (23:00 local)
    seed(db, "agent", "vel", "last night", local(23, days=-1),
         message_type="scheduled")
    assert run(gate.check(plan_for("checkin"), reader(), NOW)).allowed
    # one at 02:00 local today: today's, but spaced enough
    seed(db, "agent", "vel", "early", local(2, 0), message_type="scheduled")
    assert run(gate.check(plan_for("checkin"), reader(), NOW)).allowed
    # one an hour ago: too soon, retry when the gap has passed
    seed(db, "agent", "vel", "again", NOW - timedelta(hours=1),
         message_type="scheduled")
    verdict = run(gate.check(plan_for("checkin"), reader(), NOW))
    assert not verdict.allowed and "too soon" in verdict.reason
    assert verdict.retry_at == NOW - timedelta(hours=1) + timedelta(hours=3)
    # a third today: the cap, until local midnight
    seed(db, "agent", "vel", "third", NOW - timedelta(hours=1, minutes=1),
         message_type="scheduled")
    verdict = run(gate.check(plan_for("checkin"), reader(), NOW))
    assert not verdict.allowed and "day cap" in verdict.reason
    assert verdict.retry_at == LOCAL_MIDNIGHT + timedelta(days=1)
    # ordinary conversation replies never count against the cap
    conversation = DayCapGate(1, timedelta(0), TZ)
    seed(db, "agent", "vel", "chatting", NOW - timedelta(minutes=5))
    assert "day cap" in run(
        conversation.check(plan_for("checkin"), reader(), NOW)).reason


def test_morning_slot_gate_holds_the_followthrough_until_the_slot_passes(db):
    gate = MorningSlotGate(lambda u: "08:30", TZ)
    # 08:00 local: the brief's slot is ahead - hold until 09:30 local
    verdict = run(gate.check(plan_for("checkin"), reader(), local(8, 0)))
    assert not verdict.allowed and verdict.retry_at == local(9, 30)
    # 09:29: still inside the grace
    assert not run(gate.check(plan_for("checkin"), reader(), local(9, 29))).allowed
    # 09:30 and after: clear
    assert run(gate.check(plan_for("checkin"), reader(), local(9, 30))).allowed
    assert run(gate.check(plan_for("checkin"), reader(), local(15, 0))).allowed
    # the brief off: never held
    off = MorningSlotGate(lambda u: None, TZ)
    assert run(off.check(plan_for("checkin"), reader(), local(8, 0))).allowed

    # an unreadable preference costs the hold, never the check-in
    def boom(_):
        raise RuntimeError("db hiccup")
    assert run(MorningSlotGate(boom, TZ).check(
        plan_for("checkin"), reader(), local(8, 0))).allowed


def test_for_beats_scopes_a_gate_to_its_beats():
    class Deny:
        async def check(self, firing, events, now):
            return GateDecision(False, "no")

    gate = ForBeats(Deny(), {"checkin"})
    assert run(gate.check(plan_for("morning"), None, NOW)).allowed
    assert not run(gate.check(plan_for("checkin"), None, NOW)).allowed
    # a plan minted before beats existed is the check-in
    legacy = FiringPlan(key=RhythmKey("u1", "checkin"), kind="scheduled_tick",
                        due_at=NOW)
    assert not run(gate.check(legacy, None, NOW)).allowed


# --- the posture -------------------------------------------------------------------


def test_first_thing_is_carried_over_then_priority_then_commitment():
    payload = {
        "tasks_overdue": [{"title": "call the landlord"}],
        "tasks_today": [{"title": "low", "priority": "low"},
                        {"title": "high", "priority": "high"}],
    }
    assert pick_first_thing(payload) == "call the landlord"
    payload["tasks_overdue"] = []
    assert pick_first_thing(payload) == "high"
    payload["tasks_today"] = []
    commitments = [
        {"title": "done one", "status": "completed", "next_action": "x"},
        {"title": "no action", "status": "active", "next_action": None},
        {"title": "portfolio", "status": "active",
         "next_action": "outline section two"},
    ]
    assert pick_first_thing(payload, commitments) == \
        "portfolio - outline section two"
    # a commitment without a next action is not a small block anyone can
    # start: nothing to point at, the brief asks instead
    assert pick_first_thing(payload, commitments[:2]) is None
    assert pick_first_thing(payload, []) is None
    assert pick_first_thing(None) is None
    assert morning_posture("x") == "morning_first"
    assert morning_posture(None) == "morning_open"


def test_scheduled_prompt_keeps_its_bytes_without_a_posture():
    svc = PromptService(persona=load_personas()["vel"],
                        enable_prompt_logging=False)
    plain = run(svc.build_scheduled_request(
        conversation_history=[], user_name="megan", user_uuid=None,
        user_timezone="US/Pacific"))
    same = run(svc.build_scheduled_request(
        conversation_history=[], user_name="megan", user_uuid=None,
        user_timezone="US/Pacific", posture=None, first_thing=None))
    assert plain.messages[-1].content == same.messages[-1].content
    assert "scheduled check-in" in plain.messages[-1].content

    first = run(svc.build_scheduled_request(
        conversation_history=[], user_name="megan", user_uuid=None,
        user_timezone="US/Pacific", posture="morning_first",
        first_thing="call the landlord"))
    body = first.messages[-1].content
    assert "morning brief" in body
    assert 'point at ONE first thing: "call the landlord"' in body
    assert "scheduled check-in" not in body

    open_day = run(svc.build_scheduled_request(
        conversation_history=[], user_name="megan", user_uuid=None,
        user_timezone="US/Pacific", posture="morning_open"))
    assert "ask what today's shape is" in open_day.messages[-1].content


# --- end to end ---------------------------------------------------------------------


class RecordingAgent:
    def __init__(self):
        self.name = "vel"
        self.briefings = []

    async def act(self, briefing):
        self.briefings.append(briefing)
        return AgentOutcome(text="this morning, then~")


class FakeDeliver:
    def __init__(self):
        self.calls = []

    async def __call__(self, platform, target_id, text, speaker="vel"):
        self.calls.append((platform, target_id, text, speaker))
        return True


def make_pulse(db, clock):
    companion = RecordingAgent()
    deliver = FakeDeliver()
    orch = build_orchestrator(
        agents={"vel": companion}, user_manager=UserManager(), deliver=deliver)
    pulse = build_pulse(
        orchestrator=orch, user_manager=UserManager(), platforms=["discord"],
        now=lambda: clock["now"])
    return pulse, companion, deliver


def test_an_unanswered_evening_checkin_still_gets_a_brief_next_morning(db):
    """the sep 3 morning (§13): the 21:55 check-in went unanswered, the
    ladder's 1d rung would have skipped the morning. the brief is the
    recency window's, not the ladder's - it lands at 08:30."""
    seed(db, "user", "user", "hey", NOW - timedelta(days=2))
    seed(db, "agent", "vel", "evening check-in", local(21, 55, days=-1),
         message_type="scheduled")
    clock = {"now": local(8, 0)}
    pulse, companion, deliver = make_pulse(db, clock)

    # 08:00: the calendar beat registers its start; the check-in is held
    # by the ladder (one unanswered, wait a day). nothing lands.
    run(pulse.tick())
    assert deliver.calls == []

    clock["now"] = NOW
    run(pulse.tick())
    assert [c[2] for c in deliver.calls] == ["this morning, then~"]
    briefing = companion.briefings[-1]
    assert briefing.kind == "scheduled_checkin"
    assert briefing.extras["beat"] == "morning"
    # nothing planned in this workspace: the open posture
    assert briefing.extras["checkin_posture"] == "morning_open"
    assert briefing.extras["first_thing"] is None
    recorded = EventLog("u1").recent()[-1]
    assert (recorded.author, recorded.message_type) == ("vel", "scheduled")

    # and once is once: the same morning never fires twice
    clock["now"] = NOW + timedelta(minutes=10)
    run(pulse.tick())
    assert len(deliver.calls) == 1


def test_an_evening_reply_never_lets_the_checkin_cap_the_brief(db):
    """sol's #85 round: a reply the previous evening, nothing unanswered.
    the check-in comes due the minute quiet hours end; at 08:00 it must
    hold for the brief's slot, or the 08:30 brief is capped out of its
    grace and the morning is skipped."""
    seed(db, "user", "user", "night!", local(20, 0, days=-1))
    clock = {"now": local(8, 0)}
    pulse, companion, deliver = make_pulse(db, clock)

    run(pulse.tick())
    assert deliver.calls == [], "the 08:00 check-in must wait for the brief"

    clock["now"] = NOW
    run(pulse.tick())
    assert len(deliver.calls) == 1
    assert companion.briefings[-1].extras["beat"] == "morning"

    # the slot passes: the check-in is next in line, but the brief it
    # follows is unanswered (the ladder) and too recent (the cap)
    clock["now"] = local(9, 31)
    run(pulse.tick())
    assert len(deliver.calls) == 1


def test_a_reply_that_morning_skips_the_brief(db):
    seed(db, "user", "user", "hey", NOW - timedelta(days=2))
    seed(db, "user", "user", "up early today", local(7, 50))
    clock = {"now": local(8, 0)}
    pulse, companion, deliver = make_pulse(db, clock)
    run(pulse.tick())
    clock["now"] = NOW
    run(pulse.tick())
    assert deliver.calls == []
    assert companion.briefings == []


def test_four_quiet_days_hand_the_brief_to_the_ladder(db):
    seed(db, "user", "user", "hey", NOW - timedelta(days=5))
    clock = {"now": local(8, 0)}
    pulse, companion, deliver = make_pulse(db, clock)
    run(pulse.tick())
    clock["now"] = NOW
    run(pulse.tick())
    # the check-in beat fires here instead (first contact after a long
    # quiet: the ladder is clear, quiet hours are over) - the brief did not
    beats_seen = [b.extras["beat"] for b in companion.briefings]
    assert "morning" not in beats_seen
