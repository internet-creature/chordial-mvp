"""the helper: a persona's chat agent.

owns everything about HOW one persona speaks - the persona/prompt construction
(PromptService, with its cache-zone layout), the tool surface, and the
tool-call loop on the persona model. the engine just hands it a briefing and
gets back text + the actions it took.

the persona itself lives in a PersonaCard (id, voice, tool allowlist); the agent
is otherwise identical from one helper to the next. swapping cards swaps who's
talking. `card.tools` None means the full registry; a list is an explicit
allowlist, resolved to a filtered view at construction (a typo raises here, not
mid-chat).

the briefing is the dainframe's: the user's identity rides in extras
(user_id/user_name/user_timezone from ChordialContext - the stream id is a
conversation key, NOT the user; see src/services/identity.py), and the event
window arrives as library events - converted at this boundary to chordial
Events so PromptService renders byte-identical prompts.
"""
from __future__ import annotations

import logging

from dainframe.core import AgentOutcome, Briefing
from dainframe.loop.agent_loop import AgentLoop
from dainframe.tools.context import ToolContext

from src.managers.event_log import Event
from src.services.identity import USER_ID_KEY, user_of_briefing
from src.personas import PersonaCard
from src.services.prompt_service import PromptService
from src.services.tools import ToolRegistry

logger = logging.getLogger(__name__)


class HelperAgent:
    def __init__(self, card: PersonaCard, agent_service: AgentLoop, tool_registry: ToolRegistry):
        self.card = card
        self.name = card.id
        self.loop = agent_service
        self.registry = tool_registry if card.tools is None else tool_registry.view(card.tools)
        self.prompts = PromptService(persona=card)

    async def act(self, briefing: Briefing) -> AgentOutcome:
        user_uuid = user_of_briefing(briefing)
        user_name = briefing.extras.get("user_name")
        user_timezone = briefing.extras.get("user_timezone") or "UTC"
        user_pronouns = briefing.extras.get("user_pronouns")
        history = [Event.from_dainframe(e) for e in briefing.events]

        if briefing.kind == "introduction":
            request = await self.prompts.build_introduction_request(
                conversation_history=history,
                user_name=user_name,
                user_uuid=user_uuid,
                user_timezone=user_timezone,
                user_pronouns=user_pronouns,
                tools=self.registry.definitions(),
                ambient_context=briefing.ambient_context,
            )
            turn_kind = "introduction"
        elif briefing.kind == "scheduled_checkin":
            request = await self.prompts.build_scheduled_request(
                conversation_history=history,
                user_name=user_name,
                user_uuid=user_uuid,
                user_timezone=user_timezone,
                user_pronouns=user_pronouns,
                tools=self.registry.definitions(),
                ambient_context=briefing.ambient_context,
                # the day's beat shapes the instruction (§13.3); absent =
                # the plain check-in, byte-identical to before
                posture=briefing.extras.get("checkin_posture"),
                first_thing=briefing.extras.get("first_thing"),
            )
            turn_kind = "scheduled"
        else:
            request = await self.prompts.build_conversation_request(
                conversation_history=history,
                user_name=user_name,
                user_uuid=user_uuid,
                user_timezone=user_timezone,
                user_pronouns=user_pronouns,
                tools=self.registry.definitions(),
                ambient_context=briefing.ambient_context,
            )
            turn_kind = "conversation"

        result = await self.loop.run(
            request,
            # the engine's activation id ties this run's tool actions to the
            # turn that produced them
            context=ToolContext(
                # the CONVERSATION key, verbatim - never the user. usage
                # events inherit it (so room streams attribute through the
                # resolver) and future room-scoped tools read it to know
                # which conversation they are in
                stream_id=briefing.stream_id,
                activation_id=briefing.activation_id,
                actor=self.name,
                # where this reply will land. scope-sensitive tools (web_login
                # mints bearer credentials!) must know a group is listening.
                # user_id is the identity-split thread: tools act for the USER
                metadata={"scope": briefing.scope, USER_ID_KEY: user_uuid},
            ),
            platform=briefing.platform,
            turn_kind=turn_kind,
            # the director's per-line routing (§4.8): chordial's director
            # hints nothing today, so this resolves to the default route -
            # but the parked dynamic-model-routing is now a director-only
            # change away
            hints=briefing.execution,
        )
        return AgentOutcome(
            text=result.text,
            actions=tuple(result.actions),
            refused=result.refused,
        )
