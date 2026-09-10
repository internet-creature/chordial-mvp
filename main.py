import asyncio
import importlib.metadata
import logging
import subprocess
from pathlib import Path

from config import Config
from src.services.chat_service import ChatService
from dainframe.loop.agent_loop import AgentLoop
from src.services.pulse_wiring import build_pulse
from src.services.usage_recorder import UsageRecorder
from src.services.message_router import MessageRouter
from src.services.tools import build_default_registry
from src.managers.user_manager import UserManager
from src.database.database import init_db


def _build_interfaces(chat_service, link_service, user_manager,
                      app_interface=None):
    """construct every enabled platform interface. add a branch here per platform;
    nothing else in main() needs to change - the router and scheduler discover
    platforms from whatever this returns.

    telegram is the tether (ROOMS_DESIGN section 9): ONE bot for the whole
    council, speakers attributed inline in the text. `app_interface` (when
    the web surface is enabled) is handed to it as the mirror, so a phone
    line also echoes live onto the user's connected desktop. the phase-3
    one-bot-per-helper ensemble was retired outright in phase 7a."""
    interfaces = []
    if Config.ENABLE_DISCORD:
        from src.providers.platforms.discord_bot import DiscordInterface

        interfaces.append(DiscordInterface(chat_service))
        logger.info("discord interface enabled")
    if Config.ENABLE_TELEGRAM:
        from src.providers.platforms.telegram_bot import TelegramInterface

        if not Config.TELEGRAM_TOKEN:
            raise RuntimeError(
                "ENABLE_TELEGRAM is true but TELEGRAM_TOKEN is not set"
            )
        if not Config.TELEGRAM_BOT_USERNAME:
            raise RuntimeError(
                "ENABLE_TELEGRAM is true but TELEGRAM_BOT_USERNAME is not "
                "set - this must be the REAL BotFather username (link-code "
                "deep links point at it)"
            )
        interfaces.append(
            TelegramInterface(
                token=Config.TELEGRAM_TOKEN,
                telegram_handle=Config.TELEGRAM_BOT_USERNAME,
                chat_service=chat_service,
                link_service=link_service,
                user_manager=user_manager,
                mirror=app_interface,
            )
        )
        logger.info("telegram tether enabled (one bot, whole council)")
    return interfaces


def _build_resolver(provider_name: str):
    """the §4.8 routing seam: a director hinting a model on a ScriptLine
    resolves through this table. chordial's builders carry its config - api
    keys, and thinking-by-model (anthropic utility-tier models reject
    adaptive thinking) - and every resolved instance shares ONE concurrency
    ceiling, so hinting a new model never mints a new 'global' semaphore.
    the chat provider itself is the table's default route (resolved once at
    startup), so hint-less runs use the exact same instance."""
    from dainframe.providers import ProviderTable
    from dainframe.providers.limits import ConcurrencyLimiter

    def build_anthropic(model, limiter):
        from dainframe.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            model=model,
            api_key=Config.ANTHROPIC_API_KEY,
            thinking=model != Config.ANTHROPIC_UTILITY_MODEL,
            limiter=limiter,
        )

    def build_openai(model, limiter):
        from dainframe.providers.openai import OpenAIProvider

        return OpenAIProvider(
            model=model, api_key=Config.OPENAI_API_KEY, limiter=limiter
        )

    return ProviderTable(
        default_provider=provider_name,
        default_models={
            "anthropic": Config.CHAT_MODEL,
            "openai": Config.OPENAI_MODEL,
        },
        limiter=ConcurrencyLimiter(Config.MAX_CONCURRENT_AI_CALLS),
        builders={"anthropic": build_anthropic, "openai": build_openai},
    )


def _build_provider(provider_name: str, model: str = None, thinking: bool = True):
    """construct the configured ai provider, or None if misconfigured. pass
    `model` to override the default (e.g. the cheaper utility model), and
    `thinking=False` for models that don't support adaptive thinking (haiku).

    dainframe providers take all config via constructor - model, key, and the
    concurrency ceiling. each provider gets its own private limiter here,
    byte-for-byte the pre-extraction behavior (one semaphore per provider
    instance); a shared ceiling across providers is one injected limiter away
    when it's ever wanted."""
    from dainframe.providers.limits import ConcurrencyLimiter

    limiter = ConcurrencyLimiter(Config.MAX_CONCURRENT_AI_CALLS)
    if provider_name == "anthropic":
        from dainframe.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            model=model or Config.CHAT_MODEL,
            api_key=Config.ANTHROPIC_API_KEY,
            thinking=thinking,
            limiter=limiter,
        )
    if provider_name == "openai":
        from dainframe.providers.openai import OpenAIProvider

        return OpenAIProvider(
            model=model or Config.OPENAI_MODEL,
            api_key=Config.OPENAI_API_KEY,
            limiter=limiter,
        )
    logger.error(
        f"unknown AI_PROVIDER '{provider_name}' (expected 'anthropic' or 'openai')"
    )
    return None


# setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def _run_services(interfaces, pulse, router, tether=None, scorer=None):
    """Supervise long-running services as one failure domain.

    A platform task returning normally is still unexpected while the process
    is running. Stop and cancel its siblings so a dead interface can never be
    hidden by the pulse's infinite loop.
    """
    named_tasks = {
        asyncio.create_task(
            interface.start()
        ): f"{getattr(interface, 'platform', type(interface).__name__)} interface"
        for interface in interfaces
    }
    if pulse is not None:
        named_tasks[asyncio.create_task(pulse.run())] = "pulse"
        logger.info("the pulse started")
    if tether is not None:
        named_tasks[asyncio.create_task(tether.run())] = "rewind tether"
    if scorer is not None:
        named_tasks[asyncio.create_task(scorer.run())] = "cycle scorer"

    try:
        done, _ = await asyncio.wait(named_tasks, return_when=asyncio.FIRST_COMPLETED)
        task = next(iter(done))
        service_name = named_tasks[task]
        exception = task.exception()
        if exception is not None:
            raise RuntimeError(f"{service_name} stopped unexpectedly") from exception
        raise RuntimeError(f"{service_name} stopped unexpectedly")
    finally:
        if pulse is not None:
            pulse.stop()
        for interface in interfaces:
            try:
                await interface.stop()
            except Exception:
                logger.exception(
                    "failed to stop %s interface",
                    getattr(interface, "platform", type(interface).__name__),
                )
        for task in named_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*named_tasks, return_exceptions=True)


async def _close_provider(provider):
    """Close an SDK's async HTTP client when it exposes a close hook."""
    if provider is None:
        return
    close = getattr(getattr(provider, "client", None), "close", None)
    if close is not None:
        result = close()
        if asyncio.iscoroutine(result):
            await result


def _dainframe_stamp() -> str:
    """which dainframe this process is actually running.

    the dainframe is a path dependency (pyproject: "the extraction's
    development seam"), so the lockfile pins every dependency by hash
    *except* the one that changes most - the shipping combination is
    only knowable if the process says it out loud. version comes from
    the installed metadata; the sha from whatever the sibling clone is
    sitting on (absent when it isn't a git checkout)."""
    import dainframe

    root = Path(dainframe.__file__).resolve().parent.parent
    try:
        version = importlib.metadata.version("dainframe")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        # SubprocessError covers TimeoutExpired: a hung git must degrade
        # the stamp, never abort the boot
        sha = ""
    return f"dainframe {version}{f' @ {sha}' if sha else ''} from {root}"


async def main():
    """main entry point for chordial"""
    logger.info("starting chordial...")
    logger.info(_dainframe_stamp())

    # initialize database
    init_db()

    # initialize core services
    user_manager = UserManager()

    # initialize ai provider + agent loop. the chat provider is the resolver
    # table's default route, so hint-less runs and hinted runs share the same
    # instances and the same concurrency ceiling.
    provider_name = Config.AI_PROVIDER
    resolver = None
    provider = None
    if provider_name in ("anthropic", "openai"):
        from dainframe.core import ExecutionHints

        resolver = _build_resolver(provider_name)
        provider = resolver.resolve(ExecutionHints(), agent="startup").provider
    else:
        logger.error(
            f"unknown AI_PROVIDER '{provider_name}' (expected 'anthropic' or 'openai')"
        )
    utility_provider = None
    registry = build_default_registry()
    agent_service = None
    if provider is not None:
        if await provider.is_available():
            logger.info(
                f"{provider_name} provider initialized (model={provider.model})"
            )
            agent_service = AgentLoop(
                provider=provider,
                registry=registry,
                provider_name=provider_name,
                usage_sink=UsageRecorder(),
                max_iterations=Config.MAX_TOOL_ITERATIONS,
                resolver=resolver,
            )
        else:
            logger.warning(f"{provider_name} provider configured but not available")

    # proactive workspace awareness: a live agenda digest the chat path injects
    # as ambient context. queries postgres directly - nothing cached, nothing
    # to keep fresh.
    agenda_service = None
    if Config.agenda_enabled():
        from src.services.workspace.agenda import WorkspaceAgenda

        agenda_service = WorkspaceAgenda()
        logger.info("workspace agenda enabled (live queries)")

    # the outbound router is constructed early (before the orchestrator, which
    # borrows router.deliver for out-of-band sends like the platform-switch
    # notice); interfaces register onto it further down, and deliver resolves
    # them at call time, so the ordering is safe.
    router = MessageRouter(user_manager)

    # assemble the cast and the engine. the dainframe engine decides who talks
    # and records what happened (through chordial's director/context/hooks -
    # see src/services/orchestration.py); each agent owns how it thinks.
    # today's cast: the companion helpers (chat personas, tool loop on the
    # persona model) and the curator (silent memory hygiene, utility model).
    orchestrator = None
    curator_agent = None
    if agent_service is not None:
        from src.agents import HelperAgent, CuratorAgent
        from src.personas import load_personas, validate_enabled
        from src.services.orchestration import build_orchestrator

        # each enabled helper is a HelperAgent driven by its persona card.
        # validate_enabled crashes at startup on an unknown id or a missing
        # chair (same fail-loudly style as the telegram config checks) - a
        # mistyped helper or an unroutable deployment should be loud, never a
        # silently-absent persona.
        validate_enabled(Config.ENABLED_HELPERS)
        cards = load_personas()
        agents = {}
        for helper_id in Config.ENABLED_HELPERS:
            agents[helper_id] = HelperAgent(cards[helper_id], agent_service, registry)

        # one utility provider (haiku; thinking=False - it doesn't support
        # adaptive thinking) shared by the background utility jobs below
        utility_provider = _build_provider(
            provider_name,
            model=Config.utility_model_for(provider_name),
            thinking=False,
        )

        if utility_provider is not None:
            from src.services.memory_curator import MemoryCuratorService

            curator_agent = CuratorAgent(
                MemoryCuratorService(
                    provider=utility_provider,
                    provider_name=provider_name,
                    usage_recorder=UsageRecorder(),
                )
            )
            agents["curator"] = curator_agent
            logger.info(f"memory curator initialized (model={utility_provider.model})")

        # completion reconciler: marks tasks done that the user mentioned
        # finishing in passing. needs the agenda (for the open-task list) and
        # the utility model.
        reconciler = None
        if (
            agenda_service is not None
            and utility_provider is not None
            and Config.RECONCILER_ENABLED
        ):
            from src.services.completion_reconciler import CompletionReconcilerService

            reconciler = CompletionReconcilerService(
                provider=utility_provider,
                provider_name=provider_name,
                agenda_service=agenda_service,
                tool_registry=registry,
                usage_recorder=UsageRecorder(),
            )
            logger.info("completion reconciler initialized")

        # the speaking decider (phase 3b): the routing spine's gray-zone
        # call - a shared-room message with no mention and several plausible
        # lanes gets ONE tiny utility-model pick. no utility model, no
        # decider: the director's rules alone route everything to the chair.
        decider = None
        if utility_provider is not None:
            from src.services.speaking_policy import SpeakingDecider

            decider = SpeakingDecider(
                provider=utility_provider,
                provider_name=provider_name,
                usage_recorder=UsageRecorder(),
            )
            logger.info("speaking decider initialized (model=%s)",
                        utility_provider.model)

        from src.managers.helper_state_manager import HelperStateManager

        orchestrator = build_orchestrator(
            agents=agents,
            user_manager=user_manager,
            agenda_service=agenda_service,
            reconciler=reconciler,
            # speaker-aware delivery: the engine makes a specific helper speak
            # (a group line via that helper's bot, or the switch notice as
            # the chair). the router resolves (platform, speaker) -> interface.
            router=router,
            helper_state_manager=HelperStateManager(),
            decider=decider,
        )
        logger.info(f"dainframe engine initialized (agents: {', '.join(agents)})")

    # create chat service (falls back to echo if no orchestrator is available)
    chat_service = ChatService(
        orchestrator=orchestrator,
        user_manager=user_manager,
    )


    # the link-code service: chat-first account linking across platforms
    # (minted by the link_platform tool, redeemed by the telegram interface)
    from src.services.platform_link_service import PlatformLinkService

    link_service = PlatformLinkService(user_manager)

    # the app interface first (when the web surface is on): it is both a
    # delivery platform AND the tether's mirror + presence source, so the
    # telegram interface and the pulse need it in hand. router-registered
    # but not supervised (no loop to run; the web server owns the sockets).
    app_interface = None
    if Config.ENABLE_WEB:
        from src.providers.platforms.app import AppInterface

        app_interface = AppInterface(chat_service)
        router.register(app_interface)

    # build interfaces and register them with the outbound router. the router
    # owns platform->interface routing and link-deactivation on hard failures,
    # so the scheduler never has to know which interface backs a platform.
    interfaces = _build_interfaces(chat_service, link_service, user_manager,
                                   app_interface=app_interface)
    for interface in interfaces:
        router.register(interface)

    # the web focus view + the app-facing /api/v1 surface: supervised like
    # any interface but NOT router-registered (it serves pages and the api).
    # edwin's cycle scorer (ROOMS_DESIGN.md section 8, phase 6): a slow
    # sweep that files one assessment per ended cycle. the scores are
    # arithmetic, so the watcher runs even without a utility model - the
    # model only upgrades the prose (summary + evidence-checked findings).
    # built before the web service so the retro door can score on demand.
    from src.services.cycle_scorer import CycleScorer

    scorer = CycleScorer(
        provider=utility_provider,
        provider_name=provider_name,
        usage_recorder=UsageRecorder(),
    )
    logger.info("cycle scorer enabled (%s prose)",
                "model" if utility_provider is not None else "deterministic")

    if Config.ENABLE_WEB:
        from src.services.cycle_rooms import CycleRooms
        from src.web.server import WebService

        interfaces.append(WebService(chat_service=chat_service,
                                     app_interface=app_interface,
                                     cycle_rooms=CycleRooms(scorer=scorer)))
        logger.info(
            "web focus view + app api enabled (http://%s:%s)",
            Config.WEB_HOST, Config.WEB_PORT
        )

    # the ambient loop: the dainframe pulse drives scheduled check-ins and
    # curation through the same engine as every chat message. built after
    # the interfaces register, so delivery targeting is restricted to
    # platforms that actually have a live interface. no engine, no pulse.
    # the rewind tether (docs/REWIND_DESIGN.md section 8): a fixed-string
    # ping through the router when an offer sits unresolved and away - no
    # engine, no tokens. built only when a ringable platform is live.
    tether = None
    if any(p in ("telegram", "discord") for p in router.platforms()):
        from src.services.rewind_tether import RewindTether

        tether = RewindTether(router, user_manager)
        logger.info("rewind tether watcher enabled")

    pulse = None
    if orchestrator is not None:
        pulse = build_pulse(
            orchestrator=orchestrator,
            user_manager=user_manager,
            curator=curator_agent,
            # presence-aware routing (ROOMS_DESIGN section 9, phase 7a):
            # every platform is a candidate, and the app interface's
            # presence trichotomy decides where each proactive word lands -
            # desktop when someone is demonstrably there, the tether when
            # they're away or idle, never both. without a web surface,
            # presence is None and targeting is exactly the pre-7a
            # always-on-platforms behavior.
            platforms=list(router.platforms()),
            presence=(app_interface.presence_state
                      if app_interface is not None else None),
            idle_of=(app_interface.idle_seconds
                     if app_interface is not None else None),
        )

    try:
        await _run_services(interfaces, pulse, router, tether, scorer)
    finally:
        logger.info("shutting down chordial...")
        await _close_provider(utility_provider)
        await _close_provider(provider)


if __name__ == "__main__":
    asyncio.run(main())
