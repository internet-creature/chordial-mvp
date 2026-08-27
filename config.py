import os
from typing import Optional
from dotenv import load_dotenv


load_dotenv()


class Config:
    # discord
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

    # ai provider selection: "anthropic" (default) or "openai"
    AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").lower()

    # anthropic
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    # chat model: the persona-facing model the user talks to
    CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-sonnet-5")
    # utility models: cheap models for background jobs (summaries,
    # classification, etc). UTILITY_MODEL remains a backwards-compatible
    # override for deployments that predate provider-specific settings.
    _LEGACY_UTILITY_MODEL = os.getenv("UTILITY_MODEL")
    ANTHROPIC_UTILITY_MODEL = os.getenv(
        "ANTHROPIC_UTILITY_MODEL", _LEGACY_UTILITY_MODEL or "claude-haiku-4-5"
    )
    # effort for chat turns, on BOTH providers (anthropic output_config.effort
    # / openai reasoning.effort). default high: low produced hollow/empty
    # turns in prod (thinking got starved, replies went generic or blank);
    # the persona work is exactly where reasoning depth shows. dial down via
    # env if cost ever matters more than voice.
    CHAT_EFFORT = os.getenv("CHAT_EFFORT", "high")
    # max output tokens for a chat/scheduled turn. CRITICAL: with adaptive
    # thinking on, this cap covers thinking + reply TOGETHER - at 2048 an
    # instruction-heavy turn (e.g. an introduction) could burn the whole budget
    # on thinking and emit ZERO reply text (stop_reason=max_tokens, empty
    # response -> the user gets the error fallback). it's a ceiling you only pay
    # for when tokens are actually generated, so keep it generous; a normal
    # short reply still costs a few hundred tokens. even at 4096 this bit in
    # practice, so: 8192 default + AgentService retries an empty max_tokens
    # response once with the ceiling doubled.
    CHAT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "8192"))

    # openai (kept as an alternate provider). gpt-5.x are reasoning models:
    # CHAT_EFFORT flows through as reasoning.effort, same high default as
    # anthropic. utility jobs pass effort=None, which the provider omits
    # entirely.
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")
    OPENAI_UTILITY_MODEL = os.getenv(
        "OPENAI_UTILITY_MODEL", _LEGACY_UTILITY_MODEL or "gpt-5.4-nano"
    )
    # Public legacy attribute retained for callers outside this repository.
    UTILITY_MODEL = _LEGACY_UTILITY_MODEL or ANTHROPIC_UTILITY_MODEL
    COMPRESSOR_MODEL = os.getenv("COMPRESSOR_MODEL", "gpt-5.4-nano")

    @classmethod
    def utility_model_for(cls, provider_name: str) -> str:
        """Return a utility model that belongs to the selected provider."""
        if provider_name == "anthropic":
            return cls.ANTHROPIC_UTILITY_MODEL
        if provider_name == "openai":
            return cls.OPENAI_UTILITY_MODEL
        raise ValueError(f"unknown AI provider: {provider_name}")

    # the native in-db workspace (docs/NATIVE_WORKSPACE_DESIGN.md) is the only
    # system of record. cap rows returned by any single workspace list_* call
    # (keeps prompts lean).
    WORKSPACE_MAX_PAGE_SIZE = int(os.getenv("WORKSPACE_MAX_PAGE_SIZE", "25"))

    # notion survives only as src/services/notion/client.py, reserved for the
    # future per-user one-way mirror extension (postgres stays the source of
    # truth; per-user integration tokens will live in the db, with these as
    # constructor fallbacks). nothing in the live service reads them today.
    NOTION_API_KEY = os.getenv("NOTION_API_KEY")
    # rest api version pin. 2022-06-28 is stable and works with database ids.
    NOTION_API_VERSION = os.getenv("NOTION_API_VERSION", "2022-06-28")

    # proactive workspace awareness: the chat path injects a live agenda digest
    # (from postgres - no cache, no ttl) as ambient context on the current turn.
    AGENDA_ENABLED = os.getenv("AGENDA_ENABLED", "true").lower() == "true"

    @classmethod
    def agenda_enabled(cls) -> bool:
        return cls.AGENDA_ENABLED

    # completion reconciler: after the companion replies, a cheap utility-model
    # pass that marks tasks done which the user mentioned finishing in passing.
    # only effective when the agenda is available (it needs the open-task list).
    RECONCILER_ENABLED = os.getenv("RECONCILER_ENABLED", "true").lower() == "true"

    # telegram: the tether (docs/ROOMS_DESIGN.md section 9). ONE bot per
    # deployment carries the whole council - speakers are attributed inline
    # in the message text, so there is exactly one token and one username.
    # (the phase-3 one-bot-per-helper ensemble - TELEGRAM_TOKEN_<HELPER>,
    # TELEGRAM_USERNAME_<HELPER>, the shared crew group - was retired
    # outright in phase 7a, not ported.) the username (no '@') is used for
    # link-code deep links (https://t.me/<username>?start=<code>) and must
    # be the REAL BotFather name, never a persona card's placeholder.
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")
    ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "false").lower() == "true"
    # trust-on-first-message for telegram DMs (the discord contract): an
    # unknown sender gets a user row + the introduction flow instead of the
    # stranger wall. default OFF because telegram bot usernames are publicly
    # discoverable - any stranger who finds the bot would cost api spend.
    # dev instances turn this on to test onboarding without discord; a public
    # deployment should prefer invite codes (MULTI_USER_SPEC phase 2) over
    # leaving this open.
    TELEGRAM_OPEN_ONBOARDING = (
        os.getenv("TELEGRAM_OPEN_ONBOARDING", "false").lower() == "true"
    )
    # how long a platform link code stays redeemable
    LINK_CODE_TTL_MINUTES = int(os.getenv("LINK_CODE_TTL_MINUTES", "15"))
    # code-redemption attempts per client ip (login redeem + device link share
    # one sliding window). the default suits a household; a deployment where
    # many legitimate devices share one NAT ip (a classroom) raises it - the
    # code space stays the real defense either way.
    LINK_RATE_ATTEMPTS = int(os.getenv("LINK_RATE_ATTEMPTS", "10"))
    LINK_RATE_WINDOW_SECONDS = int(os.getenv("LINK_RATE_WINDOW_SECONDS", "300"))

    @classmethod
    def telegram_linking_enabled(cls) -> bool:
        return cls.ENABLE_TELEGRAM and bool(cls.TELEGRAM_BOT_USERNAME)

    # presence-aware proactive routing (docs/ROOMS_DESIGN.md section 9):
    # the desktop app heartbeats the sidecar's idle clock over its websocket;
    # a connected app whose last report shows this much idleness (or whose
    # reports have gone stale) counts as AWAY, and proactive words route to
    # the phone instead of an empty desk. a v1 guess, env-tunable.
    PRESENCE_IDLE_SECONDS = int(os.getenv("PRESENCE_IDLE_SECONDS", "300"))

    # the web focus view (src/web/): a localhost desktop companion page -
    # today's tasks + the pomodoro bar. served by the main process on the
    # loopback interface only; widen WEB_HOST deliberately, never by default.
    ENABLE_WEB = os.getenv("ENABLE_WEB", "true").lower() == "true"
    WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
    WEB_PORT = int(os.getenv("WEB_PORT", "8484"))
    # whose workspace the page shows. unset = the sole active real user;
    # ambiguous databases must pick explicitly (the api says so at request
    # time rather than crashing startup).
    WEB_USER_UUID = os.getenv("WEB_USER_UUID")
    # one pomodoro, in minutes - the bar's "full" mark
    POM_MINUTES = int(os.getenv("POM_MINUTES", "25"))
    # the breather between pomodoros - the frontend runs the countdown and
    # pauses the focus clock, so break time never accrues to the task
    BREAK_MINUTES = int(os.getenv("BREAK_MINUTES", "5"))

    # public deployment (e.g. behind a cloudflared tunnel at
    # https://focus.example.com). setting WEB_PUBLIC_URL is the ONE switch
    # that flips the page from trusted-localhost to authenticated-public:
    # sessions become required, the login flow activates, cookies go Secure
    # (when https), and the web_login tool registers so users can ask
    # chordial to let them in. unset = the localhost behavior, unchanged.
    WEB_PUBLIC_URL = (os.getenv("WEB_PUBLIC_URL") or "").rstrip("/") or None
    # signs session cookies; REQUIRED when WEB_PUBLIC_URL is set (the web
    # service refuses to start without it - `openssl rand -hex 32`)
    WEB_SESSION_SECRET = os.getenv("WEB_SESSION_SECRET")
    WEB_SESSION_DAYS = int(os.getenv("WEB_SESSION_DAYS", "30"))
    # who may embed the focus view in an iframe, as space-separated CSP
    # frame-ancestors sources. default: nobody but ourselves - widen
    # deliberately, e.g. "'self' https://internetcreature.dev" to let the
    # portfolio desktop show focus.exe in a window.
    WEB_FRAME_ANCESTORS = os.getenv("WEB_FRAME_ANCESTORS", "'self'")

    # the desktop app's update feed (phase 7b: the box): a directory of
    # signed bundles + latest.json, served read-only at /app/updates/ so
    # the deployment stays single-origin (the tauri updater polls the same
    # host the app already talks to). unset = the route doesn't exist.
    APP_UPDATES_DIR = os.getenv("APP_UPDATES_DIR")

    @classmethod
    def web_auth_enabled(cls) -> bool:
        return cls.WEB_PUBLIC_URL is not None

    # agent loop
    MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "5"))
    # how many recent messages of history to send as context
    MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "30"))
    # ceiling on concurrent in-flight ai api calls across all users
    MAX_CONCURRENT_AI_CALLS = int(os.getenv("MAX_CONCURRENT_AI_CALLS", "6"))

    # database
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///chordial.db")

    # scheduler
    DM_INTERVAL_MINUTES = int(os.getenv("DM_INTERVAL_MINUTES", "60"))
    QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "21"))
    QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "8"))

    # the council (ROOMS_DESIGN.md §3): every card in src/personas/*.yaml is
    # loaded, but only these ids become live agents. the default is the full
    # seven-resident council; a solo deployment is ENABLED_HELPERS=vel.
    ENABLED_HELPERS = [
        h.strip()
        for h in os.getenv(
            "ENABLED_HELPERS", "vel,pip,skip,remy,mabel,juniper,edwin"
        ).split(",")
        if h.strip()
    ]

    # proactive-outreach cadence (dainframe CadenceGate): unanswered outreach
    # climbs a declared ladder instead of the old doubling-toward-a-cap
    # backoff, which went permanently silent after ~a day of trying. the
    # default reads: once each morning for a few days, then weekly for a few
    # weeks, then every couple of months forever - the sentinel keeps its
    # post. per-user overrides live in schedule_preferences["outreach_cadence"]
    # (set_preference), same spec language. checked BEFORE any generation -
    # a denied tick costs zero tokens.
    OUTREACH_CADENCE = os.getenv("OUTREACH_CADENCE", "1d x3, 1w x3, 60d @ 8-11")

    # compressor (legacy per-message compression; off by default in favor of
    # full-history context, which is both simpler and cache-friendly)
    ENABLE_COMPRESSION = os.getenv("ENABLE_COMPRESSION", "false").lower() == "true"
    MIN_LENGTH_TO_COMPRESS = int(os.getenv("MIN_LENGTH_TO_COMPRESS", "100"))

    # app device sync (docs/ROOMS_DESIGN.md section 10): per-user quota on
    # device-origin events (trailing 24h) and the max outbox batch size.
    # generous by design - a 2s activity poll emits derived events, not raw
    # samples, so real usage sits far below the cap; the cap exists so a
    # buggy or hostile client can't grow the table unboundedly.
    SYNC_DAILY_EVENT_CAP = int(os.getenv("SYNC_DAILY_EVENT_CAP", "5000"))
    SYNC_MAX_BATCH = int(os.getenv("SYNC_MAX_BATCH", "500"))

    # the meter (phase 7c, src/services/metering.py): per-user model-token
    # budgets over a trailing 24h window (same rolling shape as the sync
    # cap - no midnight cliff, gradual recovery). budgeted = input + output
    # tokens; cache traffic is reported but never counted. soft stops
    # proactive outreach; hard also refuses conversational turns with
    # honest ephemeral copy. 0 = off (the default: single-user rigs stay
    # unmetered and byte-identical). OPS_TOKEN unlocks the operator
    # dashboard at /ops - unset, the routes don't exist.
    USER_TOKEN_BUDGET_SOFT_24H = int(
        os.getenv("USER_TOKEN_BUDGET_SOFT_24H", "0"))
    USER_TOKEN_BUDGET_HARD_24H = int(
        os.getenv("USER_TOKEN_BUDGET_HARD_24H", "0"))
    OPS_TOKEN = os.getenv("OPS_TOKEN")

    # the rewind tether (docs/REWIND_DESIGN.md section 8): an unresolved
    # offer earns one opted-in phone ping after AWAY_MINUTES; an offer older
    # than STALE_HOURS never pings (yesterday's quiet is the chip's business,
    # not a notification's). numbers live in the design doc's section 11 -
    # change them there first.
    REWIND_TETHER_AWAY_MINUTES = int(
        os.getenv("REWIND_TETHER_AWAY_MINUTES", "25"))
    REWIND_TETHER_STALE_HOURS = float(
        os.getenv("REWIND_TETHER_STALE_HOURS", "6"))
    REWIND_TETHER_SWEEP_SECONDS = float(
        os.getenv("REWIND_TETHER_SWEEP_SECONDS", "60"))

    # edwin's cycle scorer (docs/ROOMS_DESIGN.md section 8, phase 6): how
    # often the watcher looks for sealed-but-unassessed cycles; the grace
    # period between a cycle's close and its scoring (the evidence
    # watermark - an offline device's outbox gets this long to drain
    # before the card seals); and how far back discovery reaches - cycles
    # older than the backfill window are left unscored (their data
    # predates the projection; the ledger starts honest, not complete).
    CYCLE_SCORER_SWEEP_SECONDS = float(
        os.getenv("CYCLE_SCORER_SWEEP_SECONDS", "600"))
    CYCLE_SCORER_GRACE_HOURS = float(
        os.getenv("CYCLE_SCORER_GRACE_HOURS", "24"))
    CYCLE_SCORER_BACKFILL_DAYS = int(
        os.getenv("CYCLE_SCORER_BACKFILL_DAYS", "14"))

    # the taper (ROOMS_DESIGN.md section 7): success extends the quiet.
    # a cycle is "steady" when its scorecard's consistency AND execution
    # both clear their floors (and sustainability, when computable, isn't
    # poor - unsustainable bursts never earn quiet). each consecutive
    # steady cycle doubles the check-in interval, capped so presence
    # never tapers to zero. thresholds are v1 guesses, flagged tunable.
    TAPER_STEADY_CONSISTENCY = float(
        os.getenv("TAPER_STEADY_CONSISTENCY", "0.6"))
    TAPER_STEADY_EXECUTION = float(
        os.getenv("TAPER_STEADY_EXECUTION", "0.6"))
    TAPER_MIN_SUSTAINABILITY = float(
        os.getenv("TAPER_MIN_SUSTAINABILITY", "0.5"))
    TAPER_MAX_MULTIPLIER = int(os.getenv("TAPER_MAX_MULTIPLIER", "8"))
    TAPER_WINDOW = int(os.getenv("TAPER_WINDOW", "6"))

    # browser origins allowed to call /api/v1 (the tauri webview's origins:
    # production shell + vite dev server). the api is bearer-token'd - no
    # cookies - so CORS here is about letting OUR app in, not csrf defense.
    APP_ALLOWED_ORIGINS = [
        o.strip() for o in os.getenv(
            "APP_ALLOWED_ORIGINS",
            "tauri://localhost,http://tauri.localhost,http://localhost:1420"
        ).split(",")
        if o.strip()
    ]

    # features
    ENABLE_DISCORD = os.getenv("ENABLE_DISCORD", "true").lower() == "true"
