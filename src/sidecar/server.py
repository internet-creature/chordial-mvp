"""the sidecar's loopback api: what the deer window (and the shell) talk to.

binds 127.0.0.1 ONLY - this process serves the person's own machine, never
a network. the browser-facing risk is a random website poking localhost
ports, which the exact-origin CORS allowlist refuses (same posture as the
main server's /api/v1); the tauri shell and the vite dev origin are the
only welcome callers.

surface (all JSON):
    POST /v1/handshake      {token, server_url} - the shell hands the
                            sidecar its device credential once; a NEW
                            device uuid rebases the outbox (fresh server
                            cursor) and un-parks the sync pump
    GET  /v1/state          {focus, line, linked, sync_error, shell_pid}
    POST /v1/focus/start    {task_id?, label?, target_minutes?} - start or
                            SWITCH (a running clock on another task pauses
                            and banks first)
    POST /v1/focus/pause    stop the clock, bank the run
    POST /v1/focus/finish   the task is DONE - the celebration moment; the
                            window marks the task done on the chordial
                            server itself (tasks are canonical there)
    WS   /v1/ws             pushes {type: "state"|"line", ...} as they happen

the 1-second ticker announces a run crossing its block target ONCE - a
gentle ding that ends nothing; the clock keeps counting overtime until the
person lands it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Set

from aiohttp import web

from src.sidecar.drift import ActivityState, DriftWatch
from src.sidecar.focus import AlreadyStarted, FocusEngine, FocusError
from src.sidecar.gates import SpeechGates
from src.sidecar.lines import pick_line
from src.sidecar.offers import OfferError, RewindOffers
from src.sidecar.rewind import ActivityLedger
from src.sidecar.store import SidecarStore
from src.sidecar.sync import OutboxPump

logger = logging.getLogger(__name__)

SIDECAR_HOST = "127.0.0.1"
SIDECAR_PORT = int(os.getenv("SIDECAR_PORT", "8485"))
# the webview's origin varies by platform: tauri://localhost on macOS and
# linux, http://tauri.localhost on windows, the vite server in dev.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "SIDECAR_ALLOWED_ORIGINS",
        "tauri://localhost,http://tauri.localhost,http://localhost:1420"
    ).split(",") if o.strip()
]


class SidecarService:
    def __init__(self, store: SidecarStore,
                 engine: Optional[FocusEngine] = None,
                 pump: Optional[OutboxPump] = None,
                 activity: Optional[ActivityState] = None,
                 drift: Optional[DriftWatch] = None,
                 gates: Optional[SpeechGates] = None,
                 shell_pid: Optional[int] = None):
        self.store = store
        # the shell this sidecar follows (src/sidecar/parent.py), surfaced
        # in /v1/state so a NEW shell probing the port can tell a sidecar
        # that is mid-departure (its shell gone) from one worth adopting
        self.shell_pid = shell_pid
        self.engine = engine or FocusEngine(store)
        self.pump = pump or OutboxPump(store)
        self.activity = activity or ActivityState()
        self.drift = drift or DriftWatch(store, self.activity)
        self.gates = gates or SpeechGates()
        self.ledger = ActivityLedger()
        self.offers = RewindOffers(store, self.ledger,
                                   clock=self.engine.clock)
        # the tether's return channel rides the pump (REWIND_DESIGN
        # section 8): poll only while a question is open, apply through
        # the same machinery as a card tap. the sidecar stays authoritative.
        self.pump.offer_gate = lambda: self.offers.payload() is not None
        self.pump.decision_apply = self._apply_tether_decision
        # restart hydration: an open question on the live run WITHOUT a
        # recorded return means the drift episode is still mid-flight -
        # resume it so the check-in isn't re-asked. an offer that already
        # returned must NOT re-arm the episode: the restart's first
        # active sample would fire a second return and fold legitimate
        # post-return work into the proposed removal
        active = self.store.active_run()
        if active:
            open_offer = self.store.open_offer_for_run(active["id"])
            if open_offer and not open_offer.get("returned_at"):
                self.drift.hydrate_in_drift()
        self._sockets: Set[web.WebSocketResponse] = set()
        self._last_line: Optional[str] = None
        # the posture (blocked, drifting) as of the LAST broadcast - the
        # ticker compares against this, never against the start of its own
        # tick: time-based expiry happens BETWEEN ticks
        self._posture: tuple[bool, bool] = (False, False)
        self._tasks: list[asyncio.Task] = []

    # --- app assembly -----------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._cors])
        app.router.add_post("/v1/handshake", self._handshake)
        app.router.add_get("/v1/state", self._state)
        app.router.add_post("/v1/focus/start", self._focus_start)
        app.router.add_post("/v1/focus/pause", self._focus_pause)
        app.router.add_post("/v1/focus/boundary", self._focus_boundary)
        app.router.add_post("/v1/focus/finish", self._focus_finish)
        app.router.add_post("/v1/focus/rewind", self._rewind)
        app.router.add_post("/v1/focus/rewind/undo", self._rewind_undo)
        app.router.add_post("/v1/activity", self._activity)
        app.router.add_get("/v1/ws", self._ws)
        app.on_startup.append(self._start_background)
        app.on_cleanup.append(self._stop_background)
        return app

    async def _start_background(self, _app) -> None:
        self._tasks = [
            asyncio.create_task(self._ticker()),
            asyncio.create_task(self.pump.run()),
        ]

    async def _stop_background(self, _app) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.pump.close()

    # --- cors ------------------------------------------------------------------

    @web.middleware
    async def _cors(self, request: web.Request, handler):
        origin = request.headers.get("Origin")
        allowed = origin in ALLOWED_ORIGINS if origin else False

        def _stamp(headers) -> None:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
            headers["Access-Control-Allow-Headers"] = "Content-Type"
            headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            headers["Access-Control-Max-Age"] = "600"

        if request.method == "OPTIONS":
            response = web.Response(status=204)
            if allowed:
                _stamp(response.headers)
            return response
        if origin and not allowed:
            return web.json_response({"error": "origin not allowed"},
                                     status=403)
        response = await handler(request)
        if allowed:
            _stamp(response.headers)
        return response

    # --- broadcast ---------------------------------------------------------------

    async def _broadcast(self, payload: dict) -> None:
        dead = []
        for ws in self._sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._sockets.discard(ws)

    def _world(self) -> dict:
        """the state payload: the clock plus derived activity flags (never
        the bundle id - the ui gets moods, not surveillance) plus the open
        correction question, impact-framed fresh on every build. building
        it stamps the broadcast posture the ticker compares against."""
        self._posture = (self.activity.blocked, self.drift.drifting)
        return {
            "type": "state",
            "focus": self.engine.state(),
            "activity": {**self.activity.snapshot(),
                         "drifting": self._posture[1]},
            "offer": self.offers.payload(),
        }

    async def _announce(self, moment: str) -> str:
        line = pick_line(moment, last=self._last_line)
        self._last_line = line
        await self._broadcast({"type": "line", "moment": moment,
                               "text": line})
        await self._broadcast(self._world())
        return line

    async def _announce_unprompted(self, moment: str) -> None:
        """spontaneous speech rides the gate stack: hushed in meetings,
        silent in quiet hours, capped per rolling hour. the state still
        broadcasts either way - the ui may move even when the deer holds
        her tongue."""
        if self.gates.allow(blocked=self.activity.blocked):
            self.gates.spend()
            await self._announce(moment)
        else:
            await self._broadcast(self._world())

    # --- the ticker -----------------------------------------------------------

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ticker hiccup; continuing")

    async def _tick_once(self) -> None:
        """the second-hand: the target ding and the drift watch (both
        unprompted, both gated) - plus posture honesty: blocked and
        drifting can expire by TIME alone (a collector going stale lifts
        the hush, disarms a drift), and those transitions carry no line,
        so the state must broadcast on its own. the comparison is against
        the last BROADCAST posture - the change happened between ticks,
        so the tick's own before/after would never see it."""
        if self.engine.crossed_target():
            # a stuck run's target is the boundary, never the ding
            # (docs/STUCK_MODE_DESIGN.md section 4) - one or the other
            moment = ("stuck_boundary"
                      if self.engine.is_stuck_run(self.store.active_run())
                      else "block_target")
            await self._announce_unprompted(moment)
        active = self.store.active_run()
        moment = self.drift.tick(active is not None)
        if moment == "drift_checkin" and active is not None:
            # the drift mints (or extends) the correction question; the
            # check-in line rides the gates as before
            self.offers.on_drift(active)
            await self._announce_unprompted(moment)
        elif moment == "drift_return" and active is not None \
                and self.offers.on_return(active) is not None:
            # the card IS the welcome: one surface, not a pile of gentle
            # messages - the drift_return line stays in its pocket
            await self._broadcast({"type": "rewind_offer",
                                   "offer": self.offers.payload()})
            await self._broadcast(self._world())
        elif moment:
            await self._announce_unprompted(moment)
        elif (self.activity.blocked, self.drift.drifting) != self._posture:
            await self._broadcast(self._world())

    # --- handlers ------------------------------------------------------------

    async def _handshake(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        token = body.get("token") if isinstance(body, dict) else None
        server_url = body.get("server_url") if isinstance(body, dict) else None
        if not isinstance(token, str) or "." not in token:
            return _error("token (device bearer token) required")
        if not isinstance(server_url, str) or not server_url.startswith("http"):
            return _error("server_url (http origin) required")

        device_uuid = token.partition(".")[0]
        previous = self.store.get("device_uuid")
        if previous and previous != device_uuid:
            moved = self.store.rebase_outbox()
            logger.info("new device credential: outbox rebased (%d pending)",
                        moved)
        self.store.put("device_uuid", device_uuid)
        self.store.put("device_token", token)
        self.store.put("server_url", server_url.rstrip("/"))
        self.store.put("sync_error", "")     # un-park the pump
        return web.json_response({"ok": True})

    async def _state(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "focus": self.engine.state(),
            "activity": {**self.activity.snapshot(),
                         "drifting": self.drift.drifting},
            "offer": self.offers.payload(),
            "line": self._last_line,
            "linked": bool(self.store.get("device_token")),
            "sync_error": self.store.get("sync_error") or None,
            "shell_pid": self.shell_pid,
        })

    async def _activity(self, request: web.Request) -> web.Response:
        """the collector's 2s heartbeat. the rust boundary already
        sanitized; this re-checks shape because loopback posts can come
        from anything on the machine."""
        body = await _json_body(request)
        if not isinstance(body, dict):
            return _error("json object required")
        bundle_id = body.get("bundle_id")
        if bundle_id is not None and not (
                isinstance(bundle_id, str) and 0 < len(bundle_id) <= 200
                and all(c.isalnum() or c in ".-_" for c in bundle_id)):
            bundle_id = None            # malformed = absent, never trusted
        idle = body.get("idle_seconds")
        if not isinstance(idle, (int, float)) or isinstance(idle, bool) \
                or idle < 0:
            return _error("idle_seconds (non-negative number) required")
        was_blocked = self.activity.blocked
        self.activity.observe(bundle_id, float(idle))
        self.ledger.observe(self.engine.clock(), float(idle))
        if self.activity.blocked != was_blocked:
            # hush on/off changes the deer's whole posture, and it happens
            # without a line - push the state so the window follows
            await self._broadcast(self._world())
        return web.json_response({"ok": True})

    def _apply_resolution(self, body) -> Optional[web.Response]:
        """the combined buttons ride the transition: resolve the offer
        first, atomically from the caller's view. the offer must belong
        to the ACTIVE run - a frozen run's question resolves through
        /v1/focus/rewind, which also banks it; resolving it here would
        leave the frozen run terminal-questioned but never closed, an
        invisible permanently-unbanked run. returns an error response, or
        None when there was nothing to do / it worked."""
        resolution = body.get("resolution") if isinstance(body, dict) \
            else None
        if resolution is None:
            return None
        if not isinstance(resolution, dict) \
                or not isinstance(resolution.get("offer_uuid"), str) \
                or resolution.get("action") not in ("remove", "keep"):
            return _error("resolution needs offer_uuid and "
                          "action ('remove' or 'keep')")
        target = self.store.get_offer(resolution["offer_uuid"])
        active = self.store.active_run()
        if target is None:
            return _error("no such offer")
        if active is None or target["run_id"] != active["id"]:
            return _error("that question belongs to a held block - "
                          "answer it on the card instead")
        try:
            offer = self.offers.resolve(
                resolution["offer_uuid"], resolution["action"],
                at=resolution.get("at")
                if isinstance(resolution.get("at"), str) else None)
        except OfferError as e:
            return _error(str(e))
        if resolution["action"] == "remove":
            self.engine.rearm_after_excision(offer["run_id"])
        return None

    async def _focus_start(self, request: web.Request) -> web.Response:
        body = await _json_body(request) or {}
        task_id = body.get("task_id") if isinstance(body, dict) else None
        label = body.get("label") if isinstance(body, dict) else None
        target = body.get("target_minutes", 25) if isinstance(body, dict) \
            else 25
        failed = self._apply_resolution(body)
        if failed is not None:
            return failed
        execution = body.get("execution") if isinstance(body, dict) else None
        switching = self.engine.state().get("running", False)
        try:
            state = self.engine.start(
                task_id if isinstance(task_id, int) else None,
                label if isinstance(label, str) else None,
                target, execution=execution)
        except AlreadyStarted as e:
            # the same accepted proposal, again (a retry, a reload): the
            # live run is the answer, no line, no second block
            return web.json_response({"focus": e.state, "line": "",
                                      "offer": self.offers.payload(),
                                      "replayed": True})
        except FocusError as e:
            return _error(str(e), status=409 if "already ran" in str(e) else 400)
        self.ledger.clear()          # a new run starts a new story
        line = await self._announce(
            "focus_switch" if switching else "focus_start")
        return web.json_response({"focus": state, "line": line,
                                  "offer": self.offers.payload(),
                                  "replayed": False})

    async def _focus_boundary(self, request: web.Request) -> web.Response:
        """the boundary's "keep going", persisted with the run (section
        4; sol's #92 round): body {choice: "keep_going"}. "stop here" is
        a pause with reason "boundary"."""
        body = await _json_body(request) or {}
        choice = body.get("choice") if isinstance(body, dict) else None
        if choice != "keep_going":
            return _error("choice must be keep_going")
        try:
            state = self.engine.keep_going()
        except FocusError as e:
            return _error(str(e), status=409)
        await self._broadcast(self._world())
        return web.json_response({"focus": state})

    async def _focus_pause(self, request: web.Request) -> web.Response:
        body = await _json_body(request) or {}
        failed = self._apply_resolution(body)
        if failed is not None:
            return failed
        reason = body.get("reason", "paused") if isinstance(body, dict) else "paused"
        try:
            run = self.engine.pause(reason=reason)
        except FocusError as e:
            return _error(str(e))
        if run is None:
            held = self.store.frozen_runs()
            return _error(
                "no clock is running" if not held else
                "no clock is running - a held block is waiting on its "
                "question", status=409)
        if run.get("frozen"):
            # the clock STOPPED (that's the point) - the run just isn't
            # banked yet; the question is right there on the card
            line = await self._announce("focus_hold")
            return web.json_response({
                "focus": self.engine.state(),
                "held": run,
                "offer": self.offers.payload(),
                "line": line,
            })
        # "stop here" at a stuck run's boundary earns its own line
        line = await self._announce(
            "stuck_stopped" if reason == "boundary" else "focus_pause")
        return web.json_response({
            "focus": self.engine.state(),
            "run": run,
            "line": line,
        })

    async def _focus_finish(self, request: web.Request) -> web.Response:
        body = await _json_body(request) or {}
        failed = self._apply_resolution(body)
        if failed is not None:
            return failed
        try:
            run = self.engine.finish()
        except FocusError as e:
            return _error(str(e), status=409)
        if run.get("frozen"):
            line = await self._announce("focus_hold")
            return web.json_response({
                "focus": self.engine.state(),
                "held": run,
                "offer": self.offers.payload(),
                "line": line,
            })
        line = await self._announce("task_finished")
        return web.json_response({
            "focus": self.engine.state(),
            "run": run,
            "line": line,
        })

    async def _rewind(self, request: web.Request) -> web.Response:
        """answer the question: remove (a candidate boundary) or keep.
        if the run was frozen waiting on this, the answer also banks it -
        at its freeze instant - and speaks the line its transition owed."""
        body = await _json_body(request)
        if not isinstance(body, dict) \
                or not isinstance(body.get("offer_uuid"), str):
            return _error("offer_uuid required")
        action = body.get("action")
        if action not in ("remove", "keep"):
            return _error("action must be 'remove' or 'keep'")
        at = body.get("at") if isinstance(body.get("at"), str) else None
        try:
            offer = self.offers.resolve(body["offer_uuid"], action, at=at)
        except OfferError as e:
            return _error(str(e))
        if action == "remove":
            self.engine.rearm_after_excision(offer["run_id"])
        run_summary = None
        line = None
        run = self.store.run(offer["run_id"])
        if run and run["ended_at"] is None and run.get("frozen_at"):
            run_summary = self.engine.close_frozen(offer["run_id"])
            line = await self._announce(
                "task_finished" if run_summary["reason"] == "finished"
                else "focus_pause")
        else:
            await self._broadcast(self._world())
        # `offer` is the NEXT authoritative open question (another held
        # block may still be waiting) - the client must set its state
        # from this, not assume null
        return web.json_response({
            "focus": self.engine.state(),
            "resolved": {"offer_uuid": offer["offer_uuid"],
                         "status": offer["status"]},
            "offer": self.offers.payload(),
            "run": run_summary,
            "line": line,
        })

    async def _rewind_undo(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        if not isinstance(body, dict) \
                or not isinstance(body.get("offer_uuid"), str):
            return _error("offer_uuid required")
        try:
            offer = self.offers.undo(body["offer_uuid"])
        except OfferError as e:
            return _error(str(e))
        self.engine.suppress_ding(offer["run_id"])
        await self._broadcast(self._world())
        return web.json_response({
            "focus": self.engine.state(),
            "offer": self.offers.payload(),
        })

    async def _apply_tether_decision(self, decision: dict) -> bool:
        """one phone answer from the decision poll. the sidecar is
        authoritative: anything it can't honor - the question already
        answered at the desk (first answer wins), the run banked, a foreign
        offer_uuid from another device - is skipped without ceremony; the
        offer's terminal event closes the server's loop either way.

        the transition half is presence-aware: 'remove & pause' pauses the
        clock only while the quiet is still running (the away case the
        tether exists for). if the person is back at the desk and working,
        the answer resolves the question and the clock keeps going -
        stopping live work because a phone tap arrived late would be the
        correction becoming the interruption."""
        offer_uuid = decision.get("offer_uuid")
        action = decision.get("action") or decision.get("choice")
        if not isinstance(offer_uuid, str) or action not in ("remove", "keep"):
            return False
        offer = self.store.get_offer(offer_uuid)
        if offer is None or offer["status"] != "open":
            return False               # stale or foreign: the desk won
        run = self.store.run(offer["run_id"])
        if run is None or run["ended_at"] is not None:
            return False               # banked runs are beyond reach
        still_quiet = (offer.get("returned_at") is None
                       and not run.get("frozen_at"))
        try:
            self.offers.resolve(offer_uuid, action, surface="tether")
        except OfferError as e:
            logger.info("tether decision refused: %s", e)
            return False
        if action == "remove":
            self.engine.rearm_after_excision(offer["run_id"])
        if run.get("frozen_at"):
            # a held block's question answered from the phone: banks at its
            # freeze instant, wearing the transition it owed - same as the
            # card path
            summary = self.engine.close_frozen(offer["run_id"])
            await self._announce(
                "task_finished" if summary["reason"] == "finished"
                else "focus_pause")
        elif still_quiet:
            active = self.store.active_run()
            if active is not None and active["id"] == run["id"]:
                self.engine.pause()    # resolved above, so this banks
                await self._announce("focus_pause")
        await self._broadcast({"type": "rewind_offer",
                               "offer": self.offers.payload()})
        await self._broadcast(self._world())
        return True

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            await ws.send_json(self._world())
            async for _ in ws:
                pass          # loopback push channel; client frames ignored
        finally:
            self._sockets.discard(ws)
        return ws


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _json_body(request: web.Request):
    try:
        return await request.json()
    except Exception:
        return None
