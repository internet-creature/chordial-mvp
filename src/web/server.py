"""the web focus view server: today's tasks + the pomodoro bar, on localhost.

deployment shape (decided 2026-07-28): an aiohttp app inside the main
process, supervised by _run_services exactly like the platform interfaces
and the pulse - aiohttp is already a dependency (discord.py rides on it)
and runs natively on the same asyncio loop, so there's no second process,
no build step, and no new server to deploy. the frontend is plain static
files served from src/web/static/ - edit, refresh, done.

not a chat platform: the service intentionally does NOT register with the
MessageRouter (nothing here sends messages). it composes the two workspace
stores - WorkspaceStore for task truth, FocusStore for the clock - the same
way the tool layer would, so the web buttons and the chat tools can never
disagree about what "complete" means.

api surface (all JSON):
    GET  /api/today                  the whole picture: buckets + focus + config
    POST /api/focus/start            {"task_id": int} start/resume/switch
    POST /api/focus/pause            bank the running clock
    POST /api/tasks/{id}/status      {"status": "done"|"deprioritized"|...}
    POST /api/login/redeem           {"code": str} -> session cookie
    POST /api/logout                 clear the session

two deployment modes, ONE switch (Config.WEB_PUBLIC_URL - see config.py):
localhost keeps the original trust-the-interface behavior with the
single-user resolver; public mode requires a chordial-issued session on
every page and api call, and each request acts as the session's OWN user -
the multi-user seam the localhost resolver deliberately punted on.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid as uuid_mod
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from weakref import WeakValueDictionary

from aiohttp import WSMsgType, web

from config import Config
from src.database.database import get_db
from src.database.models import User
from src.personas import CHAIR_ID
from src.services.workspace import get_store, vocab
from src.services.workspace.agenda import user_today
from src.services.workspace.focus import FocusStore
from src.services import device_sync, focus_flow
from src.services import stuck as stuck_mod
from src.services.stuck_turns import StuckTurns
from src.services.cycles import CycleStore
from src.utils.timezone_utils import utc_now
from src.web import auth, device_auth, receipts

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# how often an open websocket re-proves its device token. a revoked device's
# socket dies within this window (module-level so tests can shrink it)
WS_REVALIDATE_SECONDS = 60.0


class WebUserError(RuntimeError):
    """the page can't tell whose workspace to show - config must say."""


def resolve_web_user() -> str:
    """whose workspace the page shows. WEB_USER_UUID wins when set; else the
    sole active real (non-test) user. anything ambiguous is an error the api
    surfaces per-request - a misconfigured page must never guess a person."""
    if Config.WEB_USER_UUID:
        with get_db() as db:
            row = db.query(User.uuid).filter(
                User.uuid == Config.WEB_USER_UUID).first()
        if row is None:
            raise WebUserError(
                f"WEB_USER_UUID {Config.WEB_USER_UUID!r} matches no user")
        return Config.WEB_USER_UUID
    with get_db() as db:
        uuids = [u[0] for u in db.query(User.uuid).filter(
            User.is_active.is_(True), User.is_test.is_(False)).all()]
    if len(uuids) == 1:
        return uuids[0]
    if not uuids:
        raise WebUserError("no active users yet - say hi to chordial first")
    raise WebUserError(
        f"{len(uuids)} active users - set WEB_USER_UUID to pick one")


def _task_row(t: dict) -> dict:
    """the fields the page renders. numeric id rides along because the focus
    api keys on it; public_id is for humans and future chat deep-links."""
    return {
        "id": t["id"], "public_id": t["public_id"], "title": t["title"],
        "status": t["status"], "priority": t["priority"],
        "scheduled": t["scheduled"], "window": t["window"],
        "pom_estimate": t["pom_estimate"], "plan_title": t["plan_title"],
        "helper": t["helper"], "description": t["description"],
        # the focus dogfood round: the scope line and the parked-today mark
        "next_action": t.get("next_action"),
        "set_aside_on": t.get("set_aside_on"),
    }


class WebService:
    """the supervised service. start() binds and then parks until stop() -
    _run_services treats a returning service task as a death, so the park is
    load-bearing, same as the platform interfaces."""

    platform = "web"

    def __init__(self, store=None, focus: Optional[FocusStore] = None,
                 user_resolver=resolve_web_user, chat_service=None,
                 app_interface=None, cycle_rooms=None):
        self.store = store or get_store()
        self.focus = focus or FocusStore()
        self.cycles = CycleStore()
        # the cycle-room doors (phase 6b). main injects the scorer-armed
        # service; the default still opens doors, just never scores on
        # demand (a retro before the sweep presents cardless, honestly).
        if cycle_rooms is None:
            from src.services.cycle_rooms import CycleRooms
            cycle_rooms = CycleRooms()
        self.cycle_rooms = cycle_rooms
        self._resolve_user = user_resolver
        # the app-facing chat seam: process_message runs a turn, the app
        # interface's queues carry the delivered lines back. both None on
        # deployments that run the focus view without the chat engine.
        self.chat_service = chat_service
        self.app_interface = app_interface
        # one app-originated turn per user at a time: the reply-capture queue
        # is user-wide, so a second concurrent POST would otherwise drain the
        # first one's lines. weak values, same pattern as ChatService's locks.
        self._send_locks: WeakValueDictionary[str, asyncio.Lock] = \
            WeakValueDictionary()
        # stuck mode (docs/STUCK_MODE_DESIGN.md section 5): the episode
        # store and the turn runner. the orchestrator is read lazily from
        # the chat seam - None on chat-less deployments, where the fallback
        # ladder is the card from the first second
        self.stuck = stuck_mod.StuckStore()
        self.stuck_turns = StuckTurns(
            store=self.stuck,
            orchestrator=lambda: getattr(self.chat_service, "orchestrator", None))
        self._limiter = auth.RateLimiter(
            attempts=Config.LINK_RATE_ATTEMPTS,
            window_seconds=Config.LINK_RATE_WINDOW_SECONDS)
        self._runner: Optional[web.AppRunner] = None
        self._stop_event: Optional[asyncio.Event] = None

    # --- app assembly --------------------------------------------------------

    def build_app(self) -> web.Application:
        if Config.web_auth_enabled() and not Config.WEB_SESSION_SECRET:
            # fail at startup, not at the first login attempt
            raise RuntimeError(
                "WEB_PUBLIC_URL is set but WEB_SESSION_SECRET is not - "
                "generate one: openssl rand -hex 32")
        app = web.Application(
            middlewares=[self._security_headers, self._origin_guard,
                         self._cors_v1])
        app.router.add_get("/", self._index)
        app.router.add_get("/login", self._login_page)
        app.router.add_post("/api/login/redeem", self._api_login_redeem)
        app.router.add_post("/api/logout", self._api_logout)
        app.router.add_get("/api/today", self._api_today)
        app.router.add_post("/api/focus/start", self._api_focus_start)
        app.router.add_post("/api/focus/pause", self._api_focus_pause)
        app.router.add_post("/api/tasks/{task_id}/status", self._api_task_status)
        # --- /api/v1: the app-facing api (docs/ROOMS_DESIGN.md section 10).
        # device-bearer auth on every route; tenant scoping is structural -
        # handlers only ever see the authenticated device's user.
        app.router.add_post("/api/v1/devices/link", self._api_device_link)
        app.router.add_get("/api/v1/devices", self._api_devices_list)
        app.router.add_post("/api/v1/devices/revoke", self._api_device_revoke)
        app.router.add_post("/api/v1/sync/events", self._api_sync_events)
        app.router.add_get("/api/v1/sync/cursor", self._api_sync_cursor)
        app.router.add_get("/api/v1/sync/decisions", self._api_sync_decisions)
        app.router.add_get("/api/v1/today", self._api_v1_today)
        app.router.add_get("/api/v1/cycle", self._api_v1_cycle)
        app.router.add_get("/api/v1/scorecards", self._api_v1_scorecards)
        app.router.add_get("/api/v1/arc", self._api_v1_arc)
        app.router.add_get("/api/v1/council", self._api_council)
        # tasks are canonical HERE - the deer window lists/adds/finishes
        # them through these; the sidecar only ever owns the clock
        app.router.add_post("/api/v1/tasks", self._api_v1_task_create)
        app.router.add_post("/api/v1/tasks/{task_id}/status",
                            self._api_v1_task_status)
        # the shaping seam (docs/FOCUS_DOGFOOD_DESIGN.md section 4): scope,
        # reschedule, status, set aside - any subset in one body
        app.router.add_patch("/api/v1/tasks/{task_id}", self._api_v1_task_patch)
        # stuck mode (docs/STUCK_MODE_DESIGN.md section 5.1): one press
        # opens an episode, the page polls it, reactions move it
        app.router.add_post("/api/v1/stuck", self._api_v1_stuck_open)
        app.router.add_get("/api/v1/stuck/{episode_id}", self._api_v1_stuck_get)
        app.router.add_post("/api/v1/stuck/{episode_id}/react",
                            self._api_v1_stuck_react)
        # rooms v0: the legacy per-user stream presented as today's room.
        # phase 2 makes rooms first-class; these routes keep their shape.
        app.router.add_get("/api/v1/rooms/current", self._api_room_current)
        app.router.add_get("/api/v1/rooms/current/messages",
                           self._api_room_messages)
        app.router.add_post("/api/v1/rooms/current/messages",
                            self._api_room_send)
        # the cycle-room doors (phase 6b). literal paths, registered before
        # the {room_uuid} routes so "cycle" never resolves as a room uuid.
        app.router.add_get("/api/v1/rooms/cycle", self._api_cycle_doors)
        app.router.add_post("/api/v1/rooms/cycle/retro",
                            self._api_cycle_retro_open)
        app.router.add_post("/api/v1/rooms/cycle/planning",
                            self._api_cycle_planning_open)
        # the archive routes register AFTER the /current ones so "current"
        # never resolves as a {room_uuid}
        app.router.add_get("/api/v1/rooms", self._api_rooms_archive)
        app.router.add_get("/api/v1/rooms/{room_uuid}/messages",
                           self._api_room_archive_messages)
        # the room-bound send (phase 6b): a turn into an OPEN owned room (a
        # cycle room). closed rooms stay read-only through the GET above.
        app.router.add_post("/api/v1/rooms/{room_uuid}/messages",
                            self._api_room_send_by_uuid)
        app.router.add_get("/api/v1/ws", self._api_ws)
        app.router.add_static("/static", STATIC_DIR)
        # the desktop app's update feed (phase 7b): latest.json + signed
        # bundles, public read-only static files - the updater polls before
        # any device auth exists, and the artifacts are signed, so secrecy
        # buys nothing. a configured-but-missing dir is a warning, not a
        # crash: updates being down must never take the council with it.
        if Config.APP_UPDATES_DIR:
            if Path(Config.APP_UPDATES_DIR).is_dir():
                app.router.add_static("/app/updates", Config.APP_UPDATES_DIR,
                                      show_index=False)
            else:
                logger.warning(
                    "APP_UPDATES_DIR %s does not exist - the update feed "
                    "route was not mounted", Config.APP_UPDATES_DIR)
        # the operator's meter (phase 7c): the usage dashboard. gated by
        # OPS_TOKEN - unset means the routes don't exist at all, same
        # pattern as the update feed. the page itself is inert html; the
        # data endpoint is the thing the bearer check guards.
        if Config.OPS_TOKEN:
            app.router.add_get("/ops", self._ops_page)
            app.router.add_get("/api/ops/usage", self._api_ops_usage)
        app.on_startup.append(self._start_flow_sweep)
        app.on_cleanup.append(self._stop_flow_sweep)
        app.on_startup.append(self._start_stuck_sweep)
        app.on_cleanup.append(self._stop_stuck_sweep)
        return app

    # --- the focus-flow sweep --------------------------------------------------
    # the durable half of apply-then-process: the per-sync drain handles the
    # common case instantly, and this sweep retries anything a failed pass
    # left behind - a device that trims its outbox after the ACK and then
    # goes quiet must never strand its consequences.

    _FLOW_SWEEP_SECONDS = 60

    async def _start_flow_sweep(self, _app) -> None:
        self._flow_sweep = asyncio.create_task(self._flow_sweep_loop())

    async def _stop_flow_sweep(self, _app) -> None:
        task = getattr(self, "_flow_sweep", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _flow_sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._FLOW_SWEEP_SECONDS)
            try:
                await asyncio.to_thread(focus_flow.drain)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("focus-flow sweep failed; retrying next tick")

    # --- the stuck sweep -------------------------------------------------------
    # an episode nobody touched for the TTL is closed here, so correctness
    # never depends on the page's unload request arriving (STUCK_MODE_DESIGN
    # section 5.1).

    async def _start_stuck_sweep(self, _app) -> None:
        self._stuck_sweep = asyncio.create_task(self._stuck_sweep_loop())

    async def _stop_stuck_sweep(self, _app) -> None:
        task = getattr(self, "_stuck_sweep", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _stuck_sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(Config.STUCK_SWEEP_SECONDS)
            try:
                await asyncio.to_thread(self.stuck.sweep,
                                        Config.STUCK_EPISODE_TTL_MINUTES)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stuck sweep failed; retrying next tick")

    async def start(self):
        self._stop_event = asyncio.Event()
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, Config.WEB_HOST, Config.WEB_PORT)
        await site.start()
        logger.info("web focus view on http://%s:%s",
                    Config.WEB_HOST, Config.WEB_PORT)
        await self._stop_event.wait()

    async def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- response headers -----------------------------------------------------

    @web.middleware
    async def _security_headers(self, request: web.Request, handler):
        """every response names who may frame it. an unset header lets any
        site iframe the logged-in view and clickjack its buttons; the config
        default allows only ourselves, and deployments widen it deliberately
        (the portfolio desktop embeds focus.exe in a window).

        Cache-Control: no-cache on everything: without it, browsers
        heuristically reuse the html for ~10% of its Last-Modified age (a
        page cached before a deploy masked that deploy for days), and
        cloudflare edge-caches static js/css for hours. no-cache still
        permits storing - Last-Modified revalidation keeps 304s fast - but
        every load asks the origin, so a deploy is visible on next reload."""
        csp = "frame-ancestors " + Config.WEB_FRAME_ANCESTORS
        try:
            response = await handler(request)
        except web.HTTPException as e:
            e.headers["Content-Security-Policy"] = csp
            e.headers.setdefault("Cache-Control", "no-cache")
            raise
        response.headers["Content-Security-Policy"] = csp
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # --- cross-origin writes --------------------------------------------------

    @web.middleware
    async def _origin_guard(self, request: web.Request, handler):
        """public mode refuses state-changing requests from any other origin.
        SameSite=Lax alone doesn't cover a sibling subdomain on the same
        personal domain (the portfolio site could POST here with cookies, and
        aiohttp parses json out of text/plain "simple" requests). browsers
        always send Origin on cross-origin POSTs, so a mismatch is decisive;
        absent means non-browser (curl), which csrf doesn't apply to."""
        if request.path.startswith("/api/v1"):
            # bearer-token routes carry no ambient credentials (no cookies),
            # so csrf doesn't apply; _cors_v1 owns their origin policy
            return await handler(request)
        if Config.web_auth_enabled() and request.method not in ("GET", "HEAD"):
            origin = request.headers.get("Origin")
            if origin is not None and origin != Config.WEB_PUBLIC_URL:
                return _error("cross-origin request refused", status=403)
        return await handler(request)

    # --- /api/v1 CORS ---------------------------------------------------------

    @web.middleware
    async def _cors_v1(self, request: web.Request, handler):
        """lets the tauri webview (and the vite dev server) call the app api
        from its own origin. scoped to /api/v1 only; the allowlist is exact
        origins from config, never a wildcard. non-browser clients (no
        Origin header) pass straight through - the api's real gate is the
        bearer token, this just satisfies the browser's preflight."""
        if not request.path.startswith("/api/v1"):
            return await handler(request)
        origin = request.headers.get("Origin")
        allowed = origin in Config.APP_ALLOWED_ORIGINS if origin else False

        def _stamp(headers) -> None:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
            headers["Access-Control-Allow-Headers"] = \
                "Authorization, Content-Type"
            headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
            headers["Access-Control-Max-Age"] = "600"

        if request.method == "OPTIONS":
            # the preflight; no route handlers register OPTIONS
            if not allowed:
                return _error("origin not allowed", status=403)
            response = web.Response(status=204)
            _stamp(response.headers)
            return response
        try:
            response = await handler(request)
        except web.HTTPException as e:
            if allowed:
                _stamp(e.headers)
            raise
        if allowed:
            _stamp(response.headers)
        return response

    # --- who is asking --------------------------------------------------------

    def _session_user(self, request: web.Request) -> Optional[str]:
        return auth.verify_session(request.cookies.get(auth.SESSION_COOKIE))

    async def _request_user(self, request: web.Request) -> str:
        """the user this request acts as. public mode: the session's user,
        or 401. localhost mode: the configured resolver, exactly as before.
        raises aiohttp HTTP errors; handlers just await it first."""
        if Config.web_auth_enabled():
            user_uuid = self._session_user(request)
            if user_uuid is None:
                raise web.HTTPUnauthorized(
                    text='{"error": "not logged in - ask chordial for a '
                         'login code"}',
                    content_type="application/json")
            return user_uuid
        try:
            return await asyncio.to_thread(self._resolve_user)
        except WebUserError as e:
            raise web.HTTPConflict(
                text='{"error": "%s"}' % str(e).replace('"', "'"),
                content_type="application/json")

    # --- login ----------------------------------------------------------------

    async def _login_page(self, request: web.Request) -> web.StreamResponse:
        # a valid session skips the page; ?code= stays in the url for the
        # page script to auto-redeem (the GET itself must never redeem -
        # link-preview prefetchers would burn the single-use code)
        if not Config.web_auth_enabled() or self._session_user(request):
            raise web.HTTPFound("/")
        return web.FileResponse(STATIC_DIR / "login.html")

    async def _api_login_redeem(self, request: web.Request) -> web.Response:
        if not Config.web_auth_enabled():
            return _error("login is not enabled on this deployment", status=404)
        if not self._limiter.allow(_client_ip(request)):
            return _error("too many attempts - wait a few minutes", status=429)
        body = await _json_body(request)
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str):
            return _error("code (string) required")
        user_uuid = await asyncio.to_thread(auth.redeem_login_code, code)
        if user_uuid is None:
            return _error("that code is invalid or expired - ask chordial "
                          "for a fresh one", status=401)
        response = web.json_response({"ok": True})
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.mint_session(user_uuid),
            max_age=Config.WEB_SESSION_DAYS * 86400,
            httponly=True,
            samesite="Lax",
            secure=Config.WEB_PUBLIC_URL.startswith("https"),
            path="/",
        )
        return response

    async def _api_logout(self, request: web.Request) -> web.Response:
        response = web.json_response({"ok": True})
        response.del_cookie(auth.SESSION_COOKIE, path="/")
        return response

    # --- handlers -------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.StreamResponse:
        if Config.web_auth_enabled() and self._session_user(request) is None:
            raise web.HTTPFound("/login")
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _api_today(self, request: web.Request) -> web.Response:
        user_uuid = await self._request_user(request)
        return await asyncio.to_thread(self._today_payload, user_uuid)

    def _today_payload(self, user_uuid: str) -> web.Response:
        today = user_today(user_uuid)
        today_iso = today.isoformat()
        with get_db() as db:
            user = db.query(User).filter(User.uuid == user_uuid).first()
            name = user.preferred_name if user else None
            user_tz = (user.timezone if user and user.timezone else "UTC")

        # same bucketing as the agenda payload (today / overdue / started-but-
        # undated), but rows keep their numeric ids for the focus api
        tasks = self.store.list_tasks(user_uuid)   # open only, scheduled order
        # the evidence nudge (section 10.2): which rows keep waiting. read
        # from the day's snapshot; a failed read flags nothing
        from src.services import focus_day
        flagged = focus_day.breakdown_flags(user_uuid)
        buckets = {"overdue": [], "today": [], "in_progress": [], "done": [],
                   "set_aside": []}
        for t in tasks:
            sched = t["scheduled"]
            row = _task_row(t)
            row["needs_breakdown"] = t["id"] in flagged
            if t.get("set_aside_on") == today_iso:
                # parked for today (section 2): still open, still listed,
                # but out of the day's live buckets so nothing nudges on it.
                # tomorrow the stamp no longer matches and it simply returns.
                buckets["set_aside"].append(row)
            elif sched == today_iso:
                buckets["today"].append(row)
            elif sched and sched < today_iso:
                buckets["overdue"].append(row)
            elif t["status"] == "in_progress":
                buckets["in_progress"].append(row)

        # finished-today rides along so the deer window can show the day's
        # wins darkened-with-a-checkmark. closed_at is naive utc; "today" is
        # the user's local day, so convert before comparing.
        from src.database.models import Task
        from src.utils.timezone_utils import to_user_timezone
        with get_db() as db:
            recent_done = db.query(Task).filter(
                Task.user_uuid == user_uuid,
                Task.status == "done",
                Task.closed_at.isnot(None),
                Task.closed_at >= utc_now() - timedelta(days=2),
            ).order_by(Task.closed_at).all()
            for row in recent_done:
                closed_utc = row.closed_at.replace(tzinfo=None)
                if to_user_timezone(closed_utc, user_tz).date() == today:
                    buckets["done"].append({
                        "id": row.id, "title": row.title,
                        "status": row.status,
                        "pom_estimate": row.pom_estimate,
                        "plan_title": None, "priority": row.priority,
                        "scheduled": (row.scheduled.isoformat()
                                      if row.scheduled else None),
                        "closed_at": row.closed_at.isoformat(),
                    })

        snap = self.focus.snapshot(user_uuid)
        return web.json_response({
            "today": today_iso,
            "user": {"name": name},
            "pom_minutes": Config.POM_MINUTES,
            "break_minutes": Config.BREAK_MINUTES,
            "buckets": buckets,
            "focus": {
                "active_task_id": snap["active_task_id"],
                # json object keys are strings; the frontend knows
                "seconds": {str(k): v for k, v in snap["seconds"].items()},
            },
            "server_time": utc_now().isoformat(),
        })

    async def _api_focus_start(self, request: web.Request) -> web.Response:
        user_uuid = await self._request_user(request)
        body = await _json_body(request)
        task_id = body.get("task_id") if isinstance(body, dict) else None
        if not isinstance(task_id, int):
            return _error("task_id (int) required")
        return await asyncio.to_thread(self._focus_start, user_uuid, task_id)

    def _focus_start(self, user_uuid: str, task_id: int) -> web.Response:
        try:
            self.focus.start(user_uuid, task_id)
            # starting the clock means the work started: same transition the
            # chat tools make. idempotent when it's already in_progress.
            task = self.store.update_task(user_uuid, task_id,
                                          status="in_progress")
        except ValueError as e:
            return _error(str(e), status=404 if "not found" in str(e) else 400)
        return web.json_response({"ok": True, "task": _task_row(task)})

    async def _api_focus_pause(self, request: web.Request) -> web.Response:
        user_uuid = await self._request_user(request)
        return await asyncio.to_thread(self._focus_pause, user_uuid)

    def _focus_pause(self, user_uuid: str) -> web.Response:
        self.focus.pause(user_uuid)
        return web.json_response({"ok": True})

    async def _api_task_status(self, request: web.Request) -> web.Response:
        user_uuid = await self._request_user(request)
        body = await _json_body(request)
        status = body.get("status") if isinstance(body, dict) else None
        if not isinstance(status, str):
            return _error("status (string) required")
        try:
            task_id = int(request.match_info["task_id"])
        except ValueError:
            return _error("task id must be an integer")
        return await asyncio.to_thread(self._task_status, user_uuid, task_id, status)

    def _task_status(self, user_uuid: str, task_id: int, status: str) -> web.Response:
        try:
            canonical = vocab.canonical_status("task", status)
            if vocab.is_closed_status("task", canonical):
                # closing banks the clock first - a done task never accrues
                self.focus.stop(user_uuid, task_id)
            task = self.store.update_task(user_uuid, task_id, status=canonical)
        except ValueError as e:
            return _error(str(e), status=404 if "not found" in str(e) else 400)
        return web.json_response({"ok": True, "task": _task_row(task)})


    # --- /api/v1: devices & sync ---------------------------------------------

    async def _device(self, request: web.Request) -> device_auth.DeviceIdentity:
        """the device this request acts as, or 401. handlers await it first -
        it IS the tenant boundary (user_uuid only ever comes from here)."""
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else None
        identity = await asyncio.to_thread(
            device_auth.verify_device_token, token)
        if identity is None:
            raise web.HTTPUnauthorized(
                text='{"error": "invalid or revoked device token"}',
                content_type="application/json")
        return identity

    async def _api_device_link(self, request: web.Request) -> web.Response:
        if not self._limiter.allow(_client_ip(request)):
            return _error("too many attempts - wait a few minutes", status=429)
        body = await _json_body(request)
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str):
            return _error("code (string) required")
        name = body.get("name") if isinstance(body, dict) else None
        result = await asyncio.to_thread(
            device_auth.link_device, code,
            name if isinstance(name, str) and name else "device")
        if result is None:
            return _error("that code is invalid or expired - ask chordial "
                          "for a fresh one", status=401)
        device_uuid, token = result
        return web.json_response(
            {"device_id": device_uuid, "token": token}, status=201)

    async def _api_devices_list(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        devices = await asyncio.to_thread(
            device_auth.list_devices, identity.user_uuid)
        for d in devices:
            d["current"] = d["device_id"] == identity.device_uuid
        return web.json_response({"devices": devices})

    async def _api_device_revoke(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        body = await _json_body(request)
        target = body.get("device_id") if isinstance(body, dict) else None
        if not isinstance(target, str):
            return _error("device_id (string) required")
        revoked = await asyncio.to_thread(
            device_auth.revoke_device, identity.user_uuid, target)
        if not revoked:
            return _error("no such active device", status=404)
        return web.json_response({"ok": True})

    async def _api_sync_events(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        body = await _json_body(request)
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list):
            return _error("events (array) required")
        try:
            result = await asyncio.to_thread(
                device_sync.apply_events, identity.id, events)
        except device_sync.QuotaExceeded as e:
            return _error(str(e), status=429)
        except ValueError as e:
            return _error(str(e))
        # landed events become consequences (pip's observations) - a separate
        # crash-safe pass keyed on processed_at, so a failure here never
        # costs the device its ACK. drain (not one pass): the batch may be
        # bigger than a pass's limit. anything that still fails is retried
        # by the background sweep - the device trimming its outbox after
        # this ACK must never strand consequences.
        try:
            await asyncio.to_thread(focus_flow.drain, identity.user_uuid)
        except Exception:
            logger.exception("focus_flow drain failed; the sweep will retry")
        return web.json_response(result.as_dict())

    async def _api_sync_cursor(self, request: web.Request) -> web.Response:
        """the device's durable ACK cursor. a fresh sidecar state (wiped db,
        reinstall) restarts its outbox at seq 1 while this cursor may be far
        ahead - every event below it would be swallowed as a duplicate. the
        outbox pump reads this before its first push and renumbers above it."""
        identity = await self._device(request)

        def read() -> int:
            from src.database.models import Device
            with get_db() as db:
                return db.query(Device.acked_seq).filter(
                    Device.id == identity.id).scalar() or 0
        return web.json_response(
            {"acked_seq": await asyncio.to_thread(read)})

    async def _api_sync_decisions(self, request: web.Request) -> web.Response:
        """the tether's return channel (REWIND_DESIGN section 8): decisions
        answered on a phone, waiting for the sidecar to apply or refuse
        them. read-only and idempotent - the sidecar is authoritative, and
        the offer's terminal event is what clears a row from this list."""
        identity = await self._device(request)
        from src.services import rewind_tether
        decisions = await asyncio.to_thread(
            rewind_tether.pending_decisions, identity.user_uuid)
        return web.json_response({"decisions": decisions})

    async def _api_v1_today(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        return await asyncio.to_thread(self._today_payload, identity.user_uuid)

    async def _api_v1_cycle(self, request: web.Request) -> web.Response:
        """the cycle view read model (ROOMS_DESIGN section 6): the active
        cycle's projection - baseline + scope changes + progress from
        applied device events. {"cycle": null} when no cycle is active."""
        identity = await self._device(request)
        view = await asyncio.to_thread(
            self.cycles.projection, identity.user_uuid)
        return web.json_response(view if view is not None
                                 else {"cycle": None})

    async def _api_v1_scorecards(self, request: web.Request) -> web.Response:
        """edwin's filed assessments, newest first (ROOMS_DESIGN section 8):
        deterministic component scores + evidence-checked findings. read
        model only - the cycle scorer is the sole writer."""
        identity = await self._device(request)
        from src.services.cycle_scorer import recent_assessments
        try:
            limit = int(request.query.get("limit", "10"))
        except ValueError:
            limit = 10
        rows = await asyncio.to_thread(
            recent_assessments, identity.user_uuid,
            subject_type=request.query.get("subject_type"), limit=limit)
        return web.json_response({"assessments": rows})

    async def _api_v1_arc(self, request: web.Request) -> web.Response:
        """the taper's read model (ROOMS_DESIGN section 7): the arc
        posture, the steady streak, and the check-in beat it has earned.
        pure arithmetic over the filed scorecards - same numbers the
        pulse uses."""
        identity = await self._device(request)
        from config import Config
        from src.services import taper
        state = await asyncio.to_thread(
            taper.state_for, identity.user_uuid, Config.DM_INTERVAL_MINUTES)
        return web.json_response({"arc": state})

    # --- the operator's meter (phase 7c) ----------------------------------

    @staticmethod
    def _ops_authorized(request: web.Request) -> bool:
        """constant-time bearer check against OPS_TOKEN. the token is an
        operator credential, not a user one - no session, no cookie, the
        dashboard page holds it in sessionStorage and sends it per fetch."""
        import hmac as hmac_mod
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        presented = header[len("Bearer "):].strip()
        return bool(Config.OPS_TOKEN) and hmac_mod.compare_digest(
            presented.encode(), Config.OPS_TOKEN.encode())

    async def _ops_page(self, request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "ops.html")

    async def _api_ops_usage(self, request: web.Request) -> web.Response:
        if not self._ops_authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from src.services import metering
        try:
            days = int(request.query.get("days", "14"))
        except ValueError:
            days = 14
        days = max(1, min(days, 90))
        report = await asyncio.to_thread(metering.usage_report, days)
        return web.json_response(report)

    async def _api_v1_task_create(self, request: web.Request) -> web.Response:
        """a quick-add from the deer window: title only, scheduled today.
        richer shaping (plans, priorities, estimates) stays with the
        council's tools - this is the 'jot it down and start' path."""
        identity = await self._device(request)
        body = await _json_body(request)
        title = body.get("title") if isinstance(body, dict) else None
        if not isinstance(title, str) or not title.strip():
            return _error("title (non-empty string) required")
        if len(title.strip()) > 300:
            return _error("title too long (max 300 characters)")

        def create() -> dict:
            return self.store.create_task(
                identity.user_uuid, title.strip(),
                scheduled=user_today(identity.user_uuid).isoformat())
        task = await asyncio.to_thread(create)
        return web.json_response({"ok": True, "task": _task_row(task)},
                                 status=201)

    async def _api_v1_task_status(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        body = await _json_body(request)
        status = body.get("status") if isinstance(body, dict) else None
        if not isinstance(status, str):
            return _error("status (string) required")
        try:
            task_id = int(request.match_info["task_id"])
        except ValueError:
            return _error("task id must be an integer")

        def update() -> web.Response:
            try:
                canonical = vocab.canonical_status("task", status)
                task = self.store.update_task(identity.user_uuid, task_id,
                                              status=canonical)
            except ValueError as e:
                return _error(str(e),
                              status=404 if "not found" in str(e) else 400)
            return web.json_response({"ok": True, "task": _task_row(task)})
        return await asyncio.to_thread(update)

    async def _api_v1_task_patch(self, request: web.Request) -> web.Response:
        """PATCH /api/v1/tasks/{id} - the companion window's shaping seam
        (docs/FOCUS_DOGFOOD_DESIGN.md section 4). body = any subset of
        next_action / scheduled / status / set_aside; unknown keys are a
        400 so a typo can't silently no-op. `set_aside: true` stamps the
        user's local today (a today decision, never a lifecycle change);
        `false` clears it. the status route stays for the offline finish
        ledger; this route never touches the sidecar clock - the window
        owns that ordering (pause first, then patch)."""
        identity = await self._device(request)
        body = await _json_body(request)
        if not isinstance(body, dict) or not body:
            return _error("a json object with at least one field is required")
        unknown = set(body) - _TASK_PATCH_KEYS
        if unknown:
            return _error("unknown field(s): " + ", ".join(sorted(unknown)))
        try:
            task_id = int(request.match_info["task_id"])
        except ValueError:
            return _error("task id must be an integer")

        changes: dict = {}
        if "next_action" in body:
            value = body["next_action"]
            if value is not None and not isinstance(value, str):
                return _error("next_action must be a string or null")
            changes["next_action"] = value
        if "scheduled" in body:
            value = body["scheduled"]
            if value is not None:
                if not isinstance(value, str):
                    return _error("scheduled must be an ISO date or null")
                try:
                    date.fromisoformat(value)
                except ValueError:
                    return _error("scheduled must be an ISO date (YYYY-MM-DD)")
            changes["scheduled"] = value
        if "status" in body:
            if not isinstance(body["status"], str):
                return _error("status must be a string")
            try:
                changes["status"] = vocab.canonical_status("task", body["status"])
            except ValueError as e:
                return _error(str(e))
        if "set_aside" in body:
            if not isinstance(body["set_aside"], bool):
                return _error("set_aside must be true or false")
            changes["set_aside_on"] = (
                user_today(identity.user_uuid) if body["set_aside"] else None)

        def update() -> web.Response:
            try:
                task = self.store.update_task(identity.user_uuid, task_id,
                                              **changes)
            except ValueError as e:
                return _error(str(e),
                              status=404 if "not found" in str(e) else 400)
            return web.json_response({"ok": True, "task": _task_row(task)})
        return await asyncio.to_thread(update)

    # --- stuck mode (docs/STUCK_MODE_DESIGN.md section 5.1) -------------------

    async def _api_v1_stuck_open(self, request: web.Request) -> web.Response:
        """one press. opens an episode (idempotent per device + request_id)
        and spawns the turn; the page polls GET until a card exists. body:
        {surface, request_id, task_id?}. generating proposals changes
        nothing outside the episode."""
        identity = await self._device(request)
        body = await _json_body(request)
        if not isinstance(body, dict):
            return _error("body must be a json object")
        unknown = set(body) - _STUCK_OPEN_KEYS
        if unknown:
            return _error("unknown field(s): " + ", ".join(sorted(unknown)))
        request_id = _uuid_field(body.get("request_id"))
        if request_id is None:
            return _error("request_id must be a uuid")
        surface = body.get("surface") or "companion"
        if surface not in stuck_mod.SURFACES:
            return _error("surface must be one of " + ", ".join(stuck_mod.SURFACES))
        task_id = body.get("task_id")
        if task_id is not None and not isinstance(task_id, int):
            return _error("task_id must be an integer")

        def open_episode():
            try:
                return self.stuck.open(
                    identity.user_uuid, request_uuid=request_id,
                    surface=surface, device_id=identity.id, task_id=task_id)
            except ValueError as e:
                return _error(str(e), status=404)
        result = await asyncio.to_thread(open_episode)
        if isinstance(result, web.Response):
            return result
        episode, created = result
        if created:
            self.stuck_turns.spawn(identity.user_uuid, episode)
        return web.json_response({"ok": True, "episode": episode,
                                  "replayed": not created},
                                 status=201 if created else 200)

    async def _api_v1_stuck_get(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        episode_id = _uuid_field(request.match_info.get("episode_id"))
        if episode_id is None:
            return _error("episode id must be a uuid")
        episode = await asyncio.to_thread(
            self.stuck.get, identity.user_uuid, episode_id)
        if episode is None:
            return _error("episode not found", status=404)
        return web.json_response({"ok": True, "episode": episode})

    async def _api_v1_stuck_react(self, request: web.Request) -> web.Response:
        """body: {reaction, generation, request_id, proposal_id?}. accepting
        claims the proposal and writes its one prepared next_action in the
        same transaction; the response carries the typed execution the
        sidecar deduplicates on. a third 'different' owes a new generation
        and spawns its turn here. too_much and closed change nothing."""
        identity = await self._device(request)
        body = await _json_body(request)
        if not isinstance(body, dict):
            return _error("body must be a json object")
        unknown = set(body) - _STUCK_REACT_KEYS
        if unknown:
            return _error("unknown field(s): " + ", ".join(sorted(unknown)))
        episode_id = _uuid_field(request.match_info.get("episode_id"))
        if episode_id is None:
            return _error("episode id must be a uuid")
        request_id = _uuid_field(body.get("request_id"))
        if request_id is None:
            return _error("request_id must be a uuid")
        reaction = body.get("reaction")
        if reaction not in stuck_mod.REACTIONS:
            return _error("reaction must be one of " + ", ".join(stuck_mod.REACTIONS))
        generation = body.get("generation")
        if not isinstance(generation, int):
            return _error("generation must be an integer")
        proposal_id = body.get("proposal_id")
        if proposal_id is not None and not isinstance(proposal_id, str):
            return _error("proposal_id must be a string")
        if reaction in ("accepted", "different") and not proposal_id:
            return _error(f"{reaction} needs a proposal_id")

        def react():
            try:
                return self.stuck.react(
                    identity.user_uuid, episode_id, request_uuid=request_id,
                    generation=generation, reaction=reaction,
                    proposal_id=proposal_id)
            except ValueError as e:
                message = str(e)
                status = 404 if "not found" in message else 409
                return _error(message, status=status)
        result = await asyncio.to_thread(react)
        if isinstance(result, web.Response):
            return result
        episode, outcome = result
        if outcome == "regenerate":
            self.stuck_turns.spawn(identity.user_uuid, episode)
        payload = {"ok": True, "episode": episode, "outcome": outcome}
        if episode.get("execution"):
            payload["execution"] = episode["execution"]
        return web.json_response(payload)

    async def _api_council(self, request: web.Request) -> web.Response:
        """the presence strip's data source: who lives in this deployment,
        as THIS user knows them. roster = enabled ∩ authored cards (the same
        filter as list_available_guides - a partial deployment must not
        advertise residents who can never speak); identity = the card's
        authored default unless the user reshaped it (helper_states
        overrides). the chair never reads as unmet - vel is the front door
        and greets before any introduction has been recorded."""
        identity = await self._device(request)
        from src.managers.helper_state_manager import (
            STATUS_ACTIVE, STATUS_NOT_MET, HelperStateManager)
        from src.personas import load_personas

        cards = load_personas()
        states = HelperStateManager()
        members = []
        for helper_id in sorted(set(Config.ENABLED_HELPERS) & set(cards)):
            card = cards[helper_id]
            state = await states.get(identity.user_uuid, helper_id)
            status = state.status
            if card.chair and status == STATUS_NOT_MET:
                status = STATUS_ACTIVE
            members.append({
                "id": helper_id,
                "chair": card.chair,
                "emoji": card.emoji,
                "lane": card.lane,
                "specialty": card.specialty,
                "name": state.persona_name or helper_id,
                "species": state.persona_form or card.species,
                "status": status,
            })
        # the chair leads the strip, the rest keep id order
        members.sort(key=lambda m: (not m["chair"], m["id"]))
        return web.json_response({"council": members})

    # --- /api/v1: rooms (v0 - the legacy stream as today's room) --------------

    async def _api_room_current(self, request: web.Request) -> web.Response:
        identity = await self._device(request)

        def build() -> dict:
            from src.services.rooms import get_room_store
            from src.services.workspace.agenda import user_today
            room = get_room_store().current_room(
                identity.user_uuid, user_today(identity.user_uuid))
            return {
                "room": {
                    "id": room["room_uuid"],
                    "type": room["room_type"],
                    "date": room["date"],
                    "status": room["status"],
                },
                "chat_available": self.chat_service is not None,
            }
        return web.json_response(await asyncio.to_thread(build))

    async def _api_room_messages(self, request: web.Request) -> web.Response:
        identity = await self._device(request)
        try:
            limit = max(1, min(int(request.query.get("limit", "50")), 200))
        except ValueError:
            return _error("limit must be an integer")

        def read() -> list[dict]:
            from src.database.models import ConversationEvent
            from src.services.rooms import get_room_store
            from src.services.workspace.agenda import user_today
            room = get_room_store().current_room(
                identity.user_uuid, user_today(identity.user_uuid))
            with get_db() as db:
                rows = db.query(ConversationEvent).filter(
                    ConversationEvent.stream_id == room["room_uuid"],
                    ConversationEvent.kind == "message",
                ).order_by(ConversationEvent.id.desc()).limit(limit).all()
                # serialize INSIDE the session - orm rows detach at close
                return [{
                    "id": r.id,
                    "author": r.author,
                    "author_type": r.author_type,
                    "content": r.content,
                    "at": r.created_at.isoformat() if r.created_at else None,
                    "platform": r.platform,
                } for r in reversed(rows)]
        return web.json_response({"messages": await asyncio.to_thread(read)})

    async def _api_rooms_archive(self, request: web.Request) -> web.Response:
        """the journal-like timeline (section 4): daily rooms with their
        summaries where committed. archives are forever - and forever must
        be reachable, so limit/offset page all the way back."""
        identity = await self._device(request)
        try:
            limit = max(1, min(int(request.query.get("limit", "30")), 100))
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            return _error("limit and offset must be integers")

        def read() -> list[dict]:
            from src.services.rooms import get_room_store
            rooms = get_room_store().archive(identity.user_uuid,
                                             limit=limit, offset=offset)
            return [{k: v for k, v in r.items() if k != "user_uuid"}
                    for r in rooms]
        return web.json_response({"rooms": await asyncio.to_thread(read)})

    async def _api_room_archive_messages(self, request: web.Request
                                         ) -> web.Response:
        """a past room's transcript, read-only. an archived day reopens for
        remembering, never for writing - there is no POST twin."""
        identity = await self._device(request)
        room_uuid = request.match_info["room_uuid"]
        try:
            limit = max(1, min(int(request.query.get("limit", "200")), 500))
        except ValueError:
            return _error("limit must be an integer")

        def read() -> Optional[list[dict]]:
            from src.database.models import ConversationEvent
            from src.services.rooms import get_room_store
            # tenant check first: someone else's room and a room that never
            # existed answer identically
            if get_room_store().user_of_room(room_uuid) != identity.user_uuid:
                return None
            with get_db() as db:
                rows = db.query(ConversationEvent).filter(
                    ConversationEvent.stream_id == room_uuid,
                    ConversationEvent.kind == "message",
                ).order_by(ConversationEvent.id.desc()).limit(limit).all()
                return [{
                    "id": r.id,
                    "author": r.author,
                    "author_type": r.author_type,
                    "content": r.content,
                    "at": r.created_at.isoformat() if r.created_at else None,
                    "platform": r.platform,
                } for r in reversed(rows)]
        messages = await asyncio.to_thread(read)
        if messages is None:
            return _error("no such room", status=404)
        return web.json_response({"messages": messages})

    # --- /api/v1: the cycle-room doors (phase 6b) -----------------------------

    async def _api_cycle_doors(self, request: web.Request) -> web.Response:
        """the door state: the latest sealed cycle, whether its card is
        filed, and whichever of its rooms already exist. doors: null means
        nothing to look back on yet."""
        identity = await self._device(request)
        doors = await asyncio.to_thread(
            self.cycle_rooms.doors, identity.user_uuid)
        return web.json_response({
            "doors": doors,
            "chat_available": self.chat_service is not None,
        })

    async def _api_cycle_retro_open(self, request: web.Request
                                    ) -> web.Response:
        """open (get-or-create) the retro room for the latest sealed cycle.
        scoring runs on demand when the card isn't filed - the person
        opening the retro called the evidence question."""
        identity = await self._device(request)
        result = await self.cycle_rooms.open_retro(identity.user_uuid)
        if result is None:
            return _error("no sealed cycle to look back on yet", status=404)
        return web.json_response(result)

    async def _api_cycle_planning_open(self, request: web.Request
                                       ) -> web.Response:
        """open (get-or-create) the planning room following the latest
        sealed cycle."""
        identity = await self._device(request)
        result = await self.cycle_rooms.open_planning(identity.user_uuid)
        if result is None:
            return _error("no sealed cycle to plan from yet", status=404)
        return web.json_response(result)

    async def _api_room_send(self, request: web.Request) -> web.Response:
        """a turn into today's daily room (the v0 shape, unchanged)."""
        identity = await self._device(request)
        return await self._run_room_turn(identity, request, room_uuid=None)

    async def _api_room_send_by_uuid(self, request: web.Request
                                     ) -> web.Response:
        """a turn into a specific OPEN room the sender owns (phase 6b: the
        cycle rooms). a foreign or unknown room answers 404 exactly like the
        read-only GET; a closed room answers 409 - remembering is the GET's
        job, writing is over."""
        identity = await self._device(request)
        room_uuid = request.match_info["room_uuid"]

        def check() -> Optional[str]:
            from src.services.rooms import get_room_store
            room = get_room_store().get_by_uuid(room_uuid)
            if room is None or room["user_uuid"] != identity.user_uuid:
                return "missing"
            # OPEN, not merely not-closed: 'closing' means the digest is
            # already compressing, and a turn slipping in now would miss
            # the summary the next room hydrates
            if room["status"] != "open":
                return "closed"
            return None
        problem = await asyncio.to_thread(check)
        if problem == "missing":
            return _error("no such room", status=404)
        if problem == "closed":
            return _error("this room has settled - it reopens for "
                          "remembering, not for writing", status=409)
        return await self._run_room_turn(identity, request,
                                         room_uuid=room_uuid)

    async def _run_room_turn(self, identity, request: web.Request,
                             room_uuid: Optional[str]) -> web.Response:
        if self.chat_service is None or self.app_interface is None:
            return _error("chat is not available on this deployment",
                          status=503)
        body = await _json_body(request)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return _error("text (non-empty string) required")
        if len(text) > 4000:
            return _error("text too long (max 4000 characters)")
        client_message_id = (body.get("client_message_id")
                             if isinstance(body, dict) else None)
        if not isinstance(client_message_id, str):
            return _error("client_message_id (uuid string) required - it is "
                          "the retry-safety key for this send")
        try:
            uuid_mod.UUID(client_message_id)
        except ValueError:
            return _error("client_message_id must be a uuid")
        client_message_id = client_message_id.lower()
        user_uuid = identity.user_uuid

        # idempotency: claim before the model runs. a retry after a lost
        # response replays the stored lines - it never costs a second turn
        # (or duplicate tool mutations). same contract shape as sync events.
        outcome = await asyncio.to_thread(
            receipts.claim, identity.id, user_uuid, client_message_id)
        if outcome.status == "replay":
            return web.json_response(
                {"ok": True, "messages": outcome.response, "replayed": True})
        if outcome.status == "in_flight":
            return _error("this message is already being processed - "
                          "retry in a moment", status=409)

        # bind the 'app' platform identity to THIS user before the turn runs.
        # the app's platform_user_id IS the user_uuid (the device token
        # already proved who they are), so get_or_create_user can never
        # mint a stranger - but the identity row must exist for delivery
        # targeting and active_platform() to mean anything.
        link = await self.chat_service.user_manager.link_platform_identity(
            user_uuid, "app", user_uuid)
        if link == "conflict":     # structurally impossible; fail loudly
            await asyncio.to_thread(
                receipts.release, identity.id, client_message_id)
            return _error("app identity conflict", status=500)

        from src.models.unified_message import UnifiedMessage
        lock = self._send_locks.get(user_uuid)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[user_uuid] = lock
        try:
            async with lock:
                queue = self.app_interface.subscribe(user_uuid)
                try:
                    fallback = await self.chat_service.process_message(
                        UnifiedMessage(
                            content=text.strip(),
                            platform_user_id=user_uuid,
                            platform="app",
                            platform_message_id=client_message_id,
                            metadata={"device": identity.device_uuid},
                            # the app room is the COUNCIL's room (phase 3b):
                            # a shared scope, so the routing spine picks the
                            # speaker instead of hard-wiring the chair. the
                            # 'group' id is the user - the app fans delivery
                            # to their own connected surfaces.
                            chat_scope="group",
                            group_chat_id=user_uuid,
                            # phase 6b: bind to a specific open room (a
                            # cycle room); None = today's daily room
                            room_uuid=room_uuid,
                        ))
                    replies = self.app_interface.drain(queue)
                finally:
                    self.app_interface.unsubscribe(user_uuid, queue)
        except Exception:
            # no response exists to replay; free the claim for honest retries
            await asyncio.to_thread(
                receipts.release, identity.id, client_message_id)
            raise

        if fallback and not replies:
            # refusal/error copy: shown, never persisted (matches platforms).
            # deliberately NOT stored in the receipt either - a retry of an
            # errored turn should get a fresh chance, not a replayed apology.
            await asyncio.to_thread(
                receipts.release, identity.id, client_message_id)
            replies = [{"type": "message", "author": CHAIR_ID,
                        "content": fallback, "ephemeral": True}]
        else:
            await asyncio.to_thread(
                receipts.store, identity.id, client_message_id, replies)
        return web.json_response({"ok": True, "messages": replies})

    # --- /api/v1: the websocket ----------------------------------------------

    async def _api_ws(self, request: web.Request) -> web.WebSocketResponse:
        """the app's live channel. auth is first-message ({"token": ...}
        within 10s) because webviews can't set headers on websocket
        upgrades. after auth the socket receives every line delivered to
        this user through the app interface (one socket, whole council -
        attribution rides in the payload)."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        if self.app_interface is None:
            await ws.close(code=4503, message=b"chat not available")
            return ws
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except Exception:
            await ws.close(code=4401, message=b"auth required")
            return ws
        token = first.get("token") if isinstance(first, dict) else None
        identity = await asyncio.to_thread(
            device_auth.verify_device_token, token)
        if identity is None:
            await ws.close(code=4401, message=b"invalid device token")
            return ws

        user_uuid = identity.user_uuid
        # the socket proves this user runs the app: make sure the 'app'
        # platform identity exists (the same link the POST path maintains),
        # so presence-aware routing can resolve a desk target even for a
        # user who has only ever read here. structurally can't conflict -
        # the app's platform id IS the user uuid. (absent chat service =
        # a chatless rig; the socket still serves reads.)
        if self.chat_service is not None:
            await self.chat_service.user_manager.link_platform_identity(
                user_uuid, "app", user_uuid)
        queue = self.app_interface.subscribe(user_uuid)
        await ws.send_json({"type": "hello", "device": identity.device_uuid})

        async def pump():
            """forward deliveries, revalidating the device on a cadence so a
            revoked device's socket dies within the window instead of
            listening to the user's council forever."""
            next_check = asyncio.get_event_loop().time() + WS_REVALIDATE_SECONDS
            while True:
                remaining = next_check - asyncio.get_event_loop().time()
                if remaining <= 0:
                    still = await asyncio.to_thread(
                        device_auth.verify_device_token, token)
                    if still is None:
                        await ws.close(code=4401, message=b"device revoked")
                        return
                    next_check = (asyncio.get_event_loop().time()
                                  + WS_REVALIDATE_SECONDS)
                    continue
                try:
                    payload = await asyncio.wait_for(queue.get(),
                                                     timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                await ws.send_json(payload)

        pump_task = asyncio.create_task(pump())
        try:
            # reading keeps the connection's close/ping handling alive.
            # inbound chat rides POST; the one client frame that matters is
            # the presence heartbeat (phase 7a) - {"type": "presence",
            # "idle_seconds": N} - feeding the app interface's trichotomy
            # that routes proactive words to the desk or the phone. anything
            # else is ignored, malformed frames included: a bad heartbeat
            # must never cost the socket.
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except (ValueError, TypeError):
                    continue
                if isinstance(frame, dict) and frame.get("type") == "presence":
                    # keyed by this connection's queue: the report lives and
                    # dies with the socket, and one device's idleness can
                    # never overwrite another's activity
                    self.app_interface.note_presence(
                        user_uuid, frame.get("idle_seconds"),
                        connection=queue)
        finally:
            pump_task.cancel()
            self.app_interface.unsubscribe(user_uuid, queue)
        return ws


def _client_ip(request: web.Request) -> str:
    """the real client, for rate-limit keying. behind cloudflared every
    connection is loopback; cloudflare stamps the true origin ip in
    CF-Connecting-IP. direct localhost use has no proxy headers and keys on
    the socket peer, which is exactly right there."""
    return (request.headers.get("CF-Connecting-IP")
            or request.remote or "unknown")


# the PATCH body's whole vocabulary (section 4); anything else is a 400
_TASK_PATCH_KEYS = frozenset({"next_action", "scheduled", "status", "set_aside"})
# the stuck routes' bodies (STUCK_MODE_DESIGN.md 5.1); anything else is a 400
_STUCK_OPEN_KEYS = frozenset({"surface", "request_id", "task_id"})
_STUCK_REACT_KEYS = frozenset({"reaction", "generation", "request_id",
                               "proposal_id"})


def _uuid_field(value) -> Optional[str]:
    """a lowercase uuid string, or None when the value isn't one."""
    if not isinstance(value, str):
        return None
    try:
        uuid_mod.UUID(value)
    except ValueError:
        return None
    return value.lower()


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _json_body(request: web.Request):
    try:
        return await request.json()
    except Exception:
        return None
