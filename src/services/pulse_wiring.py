"""chordial's ambient wiring: the dainframe pulse replaces SchedulerService.

what the extraction design promised (the-dainframe DESIGN.md §6.5),
delivered: `get_scheduled_users` -> PulseSource; onboarding + quiet hours +
the proactivity gate -> a gate stack (two of the three now library-shipped,
verbatim arithmetic); `resolve_delivery_identity` + the first-contact rule ->
`StimulusFactory.plan`; scheduled delivery moves onto the engine's ordinary
DIRECT path - the pending+confirm two-phase dance is gone, generation,
delivery, and recording are one serialized activation; the piggybacked
curation pass is just a second rhythm.

what the pulse adds that the scheduler never had: a staleness precondition
(a user who speaks while the firing waits for the stream lock cancels the
check-in before a single token is spent) and persisted denial horizons (a
cadence-denied user isn't re-checked every five minutes; the gate says when -
even a sixty-day floor sleeps cheaply).

timestamps: chordial stores naive-UTC rows; the pulse normalizes naive as
UTC (dainframe as_utc), and this wiring hands the pulse an AWARE utc clock.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from dainframe.core import (
    DeliveryTarget,
    EventQuery,
    ReadOnlyEventReader,
    Stimulus,
)
from dainframe.pulse import (
    Cadence,
    CadenceGate,
    as_utc,
    FiringPlan,
    GateDecision,
    InMemoryPulseStore,
    Interval,
    NoNewerEvent,
    Pulse,
    QuietHoursGate,
    RhythmDecision,
    RhythmKey,
    TaggedRhythm,
)

from config import Config
from src.managers.event_log import EventLog
from src.managers.event_store_adapter import SqlEventStore, SqlUserEvents
from src.managers.user_manager import UserManager
from src.personas import CHAIR_ID
from src.services.metering import BudgetGate
from src.services.orchestration import chordial_visibility

logger = logging.getLogger(__name__)

CHECKIN_RHYTHM = "checkin"
CURATION_RHYTHM = "curation"

# the recency clock: any MESSAGE on any platform (one person on two platforms
# is one schedule slot). action/note rows are invisible by construction - a
# tool action or switch notice never masquerades as "the assistant replied".
ANY_MESSAGE = EventQuery(kinds=frozenset({"message"}))
USER_MESSAGES = EventQuery(
    kinds=frozenset({"message"}), author_types=frozenset({"user"})
)


class UserQuietSince:
    """ActivationPrecondition: holds while the user hasn't spoken ANYWHERE
    since the plan was made. replaces stream-scoped NoNewerEvent staleness -
    rooms made per-stream checks too narrow (the engine hands us the
    stimulus stream's reader, which we deliberately ignore)."""

    def __init__(self, user_uuid: str, than):
        self.user_uuid = user_uuid
        self.than = than

    async def holds(self, events) -> bool:
        last = await asyncio.to_thread(
            lambda: EventLog(self.user_uuid).last_user_message())
        if last is None or last.created_at is None:
            return True
        return as_utc(last.created_at) <= as_utc(self.than)


def aware_utc_now() -> datetime:
    """the pulse boundary is aware-UTC; chordial's own utc_now() stays naive
    for db comparisons."""
    return datetime.now(timezone.utc)


def checkin_rhythm(every_minutes: Optional[int] = None) -> TaggedRhythm:
    """the regular check-in beat. anchored on the last MESSAGE from anyone:
    a fresh user reply restarts the clock, and so does our own outreach -
    with the cadence gate holding ignored chains to the re-engagement
    ladder. no anchor at all = first contact, due now (still behind quiet
    hours: an imported user's first hello never lands at 3am).

    `every_minutes` is the taper's seam (phase 6c): steady scorecards
    stretch this beat per user - earned quiet - while the cadence gate
    keeps holding ignored chains on top of whatever beat is set.

    the rhythm id carries the beat when stretched: the pulse store
    persists a next_check horizon PER KEY and consults it before the
    interval is ever re-evaluated, so a shrunk beat under the same key
    would sleep out the old stretched horizon (a wobbly cycle's reset -
    or a taper failure's fallback - arriving hours late). a changed beat
    lands on a fresh key whose due recomputes from the message anchor
    immediately; the base beat keeps the bare id, so untapered users'
    key and state are exactly pre-6c. abandoned keys are inert rows in
    the in-memory store."""
    minutes = every_minutes or Config.DM_INTERVAL_MINUTES
    rhythm_id = (CHECKIN_RHYTHM if minutes == Config.DM_INTERVAL_MINUTES
                 else f"{CHECKIN_RHYTHM}@{minutes}")
    return TaggedRhythm(
        rhythm_id=rhythm_id,
        kind="scheduled_tick",
        rhythm=Interval(
            every=timedelta(minutes=minutes),
            anchor=ANY_MESSAGE,
        ),
    )


def curation_rhythm() -> TaggedRhythm:
    """the memory-cleanup beat: due whenever the curator's discovery lists
    the user. the small `every` floor only keeps a failing curation from
    re-firing inside one cycle."""
    return TaggedRhythm(
        rhythm_id=CURATION_RHYTHM,
        kind="curation_due",
        rhythm=Interval(every=timedelta(minutes=5), anchor="last_attempt"),
    )


class ChordialPulseSource:
    """who is ambient this cycle. re-read every cycle, so joining/leaving
    needs no registration dance - exactly the old per-cycle user scan."""

    def __init__(self, user_manager: UserManager, curator=None,
                 checkin_minutes=None):
        self.user_manager = user_manager
        self.curator = curator
        # the taper's read: user_uuid -> stretched interval in minutes
        # (sync, run off-loop). None = the flat base beat for everyone.
        self.checkin_minutes = checkin_minutes

    async def _beat(self, user_uuid: str) -> Optional[int]:
        """the taper must never stall the pulse: any failure here is the
        flat base beat, exactly what the pre-taper source handed out."""
        if self.checkin_minutes is None:
            return None
        try:
            return await asyncio.to_thread(self.checkin_minutes, user_uuid)
        except Exception:
            logger.exception("taper read failed for %s; base beat", user_uuid)
            return None

    async def streams(self):
        rhythms: dict[str, list[TaggedRhythm]] = {}
        for user_uuid in await self.user_manager.get_scheduled_users():
            rhythms.setdefault(user_uuid, []).append(
                checkin_rhythm(await self._beat(user_uuid)))
        if self.curator is not None:
            try:
                for user_uuid in await self.curator.find_users_needing_curation():
                    rhythms.setdefault(user_uuid, []).append(curation_rhythm())
            except Exception:
                # curation discovery must never stall check-ins
                logger.exception("curation discovery failed; skipping this cycle")
        return list(rhythms.items())


class ScheduledOnly:
    """chordial's outreach gates guard OUTREACH. curation is silent internal
    bookkeeping - it runs mid-onboarding, at 3am, and under backoff, exactly
    as the old piggybacked pass did."""

    def __init__(self, gate):
        self.gate = gate

    async def check(self, firing: FiringPlan, events, now) -> GateDecision:
        if firing.kind != "scheduled_tick":
            return GateDecision(True, "not outreach")
        return await self.gate.check(firing, events, now)


def cadence_of(default: Cadence):
    """per-user re-engagement specs: schedule_preferences["outreach_cadence"]
    (set conversationally via set_preference), parsed fresh each check so a
    change takes hold on the next tick. the read mirrors the rewind tether's
    (a schedule_preferences key read straight off the row, off-loop). a
    missing key is the house schedule; an invalid stored spec is logged and
    costs the override, never the check-in - and set_preference validates on
    the way in, so an invalid row here means the spec language itself moved."""

    def read(user_uuid: str) -> Optional[str]:
        from src.database.database import get_db
        from src.database.models import User

        with get_db() as db:
            user = db.query(User).filter(User.uuid == user_uuid).first()
            prefs = (user.schedule_preferences or {}) if user else {}
            value = prefs.get("outreach_cadence")
            return value if isinstance(value, str) else None

    async def resolve(user_uuid: str) -> Cadence:
        spec = await asyncio.to_thread(read, user_uuid)
        if spec is None:
            return default
        try:
            return Cadence.parse(spec)
        except ValueError:
            logger.warning(
                "stored outreach_cadence %r for %s no longer parses; "
                "house default", spec, user_uuid)
            return default

    return resolve


class OnboardingGate:
    """LOAD-BEARING: nothing downstream re-checks onboarding - this gate is
    the only thing keeping scheduled sends away from mid-onboarding users
    (same invariant the scheduler's first check carried)."""

    def __init__(self, user_manager: UserManager):
        self.user_manager = user_manager

    async def check(self, firing: FiringPlan, events, now) -> GateDecision:
        if await self.user_manager.needs_onboarding(firing.key.stream_id):
            return GateDecision(False, "user is mid-onboarding")
        return GateDecision(True, "clear")


class ChordialStimulusFactory:
    """plan: resolve the delivery destination BEFORE any tokens - nowhere to
    deliver = no firing. build: the same scheduled_tick stimulus the engine
    has always handled, now carrying a staleness precondition revalidated
    under the stream lock.

    where a proactive word lands is presence-aware (ROOMS_DESIGN section 9):
    `presence` is the app interface's trichotomy - callable(user_uuid) ->
    'active' | 'idle' | 'absent'. someone demonstrably at the desk gets the
    desktop; away or idle gets the phone; never both (there is exactly one
    target per firing, chosen here - the attention budget was spent by the
    gates before routing ever runs). without a presence source (no web
    surface, dev rigs) every user reads as absent, which is exactly the
    pre-7a most-recently-spoke targeting over the messaging platforms."""

    def __init__(
        self,
        user_manager: UserManager,
        platforms: Optional[List[str]] = None,
        now=aware_utc_now,
        presence=None,
    ):
        self.user_manager = user_manager
        self.platforms = platforms
        self._now = now
        self.presence = presence

    def _presence_of(self, user_uuid: str) -> str:
        """the routing trichotomy, guarded: a broken presence source must
        cost the desk preference, never the check-in."""
        if self.presence is None:
            return "absent"
        try:
            state = self.presence(user_uuid)
        except Exception:
            logger.exception("presence lookup failed for %s; treating as "
                             "absent", user_uuid)
            return "absent"
        return state if state in ("active", "idle") else "absent"

    async def _resolve_target(self, user_uuid: str) -> Optional[tuple]:
        """(platform, target_id) for a proactive send, presence first:

        active -> the desktop (falling back to any live link only if the
        app was somehow never linked); idle -> the phone platforms by
        recency, falling back to the still-connected desk (the word waits
        on screen) rather than going silent; absent -> the messaging
        platforms only - the app is closed, and a send there could never
        confirm."""
        state = self._presence_of(user_uuid)
        active = EventLog(user_uuid).active_platform()
        if state == "active":
            return await self.user_manager.resolve_delivery_identity(
                user_uuid, "app", self.platforms)

        away_platforms = (None if self.platforms is None
                          else [p for p in self.platforms if p != "app"])
        preferred = active if active != "app" else None
        target = await self.user_manager.resolve_delivery_identity(
            user_uuid, preferred, away_platforms)
        if target is None and state == "idle":
            # no phone linked, but a desk surface is connected: deliverable,
            # just not attended right now
            target = await self.user_manager.resolve_delivery_identity(
                user_uuid, "app", self.platforms)
        return target

    async def plan(
        self, stream_id: str, rhythm: TaggedRhythm, decision: RhythmDecision
    ) -> Optional[FiringPlan]:
        key = RhythmKey(stream_id=stream_id, rhythm_id=rhythm.rhythm_id)
        if rhythm.kind == "curation_due":
            return FiringPlan(key=key, kind=rhythm.kind, due_at=decision.due_at)

        target = await self._resolve_target(stream_id)
        if target is None:
            logger.debug("no deliverable platform for user %s, skipping", stream_id)
            return None
        platform, platform_user_id = target
        return FiringPlan(
            key=key,
            kind=rhythm.kind,
            due_at=decision.due_at,
            actor=CHAIR_ID,
            target=DeliveryTarget(platform=platform, target_id=platform_user_id),
            reason=decision.reason,
            # if the user speaks between this plan and the stream lock, the
            # check-in is stale: cancel before generation. USER-level on
            # purpose (not the stimulus stream): with rooms, a message in
            # today's room must cancel a check-in planned against any stream
            precondition=UserQuietSince(stream_id, than=self._now()),
        )

    async def build(self, plan: FiringPlan) -> Stimulus:
        # pulse rhythms are per-USER (the rhythm key stays the user uuid);
        # what changed in phase 2b is WHERE a check-in lands: today's daily
        # room, lazily resolved at fire time - so an overnight tick opens
        # (and thereby closes yesterday's) room exactly like a user message
        # would. curation is not a conversation and keeps the user key.
        user_uuid = plan.key.stream_id
        if plan.kind == "curation_due":
            return Stimulus(
                kind="curation_due",
                stream_id=user_uuid,
                record_inbound=False,
                extras={"user_id": user_uuid},
            )

        from src.services.rooms import get_room_store
        from src.services.workspace.agenda import user_today

        def resolve() -> str:
            room = get_room_store().current_room(
                user_uuid, user_today(user_uuid))
            return room["room_uuid"]
        stream_id = await asyncio.to_thread(resolve)

        return Stimulus(
            kind="scheduled_tick",
            stream_id=stream_id,
            platform=plan.target.platform,
            record_inbound=False,
            scope="dm",
            audience=CHAIR_ID,
            target=plan.target,
            reason=plan.reason,
            precondition=plan.precondition,
            extras={"user_id": user_uuid},
        )


def build_pulse(
    *,
    orchestrator,
    user_manager: UserManager,
    curator=None,
    platforms: Optional[List[str]] = None,
    store=None,
    now=aware_utc_now,
    presence=None,
) -> Pulse:
    """chordial's ambient loop: five-minute cycles, per-user unified recency,
    the don't-nag gate stack (onboarding -> quiet hours -> cadence, first
    denial wins). the pulse store is in-memory: horizons rebuild from the
    event log after a restart, so nothing user-visible is lost (a durable
    PulseStore is a later phase, alongside the other SQL adapters).

    the check-in beat is per-user since 6c: the taper stretches it as
    scorecard history earns quiet (src/services/taper.py). WHERE the beat
    lands is presence-aware since 7a: `presence` (the app interface's
    trichotomy) routes each firing to the desk or the phone - see
    ChordialStimulusFactory."""
    from src.services import taper
    gates = [
        ScheduledOnly(OnboardingGate(user_manager)),
        ScheduledOnly(QuietHoursGate(
            Config.QUIET_HOURS_START,
            Config.QUIET_HOURS_END,
            tz_of=user_manager.get_user_timezone,
        )),
        # the re-engagement ladder (replacing the doubling backoff, whose
        # caps went permanently silent after ~a day of trying): the house
        # spec parses at build time so a broken env var fails the boot, not
        # a 3am firing. per-user overrides resolve per check. the gate's
        # event window stays the library default - it is cadence-counting
        # depth, not prompt history, and the gate widens it to cover
        # whatever ladder a user stores.
        ScheduledOnly(CadenceGate(
            cadence_of(Cadence.parse(Config.OUTREACH_CADENCE)),
            proactive_message_type="scheduled",
            tz_of=user_manager.get_user_timezone,
        )),
        # the meter's soft gate (phase 7c): past the soft budget, proactive
        # outreach waits. deliberately LAST - the only gate that reads the
        # ledger, so the cheap denials win first. off by default (knobs 0).
        ScheduledOnly(BudgetGate()),
    ]
    return Pulse(
        source=ChordialPulseSource(user_manager, curator=curator,
                                   checkin_minutes=taper.checkin_minutes),
        factory=ChordialStimulusFactory(user_manager, platforms=platforms,
                                        now=now, presence=presence),
        engine=orchestrator,
        store=store or InMemoryPulseStore(),
        # rhythm keys are USERS: recency anchors and gate arithmetic must
        # span every room (a reply in today's room answers yesterday's nudge)
        events_for=lambda sid: ReadOnlyEventReader(
            SqlUserEvents(sid, visibility=chordial_visibility)
        ),
        gates=gates,
        now=now,
    )
