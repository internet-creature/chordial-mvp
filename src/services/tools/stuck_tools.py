"""propose_unstuck: the stuck turn's one write (STUCK_MODE_DESIGN.md 5.3).

the model proposes; the server validates, bounds, assigns ids and stores.
the tool performs no workspace, focus, contact, file, app or playlist
mutation - effects happen only when the person accepts (section 4). it is
terminal: the tool call IS the reply, nothing goes back to the model.
"""
from __future__ import annotations

import asyncio
import logging

from dainframe.providers.types import ToolDef
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool

from src.services import stuck
from src.services.identity import user_of_context

logger = logging.getLogger(__name__)

EPISODE_KEY = "stuck_episode_id"
GENERATION_KEY = "stuck_generation"
REJECTED_KEY = "stuck_rejected_kinds"

_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(stuck.KINDS)},
        "line": {"type": "string",
                 "description": "one concrete sentence a body can do (<= 140)"},
        "enough": {"type": "string",
                   "description": "what counts as enough - a boundary a tired "
                                  "person can recognise (<= 100)"},
        "minutes": {"type": "integer", "enum": list(stuck.MINUTES_ALLOWED)},
        "why": {"type": "string",
                "description": "their why in their own words, rewritten to "
                               "the size of the step (<= 120)"},
        "why_register": {"type": "string", "enum": list(stuck.WHY_REGISTERS)},
        "action": {"type": "string", "enum": sorted(stuck.ACTIONS)},
        "thing": {
            "type": "object",
            "description": "exactly one of task_id (from the brief), plan_id "
                           "(from the brief), or a short label",
            "properties": {"task_id": {"type": "integer"},
                           "plan_id": {"type": "integer"},
                           "label": {"type": "string"}},
        },
        "prepared_step": {
            "type": "object",
            "description": "the action instruction the clock runs on; "
                           "required for start_task. never draft prose.",
            "properties": {"task_id": {"type": "integer"},
                           "next_action": {"type": "string"}},
            "required": ["task_id", "next_action"],
        },
        "rationale_codes": {"type": "array", "items": {"type": "string"},
                            "description": "evidence ids from the brief only"},
    },
    "required": ["kind", "line", "enough", "action"],
}


async def _propose_unstuck(tool_input: dict, context: ToolContext) -> str:
    episode_uuid = context.metadata.get(EPISODE_KEY)
    if not episode_uuid:
        return "error: this tool only works inside a stuck turn"
    generation = int(context.metadata.get(GENERATION_KEY) or 1)
    rejected = list(context.metadata.get(REJECTED_KEY) or [])
    user_uuid = user_of_context(context)

    def write() -> str:
        ev = stuck.gather(user_uuid)
        try:
            proposals = stuck.validate(tool_input.get("proposals"), ev, rejected)
        except stuck.ProposalError as e:
            return f"refused: {e}. call propose_unstuck again with the fix."
        settled = stuck.StuckStore().settle(episode_uuid, generation,
                                            proposals, "model")
        if settled is None:
            return ("this episode already settled (the person chose, or the "
                    "fallback stood in) - nothing more to do this turn")
        return "recorded three proposals. the card is showing; say nothing."

    return await asyncio.to_thread(write)


PROPOSE_UNSTUCK = Tool(
    definition=ToolDef(
        name="propose_unstuck",
        description=(
            "Inside a stuck turn only: record exactly three proposals of "
            "three different kinds for the person who pressed 'i'm stuck'. "
            "The server validates and assigns ids; the person sees one card "
            "at a time. This call is the whole reply - write no message."),
        input_schema={
            "type": "object",
            "properties": {
                "proposals": {"type": "array", "items": _PROPOSAL_SCHEMA,
                              "minItems": 3, "maxItems": 3},
            },
            "required": ["proposals"],
        },
    ),
    handler=_propose_unstuck,
    record_event=True,
    terminal=True,
)
