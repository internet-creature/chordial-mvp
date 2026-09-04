"""preference tools: let the user reconfigure the bot through conversation.

since chat is the only UI, configuration should *be* conversation - "call me
Dee" or "my timezone is US/Pacific" should just work. this covers the settings
that take effect immediately today; schedule/quiet-hours wiring lands with
scheduler v2.
"""
import logging

import pytz

from dainframe.pulse import Cadence

from config import Config
from src.services.beats import canonical_morning_time, morning_time_allowed

from src.managers.user_manager import UserManager
from src.utils.timezone_utils import canonicalize_timezone
from dainframe.providers.types import ToolDef
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool
from src.services.identity import user_of_context

logger = logging.getLogger(__name__)

_users = UserManager()

_VALID_PERSONALITIES = {"friendly", "professional", "cheerful", "calm"}


async def _set_preference(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    updates: dict = {}
    notes: list[str] = []

    name = tool_input.get("preferred_name")
    if name:
        updates["preferred_name"] = name.strip()
        notes.append(f"call you {name.strip()}")

    pronouns = tool_input.get("pronouns")
    if pronouns:
        # stored verbatim, deliberately unvalidated - there is no list of
        # acceptable answers here, and rejecting one would be the single most
        # alienating thing this tool could do
        updates["pronouns"] = pronouns.strip()
        notes.append(f"use {pronouns.strip()} for you")

    tz = tool_input.get("timezone")
    if tz:
        try:
            pytz.timezone(tz)
        except pytz.UnknownTimeZoneError:
            return (
                f"'{tz}' isn't a timezone i recognize. use an IANA name like "
                "'America/Los_Angeles', 'America/New_York', or 'Europe/London'."
            )
        # legacy aliases ("US/Pacific") validate under pytz but break
        # stdlib-zoneinfo consumers - store the canonical name instead
        tz = canonicalize_timezone(tz)
        updates["timezone"] = tz
        notes.append(f"set your timezone to {tz}")

    personality = tool_input.get("bot_personality")
    if personality:
        personality = personality.lower().strip()
        if personality not in _VALID_PERSONALITIES:
            return f"i can be one of: {', '.join(sorted(_VALID_PERSONALITIES))}."
        updates["bot_personality"] = personality
        notes.append(f"switch my style to {personality}")

    # everything validates BEFORE anything writes: one tool call must never
    # half-apply (a saved tether beside a rejected cadence)
    schedule_updates: dict = {}

    tether = tool_input.get("rewind_tether")
    if tether is not None:
        # the rewind tether is separately opt-in (REWIND_DESIGN section 8):
        # linking a phone platform never by itself turns on pings about
        # quiet focus blocks - only this explicit ask does
        schedule_updates["rewind_tether"] = bool(tether)
        notes.append(
            "ping your phone when a focus block runs quiet" if tether
            else "keep quiet-block questions on the desk only")

    cadence = tool_input.get("outreach_cadence")
    if cadence:
        cadence = cadence.strip()
        if cadence.lower() == "default":
            # None (not a deleted key) so the wiring's isinstance check
            # falls through to the house schedule
            schedule_updates["outreach_cadence"] = None
            notes.append("check in on the usual schedule again")
        else:
            try:
                parsed = Cadence.parse(cadence)
            except ValueError as err:
                return (
                    f"that cadence didn't parse ({err}). the format is "
                    "comma-separated waits like '1d x3, 1w x3, 60d' - each "
                    "wait is a number plus m/h/d/w, xN for how many tries, "
                    "the last one open-ended - optionally '@ 8-11' for the "
                    "local hours it should land in. or 'default' to go back "
                    "to the usual schedule."
                )
            schedule_updates["outreach_cadence"] = str(parsed)
            notes.append(f"hold unanswered check-ins to '{parsed}'")

    morning = tool_input.get("morning_time")
    if morning:
        morning = morning.strip()
        if morning.lower() == "default":
            schedule_updates["morning_time"] = None
            notes.append("send the morning brief at the usual time again")
        elif morning.lower() == "off":
            schedule_updates["morning_time"] = "off"
            notes.append("stop sending the morning brief")
        else:
            try:
                canonical = canonical_morning_time(morning)
            except ValueError as err:
                return f"that time didn't parse ({err}). or 'off', or 'default'."
            if not morning_time_allowed(
                    canonical, Config.QUIET_HOURS_START, Config.QUIET_HOURS_END):
                return (
                    f"{canonical} is inside quiet hours "
                    f"({Config.QUIET_HOURS_START:02d}:00-"
                    f"{Config.QUIET_HOURS_END:02d}:00), when nothing is "
                    f"sent. pick a time from {Config.QUIET_HOURS_END:02d}:00 "
                    "on, or 'off'."
                )
            schedule_updates["morning_time"] = canonical
            notes.append(f"send the morning brief at {canonical}")

    if not updates and not schedule_updates:
        return "no recognized preferences to update."

    if updates:
        await _users.update_user_preferences(user_uuid, updates)
    if schedule_updates:
        await _users.merge_schedule_preferences(user_uuid, schedule_updates)
    return "updated: " + "; ".join(notes)


SET_PREFERENCE = Tool(
    definition=ToolDef(
        name="set_preference",
        description=(
            "Update the user's settings when they ask you to change how you "
            "work. Use for: what to call them (preferred_name), how to refer "
            "to them (pronouns), their timezone (so check-ins and time "
            "references land right), and your conversational style "
            "(bot_personality). Only include fields the user actually asked to "
            "change. If they tell you a new name or new pronouns at any point, "
            "call this immediately - don't wait to be asked. rewind_tether "
            "opts them in (or out) of a phone ping when a focus block sits "
            "quiet and unresolved - only set it when they explicitly ask for "
            "that. outreach_cadence pins how often you may check in when "
            "they've gone quiet - only set it when they explicitly ask to "
            "hear from you more or less often. morning_time moves (or turns "
            "off) the morning brief - the short opener you send at the start "
            "of their day."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "preferred_name": {
                    "type": "string",
                    "description": "What the user wants to be called.",
                },
                "pronouns": {
                    "type": "string",
                    "description": (
                        "The user's pronouns, exactly as they said them, e.g. "
                        "'she/her', 'they/them', 'he/they', 'any'. Free text - "
                        "record what they actually said rather than mapping it "
                        "onto an expected set."
                    ),
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name, e.g. 'America/Los_Angeles', 'Europe/London'.",
                },
                "bot_personality": {
                    "type": "string",
                    "enum": ["friendly", "professional", "cheerful", "calm"],
                    "description": "The conversational style the user prefers from you.",
                },
                "rewind_tether": {
                    "type": "boolean",
                    "description": (
                        "True to ping their phone (telegram/discord) when a "
                        "focus block has been quiet a while with its question "
                        "unanswered; false to keep those questions on the "
                        "desktop only. Separate consent - never infer it from "
                        "having a linked platform."
                    ),
                },
                "outreach_cadence": {
                    "type": "string",
                    "description": (
                        "How often you may check in while the user isn't "
                        "replying, as a ladder of waits: comma-separated "
                        "'<N><m|h|d|w>[ xK]' rungs, the last one open-ended, "
                        "optionally '@ H-H' for the local hours check-ins "
                        "should land in. 'monthly only' is '30d'; 'weekly, "
                        "mornings' is '1w @ 8-11'; '60d' holds at every two "
                        "months. 'default' returns to the usual schedule "
                        "(daily for a few days, weekly for a few weeks, then "
                        "every couple of months). Replying normally speeds "
                        "things back up unless they've pinned it like this."
                    ),
                },
                "morning_time": {
                    "type": "string",
                    "description": (
                        "When the morning brief should land, as a local "
                        "24-hour time like '08:30' - outside quiet hours "
                        "(nothing sends before 08:00 by default); 'off' turns "
                        "the brief off; 'default' returns to the house time."
                    ),
                },
            },
        },
    ),
    handler=_set_preference,
    terminal=True,  # updating a setting is a side effect - keep the reply
)
