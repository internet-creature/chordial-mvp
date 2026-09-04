"""the workspace vocabulary, encoded once (the schema.py philosophy, natively).

the DB stores canonical lowercase-snake values; the tool layer accepts display
strings case-insensitively - including the legacy notion vocabulary ("To do",
"In progress", "Not started", "recurring") - and renders the display form, so
the model-facing vocabulary never changed across the notion -> native move.

also home to: the open/closed status sets behind the lifecycle convention
(NATIVE_WORKSPACE_DESIGN section 2.0), public-id render/parse (t42, p3, ...),
and the one-line promptable formatters the digest and tools render.
"""
from __future__ import annotations

from typing import Optional


# --- canonical vocabularies --------------------------------------------------
# per entity: every canonical value, split into the open set and the closed
# set. closed always distinguishes a completed-ending from a released-ending.

PLAN_STATUS_OPEN = ["proposed", "active", "paused"]
PLAN_STATUS_CLOSED = ["complete", "released"]

GOAL_STATUS_OPEN = ["not_started", "in_progress"]
GOAL_STATUS_CLOSED = ["done", "renegotiated"]

TASK_STATUS_OPEN = ["todo", "in_progress"]
TASK_STATUS_CLOSED = ["done", "deprioritized"]

CYCLE_STATUS_OPEN = ["upcoming", "active"]
CYCLE_STATUS_CLOSED = ["complete"]

# ROOMS_DESIGN section 6: completing a commitment is progress; releasing
# one moves planned capacity (post-freeze that must be a scope change)
COMMITMENT_STATUS_OPEN = ["active"]
COMMITMENT_STATUS_CLOSED = ["completed", "released"]

NOTE_STATUS_OPEN = ["active"]
NOTE_STATUS_CLOSED = ["promoted", "archived"]

TASK_PRIORITY = ["high", "medium", "low"]
TASK_WINDOW = ["morning", "afternoon", "evening", "anytime"]
PLAN_CADENCE = ["daily", "weekly", "loose"]
WIN_WEIGHT = ["spark", "solid", "milestone"]
CHECKIN_KIND = ["morning", "evening", "adhoc"]
CHECKIN_ENERGY = ["low", "ok", "good", "great"]
OCCASION_RECURRENCE = ["yearly", "monthly", "weekly"]

STATUS_SETS = {
    "plan": (PLAN_STATUS_OPEN, PLAN_STATUS_CLOSED),
    "goal": (GOAL_STATUS_OPEN, GOAL_STATUS_CLOSED),
    "task": (TASK_STATUS_OPEN, TASK_STATUS_CLOSED),
    "cycle": (CYCLE_STATUS_OPEN, CYCLE_STATUS_CLOSED),
    "note": (NOTE_STATUS_OPEN, NOTE_STATUS_CLOSED),
    "commitment": (COMMITMENT_STATUS_OPEN, COMMITMENT_STATUS_CLOSED),
}


def is_closed_status(entity: str, status: str) -> bool:
    """does this canonical status belong to the entity's closed set?"""
    return status in STATUS_SETS[entity][1]


# --- display <-> canonical ---------------------------------------------------
# canonicalization is forgiving: case-insensitive, spaces and underscores
# interchangeable ("In progress", "in_progress", "IN PROGRESS" all land on
# 'in_progress'). legacy notion-only strings map onto the nearest canonical
# value so old muscle memory (the model's and dain's) keeps working.

_LEGACY_ALIASES = {
    "plan": {
        "to_do": "proposed",        # never valid for plans, but harmless
        "not_started": "proposed",  # legacy PROJECT_STATUS
        "in_progress": "active",    # legacy PROJECT_STATUS "In progress"
        "recurring": "active",      # legacy: recurring projects become active+loose
        "done": "complete",
    },
    "task": {
        "to_do": "todo",            # legacy TASK_STATUS "To do"
    },
    "goal": {},
    "cycle": {},
    "note": {},
}

# how canonical values are rendered back to the model/user. only values whose
# display form differs from .replace('_', ' ').capitalize() need an entry.
_DISPLAY = {
    "todo": "To do",
    "in_progress": "In progress",
    "not_started": "Not started",
    "done": "Done",
    "deprioritized": "deprioritized",   # legacy dainframe casing, kept verbatim
}


def _norm(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def canonical_status(entity: str, value: str) -> str:
    """canonicalize a status for the given entity ('plan'/'goal'/'task'/
    'cycle'/'note'), accepting canonical, display, and legacy forms. raises
    ValueError naming the valid options - tool handlers surface that message
    to the model, which then self-corrects."""
    open_set, closed_set = STATUS_SETS[entity]
    valid = open_set + closed_set
    key = _norm(value)
    key = _LEGACY_ALIASES.get(entity, {}).get(key, key)
    if key in valid:
        return key
    raise ValueError(
        f"unknown {entity} status {value!r} - valid: {', '.join(display(v) for v in valid)}")


def canonical_value(value: str, vocab: list[str], label: str) -> str:
    """canonicalize a non-status vocab value (priority, window, weight, ...)."""
    key = _norm(value)
    if key in vocab:
        return key
    raise ValueError(f"unknown {label} {value!r} - valid: {', '.join(vocab)}")


def display(value: Optional[str]) -> Optional[str]:
    """render a canonical value in its display form."""
    if value is None:
        return None
    return _DISPLAY.get(value, value.replace("_", " ").capitalize())


# --- public ids --------------------------------------------------------------
# native formatters render prefixed ids (t42, p3, g7, c2, w15, ci4, n7, o5);
# the tool layer parses whatever id string the model echoes back.

_PREFIXES = {
    "plan": "p", "goal": "g", "task": "t", "cycle": "c",
    "win": "w", "checkin": "ci", "note": "n", "occasion": "o",
    "commitment": "cm",
}
# longest prefix first so 'ci4' parses as checkin, not cycle
_PARSE_ORDER = sorted(_PREFIXES.items(), key=lambda kv: -len(kv[1]))


def public_id(kind: str, row_id: int) -> str:
    return f"{_PREFIXES[kind]}{row_id}"


def parse_public_id(value: str) -> Optional[tuple[str, int]]:
    """'t42' -> ('task', 42); None when the string isn't a public id (a title,
    probably - callers fall through to name resolution)."""
    s = value.strip().lower()
    for kind, prefix in _PARSE_ORDER:
        if s.startswith(prefix) and s[len(prefix):].isdigit():
            return kind, int(s[len(prefix):])
    return None


# --- one-line promptable formatters ------------------------------------------
# same style as the legacy schema.py formatters: short, one line, only the
# fields that exist. these take the plain-dict rows WorkspaceStore returns.


def _kv(parts: list[str], key: str, value) -> None:
    if value not in (None, "", []):
        parts.append(f"{key}={value}")


def format_task(row: dict) -> str:
    parts = [f'"{row["title"]}" [{display(row["status"])}]']
    _kv(parts, "prio", row.get("priority"))
    _kv(parts, "plan", row.get("plan_title"))
    _kv(parts, "goal", row.get("goal_title"))
    _kv(parts, "cycle", row.get("cycle_title"))
    _kv(parts, "scheduled", row.get("scheduled"))
    _kv(parts, "window", row.get("window"))
    _kv(parts, "pom", row.get("pom_estimate"))
    _kv(parts, "helper", row.get("helper"))
    if row.get("reschedules"):
        _kv(parts, "reschedules", row["reschedules"])
    if row.get("next_action"):
        _kv(parts, "next", f'"{row["next_action"]}"')
    if row.get("set_aside_on"):
        _kv(parts, "set-aside", row["set_aside_on"])
    _kv(parts, "id", row["public_id"])
    return " ".join(parts)


def format_plan(row: dict) -> str:
    parts = [f'"{row["title"]}" [{display(row["status"])}]']
    _kv(parts, "helper", row.get("helper"))
    _kv(parts, "cadence", row.get("cadence"))
    _kv(parts, "horizon", _range(row.get("horizon_start"), row.get("horizon_end")))
    _kv(parts, "id", row["public_id"])
    return " ".join(parts)


def format_goal(row: dict) -> str:
    parts = [f'"{row["title"]}" [{display(row["status"])}]']
    _kv(parts, "plan", row.get("plan_title"))
    _kv(parts, "target", row.get("target"))
    _kv(parts, "id", row["public_id"])
    return " ".join(parts)


def format_cycle(row: dict) -> str:
    parts = [f'"{row["title"]}" [{display(row["status"])}]']
    _kv(parts, "range", _range(row.get("start_date"), row.get("end_date")))
    _kv(parts, "theme", row.get("theme"))
    _kv(parts, "capacity", row.get("capacity_blocks"))
    _kv(parts, "focus", row.get("focus"))
    _kv(parts, "id", row["public_id"])
    return " ".join(parts)


def format_commitment(row: dict) -> str:
    parts = [f'"{row["title"]}" [{display(row["status"])}]']
    _kv(parts, "prio", row.get("priority"))
    _kv(parts, "blocks", row.get("blocks_planned"))
    _kv(parts, "plan", row.get("plan_title"))
    _kv(parts, "task", row.get("task_title"))
    if row.get("next_action"):
        _kv(parts, "next", f'"{row["next_action"]}"')
    _kv(parts, "id", row["public_id"])
    return " ".join(parts)


def format_win(row: dict) -> str:
    parts = [f'"{row["title"]}" ({row.get("weight") or "solid"})']
    _kv(parts, "date", row.get("date"))
    _kv(parts, "plan", row.get("plan_title"))
    _kv(parts, "by", row.get("helper"))
    _kv(parts, "id", row["public_id"])
    return " ".join(parts)


def format_checkin(row: dict) -> str:
    parts = [f'{row["date"]} {row["kind"]}']
    _kv(parts, "energy", row.get("energy"))
    _kv(parts, "by", row.get("helper"))
    _kv(parts, "id", row["public_id"])
    if row.get("notes"):
        parts.append(f'- {row["notes"]}')
    return " ".join(parts)


def format_note(row: dict) -> str:
    parts = [f'"{row["title"]}"' if row.get("title") else f'"{_first_line(row["body"])}"']
    if row.get("status") != "active":
        parts[0] += f' [{display(row["status"])}]'
    _kv(parts, "plan", row.get("plan_title"))
    if row.get("tags"):
        _kv(parts, "tags", ",".join(row["tags"]))
    _kv(parts, "id", row["public_id"])
    return " ".join(parts)


def format_occasion(row: dict) -> str:
    parts = [f'"{row["title"]}"']
    _kv(parts, "date", row.get("date"))
    _kv(parts, "time", row.get("time"))
    _kv(parts, "recurs", row.get("recurrence"))
    _kv(parts, "plan", row.get("plan_title"))
    _kv(parts, "id", row["public_id"])
    return " ".join(parts)


def _range(start, end) -> Optional[str]:
    if start and end:
        return f"{start}..{end}"
    return str(start) if start else (f"..{end}" if end else None)


def _first_line(body: str, cap: int = 60) -> str:
    line = body.strip().splitlines()[0] if body.strip() else ""
    return line if len(line) <= cap else line[: cap - 1] + "…"
