from datetime import datetime
from typing import Optional
import re
import logging

import pytz

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "UTC"


# legacy/deprecated zone names -> canonical IANA. pytz resolves these from its
# bundled backward-links, but the dainframe pulse uses stdlib zoneinfo, which
# only knows them when the host tzdata ships the links - a stored "US/Pacific"
# once silenced every proactive send (quiet hours fails closed on a zone it
# can't resolve). canonical names work everywhere; nothing else may be stored.
_LEGACY_TO_CANONICAL = {
    "US/Pacific": "America/Los_Angeles",
    "US/Mountain": "America/Denver",
    "US/Arizona": "America/Phoenix",
    "US/Central": "America/Chicago",
    "US/Eastern": "America/New_York",
    "US/East-Indiana": "America/Indiana/Indianapolis",
    "US/Michigan": "America/Detroit",
    "US/Hawaii": "Pacific/Honolulu",
    "US/Alaska": "America/Anchorage",
    "US/Aleutian": "America/Adak",
    "US/Samoa": "Pacific/Pago_Pago",
    "GB": "Europe/London",
    "Eire": "Europe/Dublin",
    "Japan": "Asia/Tokyo",
    "NZ": "Pacific/Auckland",
}


def canonicalize_timezone(tz_name: str) -> str:
    """map a legacy zone alias to its canonical IANA name; anything else
    passes through unchanged. applied wherever a timezone enters or leaves
    storage, so downstream consumers can rely on canonical names."""
    return _LEGACY_TO_CANONICAL.get(tz_name, tz_name)


# freeform answers -> IANA timezone, for resolving what a user types during
# onboarding ("california", "pacific time", "PST"). multi-word keys are matched
# as substrings; single-word keys only as whole words (so "et" doesn't match
# "meeting"). US-leaning, plus the common international zones.
_TZ_ALIASES = {
    # --- US ---
    "pacific": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "california": "America/Los_Angeles", "los angeles": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "portland": "America/Los_Angeles", "washington state": "America/Los_Angeles",
    "mountain": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
    "denver": "America/Denver", "colorado": "America/Denver", "utah": "America/Denver",
    "arizona": "America/Phoenix", "phoenix": "America/Phoenix",
    "central": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
    "chicago": "America/Chicago", "texas": "America/Chicago", "austin": "America/Chicago", "dallas": "America/Chicago",
    "eastern": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
    "new york": "America/New_York", "nyc": "America/New_York", "boston": "America/New_York",
    "florida": "America/New_York", "miami": "America/New_York", "atlanta": "America/New_York",
    "hawaii": "Pacific/Honolulu", "hst": "Pacific/Honolulu", "alaska": "America/Anchorage",
    # --- international ---
    "london": "Europe/London", "britain": "Europe/London", "england": "Europe/London",
    "gmt": "Europe/London", "bst": "Europe/London",
    "paris": "Europe/Paris", "france": "Europe/Paris",
    "berlin": "Europe/Berlin", "germany": "Europe/Berlin", "cet": "Europe/Berlin",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome", "italy": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam", "netherlands": "Europe/Amsterdam",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo", "jst": "Asia/Tokyo",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "india": "Asia/Kolkata", "ist": "Asia/Kolkata",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "utc": "UTC",
}

# pytz ships bare-abbreviation zones (EST, MST, HST, GMT, CET) that are fixed
# offset and DST-unaware. when a user types one of these we want the DST-aware
# region from the alias table instead, so we skip them in the exact-name match.
_LEGACY_ABBREV_ZONES = {"est", "mst", "hst", "gmt", "cet"}


def resolve_timezone(text: str) -> Optional[str]:
    """best-effort map a freeform answer to an IANA timezone name, or None if we
    can't tell. tries, in order: an exact IANA name (any case), then the alias
    table. used at onboarding so a user can say 'california' instead of having
    to know 'US/Pacific'; unresolved answers fall back to the chat flow."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    lowered = raw.lower()

    # 1. an exact IANA name, case-insensitively ("US/Pacific", "america/new_york"),
    #    except the legacy bare-abbreviation zones we'd rather map DST-aware.
    #    canonicalized on the way out: a typed "US/Pacific" is welcome, but
    #    only "America/Los_Angeles" may be stored.
    if lowered not in _LEGACY_ABBREV_ZONES:
        for tz in pytz.all_timezones:
            if tz.lower() == lowered:
                return canonicalize_timezone(tz)

    # 2. alias table: multi-word keys as substrings, single-word keys as whole words
    tokens = set(re.findall(r"[a-z0-9+]+", lowered))
    for key, tz in _TZ_ALIASES.items():
        if " " in key:
            if key in lowered:
                return tz
        elif key in tokens:
            return tz

    return None


def utc_now() -> datetime:
    """naive utc timestamp, for storing/comparing against db timestamps"""
    return datetime.utcnow()


def _resolve_timezone(tz_name: str) -> pytz.BaseTzInfo:
    """look up a pytz timezone, falling back to utc for missing/invalid names"""
    if not tz_name:
        return pytz.UTC

    try:
        return pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        logger.warning(f"unknown timezone '{tz_name}', falling back to UTC")
        return pytz.UTC


def to_user_timezone(dt_utc: datetime, tz_name: str) -> datetime:
    """
    convert a naive utc datetime into a naive local datetime for the user's timezone.

    stays naive on the way out so existing formatting code (which expects naive
    datetimes) doesn't need to change - it just receives values already shifted
    to the user's local time.
    """
    tz = _resolve_timezone(tz_name)
    aware_utc = pytz.UTC.localize(dt_utc)
    return aware_utc.astimezone(tz).replace(tzinfo=None)


def get_user_local_hour(dt_utc: datetime, tz_name: str) -> int:
    """get the hour (0-23) of the day in the user's local timezone"""
    return to_user_timezone(dt_utc, tz_name).hour


def is_within_quiet_hours(local_hour: int, quiet_start: int, quiet_end: int) -> bool:
    """
    check whether a local hour falls within a quiet-hours window.

    handles windows that wrap past midnight (e.g. start=21, end=8 means
    quiet from 9pm through 8am) as well as windows that don't (e.g.
    start=1, end=5 means quiet only between 1am and 5am).
    """
    if quiet_start == quiet_end:
        # zero-width window - never quiet
        return False

    if quiet_start > quiet_end:
        # wraps past midnight, e.g. 21 -> 8
        return local_hour >= quiet_start or local_hour < quiet_end

    # same-day window, e.g. 1 -> 5
    return quiet_start <= local_hour < quiet_end


def local_day_bounds(day, tz_name: str) -> tuple:
    """the naive-utc half-open interval [start, end) covering one user-local
    calendar day - the reverse of to_user_timezone, for range queries over
    utc-stamped rows ("everything that happened today, their today"). dst
    transitions are handled by pytz's localize, so a 23- or 25-hour day
    is exactly as long as it really was."""
    from datetime import datetime, timedelta, time as _time

    tz = _resolve_timezone(tz_name)
    start = tz.localize(datetime.combine(day, _time.min))
    end = tz.localize(datetime.combine(day + timedelta(days=1), _time.min))
    return (start.astimezone(pytz.UTC).replace(tzinfo=None),
            end.astimezone(pytz.UTC).replace(tzinfo=None))
