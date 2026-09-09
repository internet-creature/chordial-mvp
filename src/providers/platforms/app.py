"""the desktop app as a delivery platform (docs/ROOMS_DESIGN.md section 10).

registered with the MessageRouter as platform 'app', exactly like discord or
telegram - the orchestrator's confirmed-delivery-before-recording invariant
holds unchanged. delivery lands in in-process queues: every websocket
connection and every in-flight POST /rooms/current/messages request
subscribes a queue; send_message fans a payload out to all of a user's
subscribers and confirms if at least one was listening. no subscriber means
undelivered (False) - a transient absence, never an UndeliverableError, so
the router never deactivates the app link just because the app is closed.

speaker attribution rides IN the payload (single websocket, whole council -
the same one-bot decision as the tether), which is why the router forwards
`speaker=` to send_message.

this interface has no background loop: it is router-registered but NOT
supervised (nothing to run), and inbound messages arrive via the http/ws
handlers in src/web/server.py rather than handle_incoming_message.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, Optional, Set, Tuple

from config import Config
from src.personas import CHAIR_ID
from src.providers.platforms.base import BaseInterface
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

# a slow websocket consumer must never block delivery confirmation; a queue
# that somehow backs up this far is a dead consumer wearing a live socket
_QUEUE_CAP = 256

# the desktop heartbeats presence every ~30s over its websocket; a live
# socket whose reports have gone this quiet means the reporter died, and an
# unreporting machine cannot prove anyone is at it - count as away. (a
# protocol cadence detail, deliberately not Config: the idle THRESHOLD is
# the user-meaningful knob, Config.PRESENCE_IDLE_SECONDS.)
_PRESENCE_STALE_SECONDS = 90.0


class AppInterface(BaseInterface):
    """fan-out delivery to the user's connected app surfaces - and, since
    those surfaces are the only ones that can prove someone is at the desk,
    the source of truth for presence-aware proactive routing (section 9):
    the websocket handler feeds note_presence from the app's heartbeats, and
    presence_state answers 'deer bubble or tether?' for the pulse."""

    platform = "app"

    def __init__(self, chat_service=None):
        super().__init__(chat_service)
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        # user_uuid -> {connection -> (monotonic stamp, idle_seconds-or-None)}
        # of each surface's most recent heartbeat. PER CONNECTION on purpose:
        # a person can sit at one machine while another idles, and the idle
        # one's heartbeat must never overwrite the active one's (presence is
        # aggregated any-active-wins, not last-writer-wins). a connection's
        # report is pruned when that connection unsubscribes.
        self._presence: Dict[str, Dict[Any, Tuple[float, Optional[float]]]] = {}

    # --- subscription (websocket connections, in-flight sends) ---------------

    def subscribe(self, user_uuid: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_CAP)
        self._subscribers.setdefault(user_uuid, set()).add(queue)
        return queue

    def unsubscribe(self, user_uuid: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(user_uuid)
        if queues is not None:
            queues.discard(queue)
            reports = self._presence.get(user_uuid)
            if reports is not None:
                reports.pop(queue, None)
            if not queues:
                del self._subscribers[user_uuid]
                self._presence.pop(user_uuid, None)

    # --- presence (docs/ROOMS_DESIGN.md section 9) ----------------------------

    def note_presence(self, user_uuid: str,
                      idle_seconds: Optional[float] = None,
                      connection: Any = None) -> None:
        """record one surface's heartbeat. `connection` identifies the
        reporting surface (the websocket handler passes its own queue, so
        the report lives and dies with the connection); `idle_seconds` is
        the sidecar's OS-level idle clock as that surface last saw it - None
        means the app is open but the sidecar was unreachable (dev rigs),
        where openness is the best signal available and counts as active."""
        idle = None
        if idle_seconds is not None:
            try:
                idle = max(0.0, float(idle_seconds))
            except (TypeError, ValueError):
                idle = None  # a malformed report degrades to 'open = active'
        self._presence.setdefault(user_uuid, {})[connection] = (
            time.monotonic(), idle)

    def presence_state(self, user_uuid: str) -> str:
        """'active' | 'idle' | 'absent' - the routing trichotomy, aggregated
        across every connected surface: any demonstrably-active surface
        makes the person active.

        absent: no connected surface at all (the app is closed).
        idle:   surfaces are connected but none is demonstrably attended -
                every reporting surface is idle past the threshold or has
                gone quiet on a live socket.
        active: at least one surface has a fresh heartbeat that doesn't
                report idleness. a user whose surfaces have NEVER heartbeat
                is also active - deliberately fail-open, so pre-7a clients
                (which never report) keep their lines. (a mix of one
                never-reporting client and idle reporters reads idle: the
                reporters are the evidence we have.)
        """
        if not self._subscribers.get(user_uuid):
            return "absent"
        reports = self._presence.get(user_uuid)
        if not reports:
            return "active"
        now = time.monotonic()
        for stamp, idle in reports.values():
            if now - stamp > _PRESENCE_STALE_SECONDS:
                continue  # this reporter died; it proves nothing
            if idle is None or idle < Config.PRESENCE_IDLE_SECONDS:
                return "active"
        return "idle"

    def idle_seconds(self, user_uuid: str) -> Optional[float]:
        """how long the desk has been idle, by its freshest live reporter:
        the SMALLEST idle among surfaces still heartbeating (the person is
        as present as their most-attended screen). None when nothing is
        connected or no surface reports idleness."""
        if not self._subscribers.get(user_uuid):
            return None
        now = time.monotonic()
        idles = [idle for stamp, idle in
                 (self._presence.get(user_uuid) or {}).values()
                 if idle is not None and now - stamp <= _PRESENCE_STALE_SECONDS]
        return min(idles) if idles else None

    @staticmethod
    def drain(queue: asyncio.Queue) -> list[dict]:
        """everything currently in a queue, without waiting."""
        items = []
        while True:
            try:
                items.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                return items

    # --- BaseInterface --------------------------------------------------------

    async def start(self):
        """nothing to run - the web server owns the sockets."""

    async def stop(self):
        """drop every subscriber so pending waits end promptly."""
        self._subscribers.clear()

    async def send_message(self, platform_user_id: str, content: str,
                           **kwargs) -> bool:
        """deliver to every subscribed surface for this user. the app's
        platform_user_id IS the user_uuid (the device token already proved
        who they are - there is no foreign account id to map)."""
        payload = {
            "type": "message",
            # a stable id per delivered line: a client that hears the same
            # line on two surfaces (its POST response and its websocket)
            # dedupes by id instead of guessing by content
            "id": str(uuid.uuid4()),
            "author": kwargs.get("speaker") or CHAIR_ID,
            "content": content,
            "at": utc_now().isoformat(),
        }
        # which room this line belongs to (phase 6b): a client rendering one
        # room drops another room's lines instead of interleaving them.
        # absent for senders that don't know (pre-6b payload shape).
        if kwargs.get("stream_id"):
            payload["room"] = kwargs["stream_id"]
        # the tether mirror (phase 7a): a line that originated on another
        # platform - the user's own telegram words, or a council line the
        # router just confirmed there - rides with who spoke it (author_type
        # 'user' renders on the person's side of the room) and where it came
        # from (provenance, matching the history rows' `platform` field).
        # absent on ordinary app-first deliveries, keeping their bytes pre-7a.
        if kwargs.get("author_type"):
            payload["author_type"] = kwargs["author_type"]
        if kwargs.get("source_platform"):
            payload["platform"] = kwargs["source_platform"]
        queues = list(self._subscribers.get(platform_user_id, ()))
        delivered = 0
        for queue in queues:
            try:
                queue.put_nowait(payload)
                delivered += 1
            except asyncio.QueueFull:
                logger.warning("app subscriber queue full for user %s - "
                               "skipping dead consumer", platform_user_id)
        return delivered > 0

    async def handle_incoming_message(self, message: Any):
        """inbound rides the http handlers, never this hook."""
        raise NotImplementedError(
            "app inbound goes through the /api/v1 handlers")
