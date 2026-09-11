"""the stuck turn: one press, one pip turn, one card (STUCK_MODE_DESIGN.md
section 2.1 + 5). the turn is the main path and waiting for it is welcome;
at the timeout the fallback ladder becomes the card and STAYS - a late
model write is refused by the episode's conditional write, never applied.

the ROW is the durable claim: an episode in `thinking` owes a card, and
whoever notices it (the route that opened it, the poll that finds it, the
sweep after a restart) makes sure exactly one runner is on it (sol, #89).
lives beside the web service rather than in the pulse loop: the person
pressed the button, so this is their reach-out, not the house's - exempt
from the cadence ladder and the day cap (section 8).
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
        # (episode_uuid, generation) -> the one live runner for it
        self._running: dict[tuple[str, int], asyncio.Task] = {}

    # --- ensuring a runner ----------------------------------------------------

    def ensure(self, user_uuid: str, episode: dict) -> bool:
        """spawn a runner for this episode's generation if it is still
        thinking and nobody is on it. idempotent: the open, the poll, the
        regenerate and the sweep all call this and at most one runs."""
        if episode.get("status") != stuck.THINKING:
            return False
        key = (episode["episode_id"], int(episode["generation"]))
        live = self._running.get(key)
        if live is not None and not live.done():
            return False
        task = asyncio.create_task(self.run(user_uuid, episode))
        self._running[key] = task
        task.add_done_callback(lambda t, k=key: self._forget(k, t))
        return True

    def _forget(self, key, task) -> None:
        if self._running.get(key) is task:
            self._running.pop(key, None)

    async def resume_all(self) -> int:
        """every persisted thinking generation gets a runner - after a
        restart, or when the page that opened one never polled again."""
        pending = await asyncio.to_thread(self.store.list_thinking)
        return sum(1 for user_uuid, ep in pending if self.ensure(user_uuid, ep))

    # --- the turn ---------------------------------------------------------------

    async def run(self, user_uuid: str, episode: dict) -> None:
        episode_uuid = episode["episode_id"]
        generation = int(episode["generation"])
        rejected = list(episode.get("rejected_kinds") or [])
        task_id = episode.get("task_id")
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
                                generation, rejected, task_id)

    def _ensure_card(self, user_uuid: str, episode_uuid: str, generation: int,
                     rejected: Iterable[str], task_id: Optional[int]) -> None:
        """if the turn didn't settle this generation, the ladder does. a
        settle that returns None means someone else already did (the
        model landed just in time, or the person closed the page). the
        ladder never ships a thin card as if it were whole: fewer than
        three fresh kinds marks the generation exhausted."""
        current = self.store.get(user_uuid, episode_uuid)
        if current is None or current["status"] != stuck.THINKING \
                or current["generation"] != generation:
            return
        try:
            ev = stuck.gather(user_uuid, focus_task_id=task_id)
            proposals, exhausted = stuck.fallback(ev, exclude=rejected)
            if not proposals:
                self.store.fail(episode_uuid, generation,
                                "the ladder has no kinds left")
                return
            self.store.settle(episode_uuid, generation, proposals, "fallback",
                              exhausted=exhausted)
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
                    "stuck_task_id": episode.get("task_id"),
                    "stuck_rejected_kinds": rejected},
        )
