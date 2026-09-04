"""the day's beats (docs/FOCUS_DOGFOOD_DESIGN.md §13): the morning brief,
and the gates that give a day its shape.

before this, chordial's only proactive beat was an hourly interval anchored
on the last activity - vel spoke whenever an hour happened to elapse (at
15:19, at 21:55, and never in the morning, the one time a planning
companion is worth the most). the day now has BEATS, each its own rhythm
key on the pulse: the morning brief (a dainframe Calendar rhythm at a
user-local time), the existing check-in as the day's follow-through, and
- later - the evening settle. every firing plan carries which beat it is
(`extras["beat"]`), and the gates here read that:

- RecencyGate: the brief goes only to people seen within a few days;
  beyond that the cadence ladder owns the relationship (weekly, then the
  sixty-day floor). the brief is deliberately NOT under the cadence
  ladder itself: the ladder's rungs are 24h waits, so a check-in at 21:55
  plus "1d" lands at 21:55 - outside the morning window - and the brief
  would skip a day. the recency window is the brief's ladder.
- AlreadyTalkedTodayGate: a person who has already messaged this morning
  is up and talking; the brief folds into that conversation's ambient
  instead of arriving as a second message.
- DayCapGate: at most N proactive sends per user-local day, and no two
  within a few hours - applies to every beat.

all arithmetic reads the user-wide event reader the pulse already hands
every gate; chordial rows are naive utc, normalized through as_utc.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from dainframe.core import EventQuery
from dainframe.pulse import FiringPlan, GateDecision, as_utc

logger = logging.getLogger(__name__)

# beat names: the rhythm id's family. the check-in's id carries the taper's
# stretched beat ("checkin@120"), the brief's carries a custom time
# ("morning@07:15") - the beat is the part before the "@"
BEAT_CHECKIN = "checkin"
BEAT_MORNING = "morning"

# a gate-denied calendar occurrence is re-evaluated at retry_at; past the
# calendar's one-hour grace it is skipped and consumed, so a same-day
# denial sleeps well past the grace and well short of tomorrow's occurrence
_SAME_DAY_DENIAL = timedelta(hours=12)

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def beat_of(rhythm_id: str) -> str:
    """'morning@07:15' -> 'morning'; 'checkin' -> 'checkin'."""
    return rhythm_id.split("@", 1)[0]


def parse_morning_time(value: str) -> tuple[int, int]:
    """'08:30' -> (8, 30). raises ValueError with a message the preference
    tool can hand straight to the model."""
    match = _TIME_RE.match(value.strip())
    if not match:
        raise ValueError("a time looks like '08:30' (24-hour, local)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("a time looks like '08:30' (24-hour, local)")
    return hour, minute


def morning_cron(value: str) -> str:
    """the brief's five-field cron for a local 'HH:MM'."""
    hour, minute = parse_morning_time(value)
    return f"{minute} {hour} * * *"


def canonical_morning_time(value: str) -> str:
    hour, minute = parse_morning_time(value)
    return f"{hour:02d}:{minute:02d}"


def morning_time_of(user_uuid: str, default: str) -> Optional[str]:
    """the user's brief time: schedule_preferences["morning_time"] as a
    canonical 'HH:MM', the house default when unset, None when "off". a
    stored value that no longer parses costs the override, never the
    brief. sync - the source runs it off-loop like the taper's read."""
    from src.database.database import get_db
    from src.database.models import User

    with get_db() as db:
        user = db.query(User).filter(User.uuid == user_uuid).first()
        prefs = (user.schedule_preferences or {}) if user else {}
        value = prefs.get("morning_time")
    if not isinstance(value, str):
        return default
    if value.strip().lower() == "off":
        return None
    try:
        return canonical_morning_time(value)
    except ValueError:
        logger.warning("stored morning_time %r for %s no longer parses; "
                       "house default", value, user_uuid)
        return default


# --- gates -------------------------------------------------------------------

USER_PRESENCE = EventQuery(
    kinds=frozenset({"message", "action"}), author_types=frozenset({"user"})
)
USER_MESSAGES = EventQuery(
    kinds=frozenset({"message"}), author_types=frozenset({"user"})
)


def _beat(firing: FiringPlan) -> str:
    """the plan's beat; a plan minted before beats existed is the check-in"""
    beat = firing.extras.get("beat") if firing.extras else None
    return beat if isinstance(beat, str) else BEAT_CHECKIN


class ForBeats:
    """apply a gate to some beats only (the ladder to the follow-through,
    recency to the brief); every other beat passes untouched."""

    def __init__(self, gate, beats: Iterable[str]):
        self.gate = gate
        self.beats = frozenset(beats)

    async def check(self, firing: FiringPlan, events, now) -> GateDecision:
        if _beat(firing) not in self.beats:
            return GateDecision(True, "not this beat")
        return await self.gate.check(firing, events, now)


async def _local_zone(tz_of, stream_id: str) -> Optional[ZoneInfo]:
    tz = await tz_of(stream_id)
    try:
        return ZoneInfo(tz)
    except Exception:
        logger.warning("beat gate cannot resolve timezone %r for %s; "
                       "failing closed", tz, stream_id)
        return None


def _local_midnight(now: datetime, zone: ZoneInfo) -> datetime:
    local = now.astimezone(zone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


class RecencyGate:
    """the brief is for people who are around: deny unless a user-authored
    presence event (a message anywhere, or a banked block) is newer than
    `days` ago. no presence at all reads as absent - the brief is never
    the first thing a person hears from the house."""

    def __init__(self, days: int):
        self.window = timedelta(days=days)

    async def check(self, firing: FiringPlan, events, now) -> GateDecision:
        latest = await events.latest(USER_PRESENCE)
        if latest is not None and as_utc(latest.created_at) >= now - self.window:
            return GateDecision(True, "clear")
        seen = ("never" if latest is None
                else f"{(now - as_utc(latest.created_at)).days}d ago")
        return GateDecision(
            False,
            f"no presence within {self.window.days}d (last seen {seen}): "
            "the ladder owns them",
            retry_at=now + _SAME_DAY_DENIAL,
        )


class AlreadyTalkedTodayGate:
    """a person who already messaged today (their local day) is up and
    talking - the brief rides that conversation's ambient instead."""

    def __init__(self, tz_of: Callable[[str], Awaitable[str]]):
        self.tz_of = tz_of

    async def check(self, firing: FiringPlan, events, now) -> GateDecision:
        zone = await _local_zone(self.tz_of, firing.key.stream_id)
        if zone is None:
            return GateDecision(False, "unresolvable timezone (fail closed)")
        latest = await events.latest(USER_MESSAGES)
        if latest is None:
            return GateDecision(True, "clear")
        if as_utc(latest.created_at) >= _local_midnight(now, zone):
            return GateDecision(
                False, "they already messaged today: the brief folds into "
                "the conversation", retry_at=now + _SAME_DAY_DENIAL)
        return GateDecision(True, "clear")


class DayCapGate:
    """no more than `cap` proactive sends per user-local day, and never two
    within `min_gap` of each other. counts agent messages of the proactive
    message type since local midnight - every beat's sends, whichever beat
    they were."""

    def __init__(self, cap: int, min_gap: timedelta,
                 tz_of: Callable[[str], Awaitable[str]],
                 proactive_message_type: str = "scheduled"):
        self.cap = max(0, cap)
        self.min_gap = min_gap
        self.tz_of = tz_of
        self.proactive_message_type = proactive_message_type

    async def check(self, firing: FiringPlan, events, now) -> GateDecision:
        zone = await _local_zone(self.tz_of, firing.key.stream_id)
        if zone is None:
            return GateDecision(False, "unresolvable timezone (fail closed)")
        sent = await events.read(EventQuery(
            kinds=frozenset({"message"}),
            author_types=frozenset({"agent"}),
            message_types=frozenset({self.proactive_message_type}),
            message_limit=self.cap + 1,
        ))
        midnight = _local_midnight(now, zone)
        today = [e for e in sent if as_utc(e.created_at) >= midnight]
        if len(today) >= self.cap:
            tomorrow = (midnight + timedelta(days=1)).astimezone(now.tzinfo)
            return GateDecision(
                False, f"day cap reached ({len(today)}/{self.cap} proactive "
                "today)", retry_at=tomorrow)
        newest = max((as_utc(e.created_at) for e in today), default=None)
        if newest is not None and now - newest < self.min_gap:
            return GateDecision(
                False, f"too soon after the last proactive send "
                f"({(now - newest).seconds // 60}m ago)",
                retry_at=newest + self.min_gap)
        return GateDecision(True, "clear")


# --- the brief's posture -----------------------------------------------------

POSTURE_MORNING_FIRST = "morning_first"
POSTURE_MORNING_OPEN = "morning_open"

_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def pick_first_thing(payload: Optional[dict],
                     commitments: Iterable[dict] = ()) -> Optional[str]:
    """the ONE thing the brief points at, deterministically: the oldest
    carried-over task, else the highest-priority task planned today, else
    the first open cycle commitment that has a next action (rendered as
    'title - next action'). None = nothing planned, ask instead."""
    if payload:
        overdue = payload.get("tasks_overdue") or []
        if overdue:
            return overdue[0].get("title") or None
        today = sorted(
            payload.get("tasks_today") or [],
            key=lambda t: _PRIORITY_RANK.get(t.get("priority") or "", 3),
        )
        if today:
            return today[0].get("title") or None
    for c in commitments:
        if c.get("status") in (None, "active", "open") and c.get("next_action"):
            return f'{c.get("title")} - {c["next_action"]}'
    for c in commitments:
        if c.get("status") in (None, "active", "open") and c.get("title"):
            return c["title"]
    return None


def morning_posture(first_thing: Optional[str]) -> str:
    return POSTURE_MORNING_FIRST if first_thing else POSTURE_MORNING_OPEN
