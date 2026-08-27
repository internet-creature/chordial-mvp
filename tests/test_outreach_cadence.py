"""the outreach cadence: chordial's wiring of the dainframe CadenceGate.

the ladder arithmetic itself is library-tested in the dainframe; what's
chordial's to lock down is the seam on each side of it - the per-user
override resolving out of schedule_preferences (and failing soft back to
the house schedule), the set_preference key that writes it conversationally
(validating on the way in, storing the canonical form), and the composed
gate actually holding an ignored chain end-to-end.
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
from src.services.pulse_wiring import cadence_of  # noqa: E402
from src.services.tools.preference_tools import _set_preference  # noqa: E402
from dainframe.pulse import Cadence  # noqa: E402
from dainframe.tools.context import ToolContext  # noqa: E402

# noon US/Pacific in june (PDT, utc-7): comfortably outside quiet hours,
# and 12:00 local - outside the house cadence's 8-11 morning
NOW = datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc)

HOUSE = Cadence.parse("1d x3, 1w x3, 60d @ 8-11")


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


def _set_pref(db, value):
    with db() as s:
        user = s.query(User).filter(User.uuid == "u1").first()
        prefs = dict(user.schedule_preferences or {})
        prefs["outreach_cadence"] = value
        user.schedule_preferences = prefs
        s.commit()


def _stored(db):
    with db() as s:
        user = s.query(User).filter(User.uuid == "u1").first()
        return (user.schedule_preferences or {}).get("outreach_cadence")


# --- the resolver: schedule_preferences -> Cadence ---------------------------


def test_no_stored_spec_resolves_the_house_schedule(db):
    assert run(cadence_of(HOUSE)("u1")) == HOUSE


def test_stored_spec_overrides_the_house_schedule(db):
    _set_pref(db, "60d")
    assert run(cadence_of(HOUSE)("u1")) == Cadence.parse("60d")


def test_cleared_spec_falls_back_to_the_house_schedule(db):
    # 'default' stores None rather than deleting the key
    _set_pref(db, None)
    assert run(cadence_of(HOUSE)("u1")) == HOUSE


def test_unparseable_stored_spec_costs_the_override_never_the_checkin(db, caplog):
    _set_pref(db, "whenever you feel like it")
    with caplog.at_level("WARNING", logger="src.services.pulse_wiring"):
        assert run(cadence_of(HOUSE)("u1")) == HOUSE
    assert any("outreach_cadence" in r.message for r in caplog.records)


# --- the tool: setting it conversationally -----------------------------------


CTX = ToolContext(stream_id="u1", activation_id="a1", actor="vel")


def test_set_preference_stores_the_canonical_spec(db):
    reply = run(_set_preference({"outreach_cadence": "30D"}, CTX))
    assert "30d" in reply
    assert _stored(db) == "30d"
    assert run(cadence_of(HOUSE)("u1")) == Cadence.parse("30d")


def test_set_preference_rejects_junk_and_stores_nothing(db):
    reply = run(_set_preference({"outreach_cadence": "every so often"}, CTX))
    assert "didn't parse" in reply
    assert _stored(db) is None


def test_set_preference_never_half_applies(db):
    """a valid tether beside an invalid cadence: the whole call must be a
    no-op, not a saved tether behind an error message."""
    reply = run(_set_preference(
        {"rewind_tether": True, "outreach_cadence": "vibes"}, CTX))
    assert "didn't parse" in reply
    with db() as s:
        user = s.query(User).filter(User.uuid == "u1").first()
        assert (user.schedule_preferences or {}) == {}


def test_set_preference_default_returns_to_the_house_schedule(db):
    run(_set_preference({"outreach_cadence": "60d"}, CTX))
    reply = run(_set_preference({"outreach_cadence": "default"}, CTX))
    assert "usual schedule" in reply
    assert _stored(db) is None
    assert run(cadence_of(HOUSE)("u1")) == HOUSE


# --- end-to-end: the composed gate holds an ignored chain --------------------


def _seed(db, author_type, author, content, at, message_type="conversation",
          kind="message"):
    with db() as s:
        s.add(ConversationEvent(
            user_uuid="u1", platform="discord", author_type=author_type,
            author=author, kind=kind, content=content,
            message_type=message_type, created_at=naive(at),
        ))
        s.commit()


def _make_pulse(db, clock):
    """one pulse over one store, ticking at whatever time `clock` says -
    the persisted-horizon behavior only shows across reused state."""
    from src.agents import AgentOutcome
    from src.managers.user_manager import UserManager
    from src.services.orchestration import build_orchestrator
    from src.services.pulse_wiring import build_pulse

    class Companion:
        name = "vel"

        async def act(self, briefing):
            return AgentOutcome(text="checking in~")

    calls = []

    async def deliver(platform, target_id, text, speaker="vel"):
        calls.append((platform, target_id, text))
        return True

    orch = build_orchestrator(
        agents={"vel": Companion()}, user_manager=UserManager(), deliver=deliver
    )
    pulse = build_pulse(
        orchestrator=orch, user_manager=UserManager(),
        platforms=["discord"], now=lambda: clock["now"],
    )
    return pulse, calls


def _tick(db):
    pulse, calls = _make_pulse(db, {"now": NOW})
    run(pulse.tick())
    return calls


def test_an_ignored_checkin_holds_for_the_ladder(db):
    """yesterday's unanswered outreach: the beat is long past due, but the
    cadence holds the chain (first rung, one day, mornings) - no delivery."""
    _seed(db, "user", "user", "hi", NOW - timedelta(days=2))
    _seed(db, "agent", "vel", "checking in~", NOW - timedelta(hours=2),
          message_type="scheduled")
    assert _tick(db) == []


def test_a_reply_resets_the_ladder_and_the_checkin_flows(db):
    _seed(db, "user", "user", "hi", NOW - timedelta(days=2))
    _seed(db, "agent", "vel", "checking in~", NOW - timedelta(hours=3),
          message_type="scheduled")
    _seed(db, "user", "user", "sorry, busy day!", NOW - timedelta(hours=2))
    assert _tick(db) == [("discord", "42", "checking in~")]


def test_banking_a_block_resets_the_ladder_like_a_reply(db):
    """presence is not just speech: the wiring widens presence_kinds to
    actions, so the user-authored action event a landed block writes
    (focus_flow) resets the ladder exactly as a reply would - the chain
    must not keep escalating at someone who is banking blocks daily."""
    _seed(db, "user", "user", "hi", NOW - timedelta(days=2))
    _seed(db, "agent", "vel", "checking in~", NOW - timedelta(hours=3),
          message_type="scheduled")
    _seed(db, "user", "user", 'landed "essay" - 27 min',
          NOW - timedelta(hours=2), message_type=None, kind="action")
    assert _tick(db) == [("discord", "42", "checking in~")]


def test_a_reply_wakes_a_persisted_denial_within_the_bound(db):
    """ONE pulse over ONE store across the whole arc: a cadence denial
    persists its horizon, the user replies during it, and the next beat
    still flows - because the gate bounds its horizons (six hours), the
    store's sleep cannot outlive the reply by more than one bound. with an
    unbounded horizon this tick would sleep until the ladder's next
    morning and the delivery below would never happen."""
    clock = {"now": NOW}
    pulse, calls = _make_pulse(db, clock)

    _seed(db, "user", "user", "hi", NOW - timedelta(days=2))
    _seed(db, "agent", "vel", "checking in~", NOW - timedelta(hours=2),
          message_type="scheduled")
    run(pulse.tick())
    assert calls == []  # the ladder holds yesterday's unanswered outreach

    # the user comes back mid-horizon...
    _seed(db, "user", "user", "sorry! long day", NOW + timedelta(minutes=30))

    # ...and one bound later (7pm pacific: inside waking hours) the ordinary
    # beat fires: the reply reset the ladder, the horizon did not entomb it
    clock["now"] = NOW + timedelta(hours=7)
    run(pulse.tick())
    assert calls == [("discord", "42", "checking in~")]
