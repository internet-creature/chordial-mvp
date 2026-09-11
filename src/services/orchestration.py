"""chordial's wiring of the dainframe engine (phase 3 of the extraction).

what used to be a 600-line bespoke orchestrator is now the library engine
(dainframe.core.Orchestrator) plus four small chordial-shaped parts, each
plugged into a seam the engine defines:

- ChordialDirector   (§4.2)  WHO speaks, with what per-line obligations: the
                             dm/group/mention rules, the curator's silent
                             line, the scheduler's pending line
- ChordialContext    (§4.4)  briefing enrichment: user profile lookup, the
                             agenda's ambient digest, the briefing-kind map
- ChordialHooks      (§4.5)  the platform-switch courtesy + the completion
                             reconciler - product niceties, not engine logic
- ChordialDeliverer  (§4.3)  the router adapter, with the group-chat breathing
                             gap between multi-speaker lines

recording goes through SqlEventStore (the contract-proven adapter), delivery
is confirmed-before-recorded by the engine, and per-stream serialization is
the engine's StreamCoordinator (replacing ChatService's ad-hoc user locks as
the deep guarantee; the chat-service lock remains as an outer courtesy).

the legacy per-message compression hook (ENABLE_COMPRESSION, off by default,
pre-event-log vintage) did not survive the swap - CompressorService remains
for whatever comes next, but nothing invokes it on the chat path.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
import random
from typing import Iterable, Optional

import uuid

from dainframe.core import (
    BoundedDeliveryLedger,
    BriefingContext,
    DeliveryReceipt,
    DeliveryRequest,
    EventContext,
    EventQuery,
    NewEvent,
    NewPendingDelivery,
    Orchestrator,
    PendingDelivery,
    Script,
    ScriptLine,
    Stimulus,
)
from dainframe.core.events import Event as DfEvent, EventStore
from dainframe.loop.agent_loop import ExecutedAction

from config import Config
from src.managers.event_log import format_action_line
from src.managers.event_store_adapter import SqlEventStore
from src.managers.helper_state_manager import HelperStateManager
from src.personas import CHAIR_ID
from src.services.identity import user_of_stimulus
from src.managers.user_manager import UserManager

logger = logging.getLogger(__name__)


# the one-time courtesy sent to a platform the conversation just walked away
# from. asterisk stage-direction styling matches the persona's voice; on
# plain-text platforms the asterisks read as deliberate emphasis.
SWITCH_NOTICE = "*(pssst — we're chatting over on {platform} now. see you there 💜)*"


def chordial_visibility(event: DfEvent, viewer: str) -> bool:
    """the dm privacy rule on dainframe events: group events are visible to
    every helper; a dm event only to the helper the 1:1 is with (as its
    partner, or as its own author). mirrors chordial Event.visible_to."""
    if event.scope != "dm":
        return True
    return event.audience == viewer or event.author == viewer


# --- the director (§4.2): rules only this phase -------------------------------

# room types fold into the FAMILY the persona cards speak (card.default_rooms
# says 'daily'/'cycle'/'project'/'adhoc'): the grandfathered legacy stream is
# a daily room, and both cycle rooms (retro + planning, phase 6b) are the
# cycle family - the same cast sits down for looking back and planning ahead.
_ROOM_FAMILY = {
    "legacy": "daily",
    "cycle_retro": "cycle",
    "cycle_planning": "cycle",
}


class ChordialDirector:
    """casts the script of speakers for one activation, through the
    three-layer spine (ROOMS_DESIGN.md §3): deterministic rules resolve
    everything they can for free (dms, mentions, introductions, a
    single-candidate cast); only the genuine gray zone - a shared-room
    message with no mention and several plausible lanes - reaches the
    decider, one tiny enum-constrained utility call. the director must
    never break the conversation: every conversational path that empties,
    and every decider failure, falls back to the chair.

    `decider` is optional (None = rules-only, the pre-3b behavior);
    `room_type_of` resolves a stream to its room type for card eligibility;
    `deliverable_speakers` maps a platform to the set of speaker ids its
    interfaces can actually send as (None = unrestricted) - the router's
    method in production. all three injectable for tests."""

    def __init__(
        self,
        agent_ids: Iterable[str],
        helper_state_manager: Optional[HelperStateManager] = None,
        fallback: str = CHAIR_ID,
        decider=None,
        room_type_of=None,
        deliverable_speakers=None,
    ):
        self.agent_ids = set(agent_ids)
        self.helper_state_manager = helper_state_manager or HelperStateManager()
        self.fallback = fallback
        self.decider = decider
        self._room_type_of = room_type_of or self._room_type_from_store
        self._deliverable_speakers = deliverable_speakers

    @staticmethod
    async def _room_type_from_store(stream_id: str) -> str:
        """the stream's room type ('daily', 'legacy', later 'cycle'/...).
        unknown streams and lookup failures read as 'daily' - eligibility
        filtering must never take the conversation down."""
        try:
            from src.services.rooms import get_room_store

            room = await asyncio.to_thread(get_room_store().get_by_uuid, stream_id)
            return room["room_type"] if room else "daily"
        except Exception:
            logger.exception("room type lookup failed; treating as daily")
            return "daily"

    async def direct(self, stimulus: Stimulus, events) -> Script:
        kind = stimulus.kind
        if kind == "curation_due":
            # the curator acts silently: actions (none today) are the product
            return Script(lines=(
                ScriptLine(speaker="curator", response="silent", delivery="none"),
            ))
        if kind == "scheduled_tick":
            # ambient outreach rides the ordinary DIRECT path (the-dainframe
            # DESIGN.md §11.15): the engine holds the stream across
            # generation, delivery, and recording - one serialized
            # activation, no pending+confirm dance. response stays OPTIONAL:
            # a scheduled tick that generates nothing is a quiet non-event,
            # never a user-facing error
            return self._finalize(stimulus, [
                ScriptLine(
                    speaker=self.fallback,
                    response="optional",
                    delivery="direct",
                    target=stimulus.target,
                    event_context=self._dm_context(
                        stimulus, self.fallback, message_type="scheduled"
                    ),
                ),
            ])
        if kind == "stuck":
            # the "i'm stuck" turn (docs/STUCK_MODE_DESIGN.md 5.2): pip as
            # the house's background focus specialist, met or not. tool
            # output only - the card is the reply, prose is discarded (a
            # silent line with text is an errored line, and the proposals
            # it wrote are already on the episode by then)
            return self._finalize(stimulus, [
                ScriptLine(speaker="pip", response="silent", delivery="none",
                           event_context=self._dm_context(stimulus, "pip")),
            ])
        if kind == "introduction":
            speaker = stimulus.addressed[0] if stimulus.addressed else self.fallback
            return self._finalize(stimulus, [self._dm_line(stimulus, speaker)])
        if kind == "user_message":
            if stimulus.scope != "group":
                # a dm is a private 1:1 - the addressed helper is the lone voice
                speaker = stimulus.addressed[0] if stimulus.addressed else self.fallback
                return self._finalize(stimulus, [self._dm_line(stimulus, speaker)])
            return self._finalize(stimulus, await self._group_lines(stimulus, events))
        # unknown kind: cast nobody, loudly
        return Script(noop_reason=f"no chordial rule for stimulus kind '{kind}'")

    async def _group_lines(self, stimulus: Stimulus, events) -> list[ScriptLine]:
        """the shared-room user_message routing rule. @-mentions win (in
        order, deduped, active cast only, capped at 2); with no mention, the
        deterministic candidate pass runs (active cast, present in this room
        type) and the decider is consulted only when several lanes are
        plausible. a lone candidate or an absent/failed decider costs zero
        tokens: the chair fields it."""
        group_ec = EventContext(platform=stimulus.platform, scope="group")
        cast = await self.helper_state_manager.active_helpers(user_of_stimulus(stimulus))
        active_ids = {v.helper_id for v in cast if v.is_active}

        if stimulus.addressed:
            lines: list[ScriptLine] = []
            seen: set = set()
            for helper_id in stimulus.addressed:
                if (
                    helper_id in seen
                    or helper_id not in active_ids
                    or helper_id not in self.agent_ids
                ):
                    continue
                lines.append(ScriptLine(speaker=helper_id, event_context=group_ec))
                seen.add(helper_id)
                if len(lines) >= 2:
                    break
            # every mention was inactive/unknown - don't drop the message
            return lines or [ScriptLine(speaker=self.fallback, event_context=group_ec)]

        speaker = await self._pick_speaker(stimulus, active_ids, events)
        return [ScriptLine(speaker=speaker, event_context=group_ec)]

    # how much recent context the decider sees. read wider than the decider's
    # own tail: a tool-heavy previous turn interleaves action events, and the
    # decider filters to messages BEFORE slicing.
    _DECIDER_CONTEXT_LIMIT = 10

    async def _pick_speaker(self, stimulus: Stimulus, active_ids, events) -> str:
        """the no-mention gray zone: deterministic candidates first, decider
        only on genuine ambiguity, chair on every other path."""
        candidates = await self._candidates(stimulus, active_ids)
        if len(candidates) <= 1 or self.decider is None or not stimulus.content:
            return candidates[0] if candidates else self.fallback

        # the engine hands an EventReader (read/latest), never a list -
        # materialize the recent window here, fully guarded: a failed read
        # means deciding with less context, never falling back to the chair
        recent = []
        if events is not None:
            try:
                recent = await events.read(
                    EventQuery(message_limit=self._DECIDER_CONTEXT_LIMIT))
            except Exception:
                logger.exception(
                    "decider context read failed; deciding without context")

        chosen = await self.decider.decide(
            message=stimulus.content,
            candidate_ids=candidates,
            chair_id=self.fallback,
            recent=recent,
            user_uuid=user_of_stimulus(stimulus),
            platform=stimulus.platform,
        )
        # the decider's contract: a validated candidate or None - but the
        # director re-checks anyway; nothing may cast an unbuilt agent
        if chosen in candidates:
            return chosen
        return self.fallback

    async def _candidates(self, stimulus: Stimulus, active_ids) -> list[str]:
        """who could plausibly field this message: the user's active cast,
        restricted to built agents, to cards present in this room type
        (card.default_rooms; 'legacy' rooms count as daily), and to speakers
        the platform can actually deliver as (every current platform
        attributes in-band and serves the whole council, so the filter is a
        no-op today - it stays for any future platform with per-speaker
        accounts). the chair leads the list and is always a candidate."""
        from src.personas import load_personas

        room_type = await self._room_type_of(stimulus.stream_id)
        room_type = _ROOM_FAMILY.get(room_type, room_type)
        deliverable = None
        if self._deliverable_speakers is not None and stimulus.platform:
            try:
                deliverable = self._deliverable_speakers(stimulus.platform)
            except Exception:
                logger.exception("deliverable-speaker lookup failed; "
                                 "not restricting candidates")
        cards = load_personas()
        eligible = []
        for helper_id in sorted(active_ids & self.agent_ids):
            if helper_id == self.fallback:
                continue
            if deliverable is not None and helper_id not in deliverable:
                continue
            card = cards.get(helper_id)
            if card is None or room_type in card.default_rooms:
                eligible.append(helper_id)
        return [self.fallback] + eligible if self.fallback in self.agent_ids \
            else eligible or [self.fallback]

    def _dm_line(self, stimulus: Stimulus, speaker: str) -> ScriptLine:
        # the resolved speaker also names the private channel, so the legacy
        # single-helper dm stays visible to the chair's own privacy window
        return ScriptLine(
            speaker=speaker,
            event_context=self._dm_context(stimulus, speaker),
        )

    @staticmethod
    def _dm_context(
        stimulus: Stimulus, speaker: str, message_type: str = "conversation"
    ) -> EventContext:
        return EventContext(
            platform=stimulus.platform,
            scope="dm",
            audience=speaker,
            outbound_message_type=message_type,
        )

    def _finalize(self, stimulus: Stimulus, lines: list[ScriptLine]) -> Script:
        """the director's hard guardrail: cap at 2 lines, recast any speaker
        without an agent to the fallback, and never return empty on a
        conversational path."""
        kept = []
        for line in lines[:2]:
            if line.speaker in self.agent_ids:
                kept.append(line)
        if not kept:
            fallback_ec = (
                EventContext(platform=stimulus.platform, scope="group")
                if stimulus.scope == "group"
                else self._dm_context(stimulus, self.fallback)
            )
            kept = [ScriptLine(speaker=self.fallback, event_context=fallback_ec)]
        return Script(lines=tuple(kept))


# --- briefing enrichment (§4.4) -----------------------------------------------


def presence_line(presence: Optional[str], platform: Optional[str],
                  idle_minutes=None) -> Optional[str]:
    """one ambient line for a scheduled tick (§5.2): whether they're at
    the desk, and where this word lands. None when the plan carried no
    presence (dev rigs, older plans)."""
    if presence not in ("active", "idle", "absent"):
        return None
    where = platform or "the app"
    if presence == "active":
        return "they're at the desk right now; this lands in the app."
    if presence == "idle":
        idle = (f" ({int(idle_minutes)} min)"
                if isinstance(idle_minutes, (int, float))
                and not isinstance(idle_minutes, bool) else "")
        return (f"they're connected but idle at the desk{idle}; "
                f"this lands on {where}.")
    return (f"they're away from the desk; this lands on {where}, "
            "phone-sized.")


class ChordialContext:
    """fills the chordial-shaped parts of a briefing: the user profile (one
    query when the caller didn't resolve it - e.g. the scheduler), the agenda
    digest as ambient context, and the stimulus-kind -> briefing-kind map."""

    def __init__(self, user_manager: UserManager, agenda_service=None):
        self.user_manager = user_manager
        self.agenda_service = agenda_service

    async def enrich(self, stimulus: Stimulus, line: ScriptLine) -> BriefingContext:
        # curation needs no conversation context - keep it light
        if stimulus.kind == "curation_due":
            return BriefingContext(
                kind="curation",
                extras={"user_id": user_of_stimulus(stimulus)})

        user_uuid = user_of_stimulus(stimulus)
        user_name = stimulus.extras.get("user_name")
        user_timezone = stimulus.extras.get("user_timezone")
        user_pronouns = stimulus.extras.get("user_pronouns")
        if user_timezone is None:
            (
                user_name,
                user_timezone,
                user_pronouns,
            ) = await self.user_manager.get_user_profile(user_uuid)

        if stimulus.kind == "introduction":
            briefing_kind = "introduction"
        elif stimulus.kind == "scheduled_tick":
            briefing_kind = "scheduled_checkin"
        elif stimulus.kind == "stuck":
            briefing_kind = "stuck"
        else:
            briefing_kind = "user_message"

        # the day's beat picks the posture (docs/FOCUS_DOGFOOD_DESIGN.md
        # §13.3): the morning brief points at ONE first thing when the day
        # has one, asks for the day's shape when it doesn't. chosen here,
        # deterministically, so the prompt only renders it. the plain
        # check-in carries no posture and keeps its exact bytes.
        posture_extras: dict = {}
        prelude: Optional[str] = None
        today = None
        if stimulus.extras.get("beat") == "morning":
            posture, first_thing, prelude = await asyncio.to_thread(
                self._morning_posture, user_uuid)
            posture_extras = {"checkin_posture": posture,
                              "first_thing": first_thing}
        elif briefing_kind == "scheduled_checkin":
            # every other tick is block-aware (§5.3): the day's snapshot
            # picks mid_block / stuck / untouched / between / wrapped /
            # quiet_day, and the prompt renders that one shape. ONE
            # snapshot: the digest below renders from the same read, so
            # the posture and the ambient block can never disagree about
            # whether a clock is running (sol, #88)
            today = await asyncio.to_thread(self._day_snapshot, user_uuid)
            if today is not None:
                from src.services import focus_day
                posture, detail = focus_day.checkin_posture(today)
                posture_extras = {"checkin_posture": posture,
                                  "posture_detail": detail}

        stuck_extras: dict = {}
        if briefing_kind == "stuck":
            # the stuck brief (STUCK_MODE_DESIGN.md 5.2) reads the same
            # snapshot the digest renders from; the episode id rides to the
            # tool through the briefing extras
            today = await asyncio.to_thread(self._day_snapshot, user_uuid)
            stuck_extras = {
                "stuck_episode_id": stimulus.extras.get("stuck_episode_id"),
                "stuck_generation": stimulus.extras.get("stuck_generation", 1),
                "stuck_rejected_kinds": list(
                    stimulus.extras.get("stuck_rejected_kinds") or []),
                "stuck_surface": stimulus.extras.get("stuck_surface") or "companion",
                "stuck_task_id": stimulus.extras.get("stuck_task_id"),
            }

        ambient = self._compose_ambient(
            user_uuid,
            stream_id=stimulus.stream_id,
            include_agenda=(briefing_kind != "introduction"),
            prelude=prelude,
            today=today,
        )
        if briefing_kind == "stuck":
            brief = await asyncio.to_thread(
                self._stuck_brief, user_uuid, today, stuck_extras)
            if brief:
                ambient = "\n\n".join(p for p in (ambient, brief) if p)
        if briefing_kind == "scheduled_checkin":
            # where this word lands, and whether they're there (§5.2)
            line = presence_line(stimulus.extras.get("presence"),
                                 stimulus.platform,
                                 stimulus.extras.get("idle_minutes"))
            if line:
                ambient = "\n\n".join(p for p in (ambient, line) if p)

        return BriefingContext(
            kind=briefing_kind,
            # an introduction stays light (no agenda digest) but must still
            # see the privacy-safe previous-room summary: a multi-day intro
            # starts its second day in a FRESH room with an empty window, and
            # without the summary it would re-introduce itself from scratch
            ambient_context=ambient,
            extras={
                "user_id": user_uuid,
                "user_name": user_name,
                "user_timezone": user_timezone or "UTC",
                "user_pronouns": user_pronouns,
                **posture_extras,
                **stuck_extras,
            },
        )

    @staticmethod
    def _stuck_brief(user_uuid: str, today, extras: dict) -> Optional[str]:
        """the stuck brief block, guarded: a failed read costs the brief
        (the turn still runs on the ambient it has), never the turn."""
        try:
            from src.services import stuck
            recent = stuck.StuckStore().recent_summaries(
                user_uuid, exclude_uuid=extras.get("stuck_episode_id"))
            ev = stuck.gather(user_uuid, today=today, recent=recent,
                              focus_task_id=extras.get("stuck_task_id"))
            return stuck.render_brief(
                ev, surface=extras.get("stuck_surface") or "companion",
                rejected_kinds=extras.get("stuck_rejected_kinds") or ())
        except Exception:
            logger.exception("stuck brief failed for %s", user_uuid)
            return None

    def _morning_posture(self, user_uuid: str) -> tuple:
        """(posture, first_thing) for the brief. pure db reads, guarded:
        any failure is the open posture (ask about the day) rather than a
        broken morning. the agenda payload gives today's and carried-over
        tasks; the active cycle's open commitments are the fallback."""
        from src.services.beats import morning_posture, pick_first_thing

        payload = None
        commitments: list = []
        try:
            if self.agenda_service is not None:
                payload = self.agenda_service.get_payload(user_uuid)
        except Exception:
            logger.exception("agenda read failed for the brief; open posture")
        try:
            from src.services.cycles import CycleStore
            from src.services.workspace import get_store

            active = get_store().active_cycle(user_uuid)
            if active:
                commitments = CycleStore().list_commitments(
                    user_uuid, cycle_id=active["id"], include_closed=False)
        except Exception:
            logger.exception("cycle read failed for the brief; tasks only")
        first_thing = pick_first_thing(payload, commitments)
        # the brief wraps yesterday (§13.3): the focus day digest for the
        # previous local day rides as the ambient prelude. guarded on its
        # own - a failed read costs the wrap-up, never the brief
        yesterday = None
        try:
            from src.services import focus_day
            from src.services.workspace.agenda import user_today

            yesterday = focus_day.digest(
                user_uuid, user_today(user_uuid) - timedelta(days=1))
        except Exception:
            logger.exception("yesterday's digest failed; brief continues "
                             "without")
        return morning_posture(first_thing), first_thing, yesterday

    @staticmethod
    def _day_snapshot(user_uuid: str):
        """today's FocusDay for a follow-through tick, guarded: any
        failure is None - no posture (the plain check-in, byte-identical
        to before) and no digest."""
        try:
            from src.services import focus_day

            return focus_day.snapshot(user_uuid)
        except Exception:
            logger.exception("day snapshot failed for %s; plain check-in",
                             user_uuid)
            return None

    def _compose_ambient(self, user_uuid: str,
                         stream_id: Optional[str] = None,
                         include_agenda: bool = True,
                         prelude: Optional[str] = None,
                         today=None) -> Optional[str]:
        """the volatile 'now' zone, shaped by the room the turn is in.
        daily rooms hydrate from the day-shaped past (the previous
        daily/legacy summary - never a retro that happened to close last)
        plus the agenda digest. cycle rooms (phase 6b) hydrate from the
        cycle-shaped past instead: an orientation line, and for planning
        the retro's compressed consequences + the filed scorecard. pure db
        reads, fully guarded - any failure degrades to None (or to the
        daily shape), i.e. today's exact prompt bytes."""
        try:
            from src.services.rooms import get_room_store

            store = get_room_store()
            room = store.get_by_uuid(stream_id) if stream_id else None
            if room is not None and room["room_type"] in (
                    "cycle_retro", "cycle_planning"):
                return self._compose_cycle_ambient(user_uuid, room,
                                                   include_agenda)

            parts = []
            # yesterday's compressed consequences (ROOMS_DESIGN §4): the new
            # room reads the previous room's summary, never its transcript -
            # hydrated even on deployments without an agenda service
            summary = store.latest_summary(user_uuid)
            if summary and summary["content"]:
                parts.append("previously:\n" + summary["content"])
            if prelude:
                parts.append(prelude)
            digest = (self.agenda_service.get_digest(user_uuid)
                      if include_agenda and self.agenda_service else None)
            if digest:
                parts.append(digest)
            # the day so far (§5.1): what the desk has seen - banked runs,
            # the clock, finishes, drifts - on ticks AND user turns, so she
            # knows the day when you talk to her. a tick passes the
            # snapshot its posture came from; a user turn reads one here.
            # guarded on its own
            if include_agenda:
                try:
                    from src.services import focus_day
                    day_digest = (focus_day.render(today) if today is not None
                                  else focus_day.digest(user_uuid))
                except Exception:
                    logger.exception("focus day digest failed; briefing "
                                     "continues without")
                    day_digest = None
                if day_digest:
                    parts.append(day_digest)
            # the arc's posture (phase 6c): present only once quiet has
            # been EARNED - untapered users (every fresh user) get their
            # exact pre-6c prompt bytes. guarded on its own: this line is
            # optional, and its failure must cost the line, never the
            # already-composed summary and agenda around it
            try:
                from src.services import taper
                posture = taper.ambient_line_for(user_uuid)
            except Exception:
                logger.exception("taper posture read failed; "
                                 "briefing continues without")
                posture = None
            if posture:
                parts.append(posture)
            return "\n\n".join(parts) if parts else None
        except Exception:
            logger.exception("failed composing ambient context; continuing without")
            return None

    def _compose_cycle_ambient(self, user_uuid: str, room: dict,
                               include_agenda: bool) -> Optional[str]:
        """a cycle room's briefing zone. the retro stays lean - edwin's
        presentation of the card is already IN the transcript, so ambient
        only orients. planning gets the evidence base: the retro's summary
        (this cycle's, by subject - never whichever retro closed last) and
        the card render, plus the agenda (planning looks at the live
        board)."""
        from src.services import cycle_scorer
        from src.services.rooms import get_room_store

        subject_id = room.get("subject_id") or ""
        assessment = cycle_scorer.assessment_for(user_uuid, subject_id)
        title = ((assessment.get("detail") or {}).get("cycle") or {}
                 ).get("title") if assessment is not None else None
        if not title:
            title = self._cycle_title(user_uuid, subject_id) or subject_id

        if room["room_type"] == "cycle_retro":
            # the briefing must match the transcript: only claim a card is
            # on the table when one actually filed (a cardless retro exists
            # when scoring failed or no scorer is wired)
            if assessment is not None:
                return (f"this room is the retrospective for cycle "
                        f"'{title}' ({subject_id}) - a look back at what "
                        f"actually happened, with the filed scorecard on "
                        f"the table. edwin presented the card when the "
                        f"room opened.")
            return (f"this room is the retrospective for cycle "
                    f"'{title}' ({subject_id}) - a look back at what "
                    f"actually happened. NO scorecard has been filed for "
                    f"this cycle yet - never invent, estimate, or imply "
                    f"one; speak from the conversation and the ledger "
                    f"tools only.")

        parts = [f"this room is the planning that follows cycle "
                 f"'{title}' ({subject_id}) - the next cycle gets shaped "
                 f"here, against the evidence of the last one."]
        retro_summary = get_room_store().summary_of(
            user_uuid, "cycle_retro", subject_id)
        if retro_summary:
            parts.append("the retrospective settled:\n" + retro_summary)
        if assessment is not None:
            parts.append("the scorecard, as filed:\n"
                         + cycle_scorer.render_assessment(assessment))
        else:
            parts.append("NO scorecard has been filed for this cycle - "
                         "never invent or estimate one.")
        digest = (self.agenda_service.get_digest(user_uuid)
                  if include_agenda and self.agenda_service else None)
        if digest:
            parts.append(digest)
        return "\n\n".join(parts)

    @staticmethod
    def _cycle_title(user_uuid: str, subject_id: str) -> Optional[str]:
        from src.database.database import get_db
        from src.database.models import Cycle
        from src.services.workspace.vocab import parse_public_id

        parsed = parse_public_id(subject_id)
        if parsed is None or parsed[0] != "cycle":
            return None
        with get_db() as db:
            return db.query(Cycle.title).filter(
                Cycle.id == parsed[1],
                Cycle.user_uuid == user_uuid).scalar()


# --- turn hooks (§4.5): the product niceties ----------------------------------


class ChordialHooks:
    """the platform-switch courtesy and the completion reconciler. both fully
    guarded: a hook failure must never affect the reply the user already got
    (the engine additionally isolates every hook call)."""

    def __init__(
        self,
        user_manager: UserManager,
        reconciler=None,
        deliver=None,
        message_window: Optional[int] = None,
    ):
        self.user_manager = user_manager
        self.reconciler = reconciler
        # (platform, target_id, text, speaker) -> bool; router.deliver_as
        self.deliver = deliver
        self.message_window = message_window or Config.MAX_HISTORY_MESSAGES

    # -- the platform-switch courtesy ------------------------------------------

    async def after_inbound_recorded(
        self,
        stimulus: Stimulus,
        store: EventStore,
        prev_user_event: Optional[DfEvent],
    ) -> None:
        """the conversation just walked from platform A to platform B: send
        the one-time courtesy notice to A. structurally self-deduping - after
        this note, the last user message IS on B, so the trigger can't refire
        until the user speaks on A again. the engine holds the stream lock for
        the whole activation, so the check-then-write pair cannot interleave
        with another turn."""
        try:
            if stimulus.kind != "user_message":
                return
            if self.deliver is None or prev_user_event is None:
                return
            old_platform = prev_user_event.platform
            if not old_platform or old_platform == stimulus.platform:
                return

            # only notify a link we could actually reach
            identity = await self.user_manager.get_identity(
                user_of_stimulus(stimulus), old_platform
            )
            if identity is None or not identity[1]:
                return
            platform_user_id = identity[0]

            # belt-and-braces: if a switch note for A was already recorded
            # after A's last user message, another handler beat us to it
            recent = await store.read(EventQuery(message_limit=self.message_window))
            for event in reversed(recent):
                if event.event_id == prev_user_event.event_id:
                    break
                if (
                    event.kind == "note"
                    and event.metadata.get("note_type") == "platform_switch"
                    and event.platform == old_platform
                ):
                    return

            notice = SWITCH_NOTICE.format(platform=stimulus.platform)
            # note first (the at-most-once record), then best-effort delivery -
            # a transient miss is an acceptable cost for a cosmetic courtesy
            await store.append(NewEvent(
                author_type="system", author="system", kind="note",
                content=notice, platform=old_platform,
                metadata={"note_type": "platform_switch", "to": stimulus.platform},
            ))
            delivered = await self.deliver(
                old_platform, platform_user_id, notice, speaker=CHAIR_ID
            )
            if not delivered:
                logger.info(
                    "switch notice to %s not delivered (transient or dead link)",
                    old_platform,
                )
        except Exception as e:
            logger.error(
                "platform-switch notice failed for user %s: %s",
                user_of_stimulus(stimulus), e
            )

    # -- the completion reconciler ---------------------------------------------

    async def after_turn(self, stimulus: Stimulus, store: EventStore, result) -> None:
        """after a delivered user turn, reconcile any tasks the user mentioned
        finishing in passing (the companion's warmth can crowd out the
        bookkeeping; this narrow pass catches what it missed). Done marks are
        recorded as the chair's own actions, in the turn's scope, so the replay
        reads coherently next turn."""
        if (
            self.reconciler is None
            or stimulus.kind != "user_message"
            or not stimulus.content
            or not result.any_delivered
        ):
            return
        try:
            recent = await store.read(EventQuery(message_limit=self.message_window))
            # drop the just-recorded inbound message; it's passed separately
            recent = [
                e
                for e in recent
                if not (
                    e.kind == "message"
                    and e.author_type == "user"
                    and e.content == stimulus.content
                )
            ][-6:]
            reconcile_result = await self.reconciler.reconcile(
                user_uuid=user_of_stimulus(stimulus),
                stream_id=stimulus.stream_id,
                platform=stimulus.platform,
                message_text=stimulus.content,
                recent=recent,
            )
            for action in reconcile_result.actions:
                if action.is_error:
                    continue
                await store.append(NewEvent(
                    author_type="agent", author=CHAIR_ID, kind="action",
                    content=format_action_line(
                        action.name, dict(action.input), action.result_content
                    ),
                    platform=stimulus.platform,
                    scope=stimulus.scope,
                    audience=stimulus.audience,
                    metadata={
                        "tool": action.name,
                        "input": dict(action.input),
                        "result": action.result_content[:1000],
                    },
                ))
        except Exception as e:
            logger.error(
                "completion reconcile failed for user %s: %s",
                user_of_stimulus(stimulus), e
            )


# --- delivery (§4.3): the router adapter --------------------------------------


class _PacedDeliverer:
    """the group-chat breathing gap: a second (or later) delivered line in the
    same activation waits a beat first, so a two-speaker script doesn't land
    as a machine-gun burst. only group scripts cast multiple lines, so the
    pacing is effectively group-only. subclasses implement _send.

    pacing state is keyed PER ACTIVATION (a bounded recent-ids set), so
    another user's interleaved delivery can't make an activation lose its
    gap. the set is small: an activation needs pacing only while its own
    (short, serialized) script is still delivering."""

    _SEEN_CAP = 128

    def __init__(self):
        self._delivered_activations: dict[str, None] = {}  # insertion-ordered

    async def deliver(self, request: DeliveryRequest) -> Optional[DeliveryReceipt]:
        if request.activation_id in self._delivered_activations:
            await asyncio.sleep(random.uniform(2.0, 5.0))
        ok = await self._send(request)
        if not ok:
            return None
        self._delivered_activations[request.activation_id] = None
        while len(self._delivered_activations) > self._SEEN_CAP:
            self._delivered_activations.pop(
                next(iter(self._delivered_activations))
            )
        return DeliveryReceipt()

    async def _send(self, request: DeliveryRequest) -> bool:
        raise NotImplementedError


class ChordialDeliverer(_PacedDeliverer):
    """adapts MessageRouter.deliver_as to the engine's Deliverer protocol."""

    def __init__(self, router):
        super().__init__()
        self.router = router

    async def _send(self, request: DeliveryRequest) -> bool:
        return await self.router.deliver_as(
            request.target.platform,
            request.target.target_id,
            request.text,
            speaker=request.speaker,
            # the room this line belongs to (phase 6b): rides the payload on
            # in-band-attributing platforms so one room's lines never render
            # inside another room's view
            stream_id=request.stream_id,
        )


# --- the delivery ledger ------------------------------------------------------
# BoundedDeliveryLedger was born here and upstreamed to the dainframe in
# phase 5 - chordial now uses the library class it proved out.


# --- assembly -----------------------------------------------------------------


def chordial_action_line(speaker: str, action: ExecutedAction) -> str:
    """the engine's action events render with chordial's frozen one-liner
    format - deterministic serialization, write-once, replayed verbatim."""
    return format_action_line(action.name, dict(action.input), action.result_content)


def build_orchestrator(
    *,
    agents: dict,
    user_manager: UserManager,
    agenda_service=None,
    reconciler=None,
    router=None,
    deliver=None,
    helper_state_manager: Optional[HelperStateManager] = None,
    message_window: Optional[int] = None,
    decider=None,
) -> Orchestrator:
    """wire the dainframe engine with chordial's director, context, hooks,
    store, and deliverer. `router` is the production path (speaker-aware
    delivery + the switch notice); `deliver` is a raw (platform, target, text,
    speaker) -> bool awaitable for tests that don't want a router; `decider`
    is the optional speaking-policy gray-zone call (None = rules only)."""
    window = message_window or Config.MAX_HISTORY_MESSAGES
    deliver_fn = deliver if deliver is not None else (
        router.deliver_as if router is not None else None
    )
    return Orchestrator(
        agents=agents,
        store_factory=lambda sid: SqlEventStore(sid, visibility=chordial_visibility),
        director=ChordialDirector(
            agents.keys(), helper_state_manager=helper_state_manager,
            decider=decider,
            deliverable_speakers=(
                router.deliverable_speakers if router is not None else None
            ),
        ),
        context_provider=ChordialContext(user_manager, agenda_service),
        hooks=ChordialHooks(
            user_manager,
            reconciler=reconciler,
            deliver=deliver_fn,
            message_window=window,
        ),
        deliverer=(
            ChordialDeliverer(router) if router is not None
            else (_CallableDeliverer(deliver_fn) if deliver_fn else None)
        ),
        ledger=BoundedDeliveryLedger(),
        message_window=window,
        action_formatter=chordial_action_line,
    )


class _CallableDeliverer(_PacedDeliverer):
    """test convenience: wrap a bare (platform, target, text, speaker) -> bool
    awaitable as a Deliverer, with the same pacing as the router path."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    async def _send(self, request: DeliveryRequest) -> bool:
        return await self.fn(
            request.target.platform,
            request.target.target_id,
            request.text,
            speaker=request.speaker,
        )
