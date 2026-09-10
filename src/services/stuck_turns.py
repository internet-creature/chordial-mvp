"""the stuck turn: one press, one pip turn, one card (STUCK_MODE_DESIGN.md
section 2.1 + 5). the turn is the main path and waiting for it is welcome;
at the timeout the fallback ladder becomes the card and STAYS - a late
model write is refused by the episode's state, never applied.

lives beside the web service (the route spawns it) rather than in the
pulse loop: the person pressed the button, so this is their reach-out, not
the house's - exempt from the cadence ladder and the day cap (section 8).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Iterable, Optional

from config import Config
from src.services import stuck
from src.services.stuck import StuckStore

logger = logging.getLogger(__name__)

PIP = "pip"


class StuckTurns:
    """runs the turn for an episode's current generation and guarantees
    the episode leaves `thinking` one way or another."""

    def __init__(self, store: Optional[StuckStore] = None,
                 orchestrator: Optional[Callable[[], object]] = None,
                 timeout: Optional[float] = None):
        self.store = store or StuckStore()
        # a getter, not the object: the web service is built before the
        # chat engine on some deployments, and None means "no house" -
        # the fallback ladder is the card from the first second
        self._orchestrator = orchestrator or (lambda: None)
        self.timeout = (Config.STUCK_TURN_TIMEOUT_SECONDS
                        if timeout is None else timeout)
        self._tasks: set = set()

    def spawn(self, user_uuid: str, episode: dict) -> None:
        """fire-and-forget from a request handler; the task keeps itself
        alive in the set until it finishes."""
        task = asyncio.create_task(self.run(user_uuid, episode))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self, user_uuid: str, episode: dict) -> None:
        episode_uuid = episode["episode_id"]
        generation = episode["generation"]
        rejected = list(episode.get("rejected_kinds") or [])
        orchestrator = self._orchestrator()
        if orchestrator is not None:
            try:
                stimulus = await self._stimulus(user_uuid, episode, rejected)
                await asyncio.wait_for(orchestrator.handle(stimulus),
                                       timeout=self.timeout)
            except asyncio.TimeoutError:
                logger.warning("stuck turn timed out for %s gen %s; falling back",
                               episode_uuid, generation)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stuck turn failed for %s gen %s; falling back",
                                 episode_uuid, generation)
        await asyncio.to_thread(self._ensure_card, user_uuid, episode_uuid,
                                generation, rejected)

    def _ensure_card(self, user_uuid: str, episode_uuid: str,
                     generation: int, rejected: Iterable[str]) -> None:
        """if the turn didn't settle this generation, the ladder does. a
        settle that returns None means someone else already did (the
        model landed just in time, or the person closed the page)."""
        current = self.store.get(user_uuid, episode_uuid)
        if current is None or current["status"] != stuck.THINKING \
                or current["generation"] != generation:
            return
        try:
            ev = stuck.gather(user_uuid)
            proposals = stuck.fallback(ev)
            rejected = set(rejected)
            if rejected:
                # the ladder's kinds may collide with turned-down ones; keep
                # the honest end (rest) and whatever wasn't refused
                proposals = [p for p in proposals if p["kind"] not in rejected] \
                    or proposals[-1:]
            self.store.settle(episode_uuid, generation, proposals, "fallback")
        except Exception as e:
            logger.exception("stuck fallback failed for %s", episode_uuid)
            self.store.fail(episode_uuid, generation, f"fallback failed: {e}")

    @staticmethod
    async def _stimulus(user_uuid: str, episode: dict, rejected: list):
        from dainframe.core.types import Stimulus
        from src.services.rooms import get_room_store
        from src.services.workspace.agenda import user_today

        def resolve() -> str:
            room = get_room_store().current_room(user_uuid, user_today(user_uuid))
            return room["room_uuid"]
        stream_id = await asyncio.to_thread(resolve)
        return Stimulus(
            kind="stuck",
            stream_id=stream_id,
            platform="app",
            record_inbound=False,
            scope="dm",
            audience=PIP,
            addressed=(PIP,),
            reason="the person pressed i'm stuck",
            extras={"user_id": user_uuid,
                    "stuck_episode_id": episode["episode_id"],
                    "stuck_generation": episode["generation"],
                    "stuck_surface": episode.get("surface") or "companion",
                    "stuck_rejected_kinds": rejected},
        )
