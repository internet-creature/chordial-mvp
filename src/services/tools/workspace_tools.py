"""the model-facing workspace tool surface (docs/WORKSPACE.md).

two groups, both always registered (see tools/__init__.py):

- WORKSPACE_CORE_TOOLS - the task/plan/cycle surface: list/create/update x
  tasks/projects/cycles (the v2-era names, contracts preserved) plus the
  plan-named aliases. the *_project tools operate on PLANS under the hood;
  the aliases retire with the v4 persona-prompt pass, which touches those
  prompt bytes anyway.
- WORKSPACE_EXTRA_TOOLS - the v3 additions (goals, wins, check-ins, notes,
  occasions).

conventions: handlers accept friendly arguments (names or public ids,
display-vocab statuses), name resolution never guesses between look-alikes
(ambiguity returns the candidates as the tool result), list_* tools don't
record events, and the store enforces every invariant - handlers translate,
they don't decide.

`helper` attribution on wins/check-ins/notes/occasions comes from the
acting-helper contextvar (the save_memory precedent), never from the model.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from config import Config
from dainframe.providers.types import ToolDef
from src.services.workspace import get_store, vocab
from src.services.workspace.agenda import user_today
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool
from src.services.identity import user_of_context

# the ids a plan may be stewarded by: the AUTHORED council (the persona
# cards - repo truth, env-independent, so tool-definition bytes never drift
# with a stale ENABLED_HELPERS). validated on write - a model-suggested
# steward that isn't a real resident would create an orphaned stewardship
# record. legacy rows keep their historical stewards; only new writes are
# held to the roster.
from src.personas import load_personas as _load_personas

_STEWARD_IDS = sorted(_load_personas())


def _valid_steward(helper: Optional[str]) -> Optional[str]:
    """corrective string if `helper` names a steward that isn't deployed,
    else None. absent/None is fine (handlers default to the acting helper)."""
    if helper is None or helper in _STEWARD_IDS:
        return None
    return (f"'{helper}' isn't a helper in this council - steward must be "
            f"one of: {', '.join(_STEWARD_IDS)}.")

logger = logging.getLogger(__name__)

# display-form enums for the model-facing schemas. tasks/cycles are
# byte-identical to the legacy notion vocab; plans show the v3 statuses
# (the canonicalizer still accepts the legacy strings).
_TASK_STATUS = ["To do", "In progress", "Done", "deprioritized"]
_TASK_PRIORITY = list(vocab.TASK_PRIORITY)
_TASK_WINDOW = list(vocab.TASK_WINDOW)
_PLAN_STATUS = ["Proposed", "Active", "Paused", "Complete", "Released"]
_CYCLE_STATUS = ["Upcoming", "Active", "Complete"]
_WIN_WEIGHT = list(vocab.WIN_WEIGHT)
_CHECKIN_KIND = list(vocab.CHECKIN_KIND)
_CHECKIN_ENERGY = list(vocab.CHECKIN_ENERGY)
_RECURRENCE = list(vocab.OCCASION_RECURRENCE)
_CADENCE = list(vocab.PLAN_CADENCE)


def _store():
    return get_store()


def _cap(n: Optional[int]) -> int:
    hi = Config.WORKSPACE_MAX_PAGE_SIZE
    if not n:
        return hi
    return max(1, min(int(n), hi))


def _candidates_msg(plural_noun: str, name: str, candidates: list[dict]) -> str:
    """a promptable refusal: list the candidates and how to disambiguate
    (same shape the notion resolver used - the tool result is the
    clarification channel)."""
    shown = candidates[:5]
    listing = ", ".join(f'"{c["title"]}" (id={c["public_id"]})' for c in shown)
    extra = len(candidates) - len(shown)
    more = f", and {extra} more (use a list tool to narrow it)" if extra > 0 else ""
    return (
        f"multiple {plural_noun} match '{name}': {listing}{more} - "
        "retry with the id, or ask which one they meant."
    )


def _resolve(user_uuid: str, entity: str, ref: str, plural_noun: str):
    """(row, None) on a unique match, (None, promptable-error) otherwise."""
    result = _store().resolve(user_uuid, entity, ref.strip())
    if result.match is not None:
        return result.match, None
    if result.candidates:
        return None, _candidates_msg(plural_noun, ref, result.candidates)
    return None, f"no {entity} matching '{ref}'."


def _resolve_id(user_uuid: str, entity: str, ref, plural_noun: str):
    """like _resolve but returns (id, None); passes None refs through."""
    if ref is None:
        return None, None
    row, err = _resolve(user_uuid, entity, str(ref), plural_noun)
    return (row["id"], None) if row else (None, err)


def _fields_note(changes: dict) -> str:
    return ", ".join(sorted(changes.keys()))


# ============================ TASKS ========================================


async def _list_tasks(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    store = _store()
    plan_id, err = _resolve_id(user_uuid, "plan", tool_input.get("project"), "plans")
    if err:
        return err
    cycle_id, err = _resolve_id(user_uuid, "cycle", tool_input.get("sprint"), "cycles")
    if err:
        return err
    rows = store.list_tasks(
        user_uuid,
        status=tool_input.get("status"),
        plan_id=plan_id, cycle_id=cycle_id,
        scheduled_on_or_after=tool_input.get("scheduled_on_or_after"),
        scheduled_on_or_before=tool_input.get("scheduled_on_or_before"),
        include_closed=bool(tool_input.get("include_closed")),
        limit=_cap(tool_input.get("limit")),
    )
    if tool_input.get("priority"):
        want = vocab.canonical_value(tool_input["priority"], vocab.TASK_PRIORITY, "priority")
        rows = [r for r in rows if r["priority"] == want]
    if not rows:
        return "no tasks matched."
    return f"{len(rows)} task(s):\n" + "\n".join(f"- {vocab.format_task(r)}" for r in rows)


def _task_link_ids(tool_input: dict, user_uuid: str):
    """resolve the optional project/sprint/goal name args to ids.
    returns (kwargs, None) or (None, promptable-error)."""
    out = {}
    for key, entity, noun, store_key in (
        ("project", "plan", "plans", "plan_id"),
        ("sprint", "cycle", "cycles", "cycle_id"),
        ("goal", "goal", "goals", "goal_id"),
    ):
        if tool_input.get(key) is not None:
            rid, err = _resolve_id(user_uuid, entity, tool_input[key], noun)
            if err:
                return None, err
            out[store_key] = rid
    return out, None


async def _create_task(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    title = (tool_input.get("title") or "").strip()
    if not title:
        return "a task needs a title."
    links, err = _task_link_ids(tool_input, user_uuid)
    if err:
        return err
    row = _store().create_task(
        user_uuid, title,
        status=tool_input.get("status") or "To do",
        priority=tool_input.get("priority"),
        scheduled=tool_input.get("scheduled_date"),
        window=tool_input.get("window"),
        pom_estimate=tool_input.get("pom_estimate"),
        helper=tool_input.get("helper"),
        description=tool_input.get("description"),
        next_action=tool_input.get("next_action"),
        **links,
    )
    return f"created task \"{title}\" (id={row['public_id']})."


async def _update_task(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    ident = (tool_input.get("task") or "").strip()
    if not ident:
        return "which task? pass a task title or id."
    target, err = _resolve(user_uuid, "task", ident, "tasks")
    if err:
        return err
    links, err = _task_link_ids(tool_input, user_uuid)
    if err:
        return err
    changes = dict(links or {})
    for key, store_key in (("new_title", "title"), ("status", "status"),
                           ("priority", "priority"), ("scheduled_date", "scheduled"),
                           ("pom_estimate", "pom_estimate"), ("window", "window"),
                           ("helper", "helper"), ("description", "description"),
                           ("next_action", "next_action")):
        if tool_input.get(key) is not None:
            changes[store_key] = tool_input[key]
    if not changes:
        return "nothing to update - pass at least one field to change."
    row = _store().update_task(user_uuid, target["id"], **changes)
    return f"updated task (id={row['public_id']}): {_fields_note(changes)}."


# ============================ PLANS (projects) =============================


async def _list_plans(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    rows = _store().list_plans(
        user_uuid,
        status=tool_input.get("status"),
        helper=tool_input.get("helper"),
        include_closed=bool(tool_input.get("include_closed")),
    )
    if tool_input.get("area"):
        areas = tool_input["area"] if isinstance(tool_input["area"], list) else [tool_input["area"]]
        rows = [r for r in rows if r["legacy_area"] in areas]
    if not rows:
        return "no plans matched."
    return f"{len(rows)} plan(s):\n" + "\n".join(f"- {vocab.format_plan(r)}" for r in rows)


async def _create_plan(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    title = (tool_input.get("title") or "").strip()
    if not title:
        return "a plan needs a title."
    steward_err = _valid_steward(tool_input.get("helper"))
    if steward_err:
        return steward_err
    area = tool_input.get("area")
    if isinstance(area, list):
        area = area[0] if area else None
    row = _store().create_plan(
        user_uuid, title,
        helper=tool_input.get("helper") or context.actor,
        status=tool_input.get("status") or "proposed",
        why=tool_input.get("why") or tool_input.get("description"),
        success_criteria=tool_input.get("success_criteria"),
        cadence=tool_input.get("cadence"),
        legacy_area=area,
    )
    return f"created plan \"{title}\" (id={row['public_id']}, steward={row['helper']})."


async def _update_plan(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    ident = (tool_input.get("plan") or tool_input.get("project") or "").strip()
    if not ident:
        return "which plan? pass a plan title or id."
    steward_err = _valid_steward(tool_input.get("helper"))
    if steward_err:
        return steward_err
    target, err = _resolve(user_uuid, "plan", ident, "plans")
    if err:
        return err
    changes = {}
    for key, store_key in (("new_title", "title"), ("status", "status"),
                           ("helper", "helper"), ("why", "why"),
                           ("success_criteria", "success_criteria"),
                           ("cadence", "cadence"),
                           ("horizon_start", "horizon_start"),
                           ("horizon_end", "horizon_end")):
        if tool_input.get(key) is not None:
            changes[store_key] = tool_input[key]
    if tool_input.get("description") is not None and "why" not in changes:
        changes["why"] = tool_input["description"]
    if not changes:
        return "nothing to update - pass at least one field to change."
    row = _store().update_plan(user_uuid, target["id"], **changes)
    return f"updated plan (id={row['public_id']}): {_fields_note(changes)}."


# ============================ CYCLES =======================================


async def _list_cycles(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    rows = _store().list_cycles(
        user_uuid, include_closed=bool(tool_input.get("include_closed")))
    if tool_input.get("status"):
        want = vocab.canonical_status("cycle", tool_input["status"])
        rows = [r for r in rows if r["status"] == want]
    rows = list(reversed(rows))[: _cap(tool_input.get("limit"))]   # newest first
    if not rows:
        return "no cycles matched."
    return f"{len(rows)} cycle(s):\n" + "\n".join(f"- {vocab.format_cycle(r)}" for r in rows)


async def _create_cycle(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    title = (tool_input.get("title") or "").strip()
    if not title:
        return "a cycle needs a title."
    row = _store().create_cycle(
        user_uuid, title,
        status=tool_input.get("status") or "Upcoming",
        start_date=tool_input.get("start_date"),
        end_date=tool_input.get("end_date"),
        goal=tool_input.get("goal"),
        focus=tool_input.get("focus") or tool_input.get("description"),
        theme=tool_input.get("theme"),
        capacity_blocks=tool_input.get("capacity_blocks"),
    )
    return f"created cycle \"{title}\" (id={row['public_id']})."


async def _update_cycle(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    ident = (tool_input.get("cycle") or "").strip()
    if not ident:
        return "which cycle? pass a cycle title or id."
    target, err = _resolve(user_uuid, "cycle", ident, "cycles")
    if err:
        return err
    changes = {}
    for key, store_key in (("new_title", "title"), ("status", "status"),
                           ("start_date", "start_date"), ("end_date", "end_date"),
                           ("goal", "goal"), ("focus", "focus"),
                           ("theme", "theme"),
                           ("capacity_blocks", "capacity_blocks")):
        if tool_input.get(key) is not None:
            changes[store_key] = tool_input[key]
    if tool_input.get("description") is not None and "focus" not in changes:
        changes["focus"] = tool_input["description"]
    if not changes:
        return "nothing to update - pass at least one field to change."
    try:
        row = _store().update_cycle(user_uuid, target["id"], **changes)
    except ValueError as e:
        # e.g. capacity on a frozen cycle - the store's redirect to
        # change_scope is the promptable answer
        return str(e)
    return f"updated cycle (id={row['public_id']}): {_fields_note(changes)}."


# ============================ GOALS ========================================


async def _create_goal(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    title = (tool_input.get("title") or "").strip()
    if not title:
        return "a goal needs a title."
    plan_id, err = _resolve_id(user_uuid, "plan", tool_input.get("plan"), "plans")
    if err:
        return err
    if plan_id is None:
        return "a goal belongs to a plan - pass the plan's name or id."
    row = _store().create_goal(
        user_uuid, plan_id, title,
        status=tool_input.get("status") or "not_started",
        target=tool_input.get("target"),
        done_means=tool_input.get("done_means"),
    )
    return f"created goal \"{title}\" under \"{row['plan_title']}\" (id={row['public_id']})."


async def _update_goal(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    ident = (tool_input.get("goal") or "").strip()
    if not ident:
        return "which goal? pass a goal title or id."
    target, err = _resolve(user_uuid, "goal", ident, "goals")
    if err:
        return err
    changes = {}
    for key, store_key in (("new_title", "title"), ("status", "status"),
                           ("target", "target"), ("done_means", "done_means")):
        if tool_input.get(key) is not None:
            changes[store_key] = tool_input[key]
    if not changes:
        return "nothing to update - pass at least one field to change."
    row = _store().update_goal(user_uuid, target["id"], **changes)
    return f"updated goal (id={row['public_id']}): {_fields_note(changes)}."


async def _list_goals(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    plan_id, err = _resolve_id(user_uuid, "plan", tool_input.get("plan"), "plans")
    if err:
        return err
    rows = _store().list_goals(
        user_uuid, plan_id=plan_id,
        include_closed=bool(tool_input.get("include_closed")))
    if not rows:
        return "no goals matched."
    return f"{len(rows)} goal(s):\n" + "\n".join(f"- {vocab.format_goal(r)}" for r in rows)


# ============================ WINS =========================================


async def _log_win(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    title = (tool_input.get("title") or "").strip()
    if not title:
        return "a win needs a title (past-tense, concrete)."
    plan_id, err = _resolve_id(user_uuid, "plan", tool_input.get("plan"), "plans")
    if err:
        return err
    task_id, err = _resolve_id(user_uuid, "task", tool_input.get("task"), "tasks")
    if err:
        return err
    row = _store().log_win(
        user_uuid, title,
        tool_input.get("date") or user_today(user_uuid).isoformat(),
        context.actor,
        plan_id=plan_id, task_id=task_id,
        evidence=tool_input.get("evidence"),
        weight=tool_input.get("weight") or "solid",
    )
    return f"logged win \"{title}\" (id={row['public_id']})."


async def _list_wins(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    plan_id, err = _resolve_id(user_uuid, "plan", tool_input.get("plan"), "plans")
    if err:
        return err
    rows = _store().list_wins(
        user_uuid,
        since=tool_input.get("since"),
        plan_id=plan_id,
        weight=tool_input.get("weight"),
        limit=_cap(tool_input.get("limit")),
    )
    if not rows:
        return "no wins recorded for that filter (yet)."
    return f"{len(rows)} win(s):\n" + "\n".join(f"- {vocab.format_win(r)}" for r in rows)


# ============================ CHECK-INS ====================================


async def _log_checkin(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    kind = tool_input.get("kind") or "adhoc"
    plan_ids = []
    for ref in tool_input.get("plans_touched") or []:
        pid, err = _resolve_id(user_uuid, "plan", ref, "plans")
        if err:
            return err
        plan_ids.append(pid)
    row = _store().log_checkin(
        user_uuid,
        tool_input.get("date") or user_today(user_uuid).isoformat(),
        kind, context.actor,
        energy=tool_input.get("energy"),
        notes=tool_input.get("notes"),
        plan_ids=plan_ids,
    )
    return f"logged {row['kind']} check-in for {row['date']} (id={row['public_id']})."


async def _list_checkins(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    rows = _store().list_checkins(
        user_uuid,
        since=tool_input.get("since"),
        kind=tool_input.get("kind"),
        limit=_cap(tool_input.get("limit")),
    )
    if not rows:
        return "no check-ins recorded for that filter."
    return f"{len(rows)} check-in(s):\n" + "\n".join(f"- {vocab.format_checkin(r)}" for r in rows)


# ============================ NOTES ========================================


async def _jot(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    body = (tool_input.get("body") or "").strip()
    if not body:
        return "what should i jot down? pass the idea as 'body'."
    plan_id, err = _resolve_id(user_uuid, "plan", tool_input.get("plan"), "plans")
    if err:
        return err
    row = _store().jot(
        user_uuid, body,
        title=tool_input.get("title"),
        plan_id=plan_id,
        tags=tool_input.get("tags"),
        helper=context.actor,
    )
    where = f" on \"{row['plan_title']}\"" if row.get("plan_title") else ""
    return f"jotted{where} (id={row['public_id']})."


async def _list_notes(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    plan_id, err = _resolve_id(user_uuid, "plan", tool_input.get("plan"), "plans")
    if err:
        return err
    rows = _store().list_notes(
        user_uuid, plan_id=plan_id,
        tag=tool_input.get("tag"),
        since=tool_input.get("since"),
        query=tool_input.get("query"),
        include_closed=bool(tool_input.get("include_closed")),
    )[: _cap(tool_input.get("limit"))]
    if not rows:
        return "no notes matched."
    return f"{len(rows)} note(s):\n" + "\n".join(f"- {vocab.format_note(r)}" for r in rows)


async def _update_note(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    ident = (tool_input.get("note") or "").strip()
    if not ident:
        return "which note? pass its id (n7) or title."
    target, err = _resolve(user_uuid, "note", ident, "notes")
    if err:
        return err
    changes = {}
    for key, store_key in (("body", "body"), ("new_title", "title"),
                           ("tags", "tags"), ("status", "status")):
        if tool_input.get(key) is not None:
            changes[store_key] = tool_input[key]
    if tool_input.get("plan") is not None:
        pid, err = _resolve_id(user_uuid, "plan", tool_input["plan"], "plans")
        if err:
            return err
        changes["plan_id"] = pid
    if tool_input.get("promoted_task") is not None:
        tid, err = _resolve_id(user_uuid, "task", tool_input["promoted_task"], "tasks")
        if err:
            return err
        changes["promoted_task_id"] = tid
        changes.setdefault("status", "promoted")
    if tool_input.get("promoted_plan") is not None:
        pid, err = _resolve_id(user_uuid, "plan", tool_input["promoted_plan"], "plans")
        if err:
            return err
        changes["promoted_plan_id"] = pid
        changes.setdefault("status", "promoted")
    if not changes:
        return "nothing to update - pass at least one field to change."
    row = _store().update_note(user_uuid, target["id"], **changes)
    return f"updated note (id={row['public_id']}): {_fields_note(changes)}."


# ============================ OCCASIONS ====================================


async def _log_occasion(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    title = (tool_input.get("title") or "").strip()
    if not title:
        return "an occasion needs a title."
    if not tool_input.get("date"):
        return "an occasion needs a date (YYYY-MM-DD)."
    plan_id, err = _resolve_id(user_uuid, "plan", tool_input.get("plan"), "plans")
    if err:
        return err
    row = _store().create_occasion(
        user_uuid, title, tool_input["date"],
        time=tool_input.get("time"),
        recurrence=tool_input.get("recurrence"),
        plan_id=plan_id,
        notes=tool_input.get("notes"),
        helper=context.actor,
    )
    recurs = f", recurs {row['recurrence']}" if row.get("recurrence") else ""
    return f"noted occasion \"{title}\" on {row['date']}{recurs} (id={row['public_id']})."


async def _list_occasions(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    plan_id, err = _resolve_id(user_uuid, "plan", tool_input.get("plan"), "plans")
    if err:
        return err
    today = user_today(user_uuid)
    until = tool_input.get("until") or (today + timedelta(days=30)).isoformat()
    rows = _store().list_occasions(
        user_uuid, today=today.isoformat(), until=until,
        plan_id=plan_id,
        include_past=bool(tool_input.get("include_past")),
    )
    if not rows:
        return f"nothing coming up through {until}."
    return f"{len(rows)} occasion(s):\n" + "\n".join(
        f"- {vocab.format_occasion(r)}" for r in rows)


async def _update_occasion(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    ident = (tool_input.get("occasion") or "").strip()
    if not ident:
        return "which occasion? pass its id (o5) or title."
    target, err = _resolve(user_uuid, "occasion", ident, "occasions")
    if err:
        return err
    changes = {}
    for key, store_key in (("new_title", "title"), ("date", "date"),
                           ("time", "time"), ("recurrence", "recurrence"),
                           ("notes", "notes")):
        if tool_input.get(key) is not None:
            changes[store_key] = tool_input[key]
    if tool_input.get("plan") is not None:
        pid, err = _resolve_id(user_uuid, "plan", tool_input["plan"], "plans")
        if err:
            return err
        changes["plan_id"] = pid
    if not changes:
        return "nothing to update - pass at least one field to change."
    row = _store().update_occasion(user_uuid, target["id"], **changes)
    return f"updated occasion (id={row['public_id']}): {_fields_note(changes)}."


# ============================ TOOL DEFS ====================================

_INCLUDE_CLOSED = {"type": "boolean",
                   "description": "Include completed/released/archived items (default false)."}
_LIMIT = {"type": "integer", "description": "Max rows to return."}
_ISO_DATE = {"type": "string", "description": "ISO date (YYYY-MM-DD)."}


def _tool(name, description, properties, handler, *, required=None,
          record_event=True, terminal=False) -> Tool:
    return Tool(
        definition=ToolDef(
            name=name, description=description,
            input_schema={"type": "object", "properties": properties,
                          **({"required": required} if required else {})},
        ),
        handler=handler, record_event=record_event, terminal=terminal,
    )


_TASK_WRITE_PROPS = {
    "status": {"type": "string", "enum": _TASK_STATUS},
    "priority": {"type": "string", "enum": _TASK_PRIORITY},
    "project": {"type": "string", "description": "Plan name or id to link to."},
    "sprint": {"type": "string", "description": "Cycle name or id to link to."},
    "goal": {"type": "string", "description": "Goal name or id to link to (implies its plan)."},
    "scheduled_date": _ISO_DATE,
    "window": {"type": "string", "enum": _TASK_WINDOW,
               "description": "Part of day this task wants."},
    "pom_estimate": {"type": "number", "description": "Estimated pomodoros."},
    "helper": {"type": "string", "description": "Helper who assigned/nudges it."},
    "description": {"type": "string"},
    "next_action": {"type": "string",
                    "description": "The scope: the one-line first piece a "
                                   "focus block runs on (max 140 chars). "
                                   "Pass '' to clear."},
}

LIST_TASKS = _tool(
    "list_tasks",
    "List the user's tasks. Filter by any combination of status, priority, "
    "plan (project), cycle (sprint), and scheduled date range. You already "
    "see a compact agenda summary with each message; use this to look beyond "
    "it. Results are sorted by scheduled date; each line ends with the "
    "task's id.",
    {
        "status": {"type": "string", "enum": _TASK_STATUS},
        "priority": {"type": "string", "enum": _TASK_PRIORITY},
        "project": {"type": "string", "description": "Plan name to filter by."},
        "sprint": {"type": "string", "description": "Cycle name to filter by."},
        "scheduled_on_or_after": _ISO_DATE,
        "scheduled_on_or_before": _ISO_DATE,
        "include_closed": _INCLUDE_CLOSED,
        "limit": _LIMIT,
    },
    _list_tasks, record_event=False,
)

CREATE_TASK = _tool(
    "create_task",
    "Create a task. Only 'title' is required; defaults status to 'To do'. "
    "Link it to a plan/cycle/goal by name or id. Use when the user wants to "
    "capture a to-do - tasks are pomodoro-sized; bigger ambitions are plans.",
    {"title": {"type": "string", "description": "The task text."}, **_TASK_WRITE_PROPS},
    _create_task, required=["title"],
)

UPDATE_TASK = _tool(
    "update_task",
    "Update a task (identify by title or id via 'task'). Pass only fields to "
    "change: mark done (status='Done'), reprioritize, reschedule, re-link, "
    "or rename (new_title). Rescheduling later counts a slip; renegotiate "
    "kindly around 2-3.",
    {"task": {"type": "string", "description": "Title or id of the task."},
     "new_title": {"type": "string"}, **_TASK_WRITE_PROPS},
    _update_task, required=["task"],
)

_PLAN_WRITE_PROPS = {
    "status": {"type": "string", "enum": _PLAN_STATUS},
    "helper": {"type": "string",
               "description": f"Steward helper id ({'/'.join(_STEWARD_IDS)})."},
    "why": {"type": "string", "description": "The user's own motivation, their words."},
    "success_criteria": {"type": "string", "description": "What success looks like."},
    "cadence": {"type": "string", "enum": _CADENCE},
    "area": {"type": "array", "items": {"type": "string"},
             "description": "Legacy area tags."},
    "description": {"type": "string", "description": "Alias for 'why'."},
}

LIST_PLANS_PROPS = {
    "status": {"type": "string", "enum": _PLAN_STATUS},
    "helper": {"type": "string", "description": "Only plans stewarded by this helper."},
    "area": {"type": "string", "description": "Legacy area tag to filter by."},
    "include_closed": _INCLUDE_CLOSED,
    "limit": _LIMIT,
}

LIST_PROJECTS = _tool(
    "list_projects",
    "List the user's plans (formerly 'projects'), optionally filtered by "
    "status, steward helper, or legacy area.",
    LIST_PLANS_PROPS, _list_plans, record_event=False,
)
LIST_PLANS = _tool(
    "list_plans",
    "List the user's plans, optionally filtered by status, steward helper, "
    "or legacy area. Each line ends with the plan's id.",
    LIST_PLANS_PROPS, _list_plans, record_event=False,
)

CREATE_PROJECT = _tool(
    "create_project",
    "Create a plan (formerly 'project') - a helper-stewarded body of work, "
    "can be lofty/multi-month. Only 'title' is required; defaults to "
    "status 'Proposed' with the acting helper as steward.",
    {"title": {"type": "string"}, **_PLAN_WRITE_PROPS},
    _create_plan, required=["title"],
)
CREATE_PLAN = _tool(
    "create_plan",
    "Create a plan - a helper-stewarded body of work, can be lofty/"
    "multi-month. Only 'title' is required; defaults to status 'Proposed' "
    "with the acting helper as steward. Raise 'why' in conversation, don't "
    "demand it.",
    {"title": {"type": "string"}, **_PLAN_WRITE_PROPS},
    _create_plan, required=["title"],
)

UPDATE_PROJECT = _tool(
    "update_project",
    "Update a plan (formerly 'project'; identify by title or id via "
    "'project'). Pass only fields to change.",
    {"project": {"type": "string", "description": "Title or id of the plan."},
     "new_title": {"type": "string"},
     "horizon_start": _ISO_DATE, "horizon_end": _ISO_DATE,
     **_PLAN_WRITE_PROPS},
    _update_plan, required=["project"],
)
UPDATE_PLAN = _tool(
    "update_plan",
    "Update a plan (identify by title or id via 'plan'). Pass only fields "
    "to change - status, steward, why, success_criteria, cadence, horizon.",
    {"plan": {"type": "string", "description": "Title or id of the plan."},
     "new_title": {"type": "string"},
     "horizon_start": _ISO_DATE, "horizon_end": _ISO_DATE,
     **_PLAN_WRITE_PROPS},
    _update_plan, required=["plan"],
)

LIST_CYCLES = _tool(
    "list_cycles",
    "List cycles, newest first, optionally filtered by status. Use to find "
    "the current cycle (status='Active').",
    {"status": {"type": "string", "enum": _CYCLE_STATUS},
     "include_closed": _INCLUDE_CLOSED, "limit": _LIMIT},
    _list_cycles, record_event=False,
)

_CYCLE_THEME = {"type": "string",
                "description": "The cycle's theme, the user's words "
                               "('build rhythm without overloading')."}
_CYCLE_CAPACITY = {"type": "integer",
                   "description": "Honest capacity estimate in focus "
                                  "blocks (25 min each)."}

CREATE_CYCLE = _tool(
    "create_cycle",
    "Create a cycle (the bi-weekly balancing window). Only 'title' is "
    "required; defaults status to 'Upcoming'. Give it a date range, a goal, "
    "a 'focus' (the negotiated balance statement), a theme, and a capacity "
    "estimate in focus blocks when known.",
    {"title": {"type": "string"},
     "status": {"type": "string", "enum": _CYCLE_STATUS},
     "start_date": _ISO_DATE, "end_date": _ISO_DATE,
     "goal": {"type": "string", "description": "The cycle goal."},
     "focus": {"type": "string", "description": "The balance statement across plans."},
     "theme": _CYCLE_THEME,
     "capacity_blocks": _CYCLE_CAPACITY,
     "description": {"type": "string", "description": "Alias for 'focus'."}},
    _create_cycle, required=["title"],
)

UPDATE_CYCLE = _tool(
    "update_cycle",
    "Update a cycle (identify by title or id via 'cycle'). Pass only fields "
    "to change. Once the cycle's baseline is frozen, capacity_blocks must "
    "move through change_scope instead.",
    {"cycle": {"type": "string", "description": "Title or id of the cycle."},
     "new_title": {"type": "string"},
     "status": {"type": "string", "enum": _CYCLE_STATUS},
     "start_date": _ISO_DATE, "end_date": _ISO_DATE,
     "goal": {"type": "string"}, "focus": {"type": "string"},
     "theme": _CYCLE_THEME,
     "capacity_blocks": _CYCLE_CAPACITY,
     "description": {"type": "string", "description": "Alias for 'focus'."}},
    _update_cycle, required=["cycle"],
)

CREATE_GOAL = _tool(
    "create_goal",
    "Create a goal under a plan - a concrete milestone. 'done_means' is the "
    "anti-vagueness field: what will be true when it's done.",
    {"plan": {"type": "string", "description": "Plan name or id it belongs to."},
     "title": {"type": "string"},
     "status": {"type": "string", "enum": ["Not started", "In progress"]},
     "target": _ISO_DATE,
     "done_means": {"type": "string"}},
    _create_goal, required=["plan", "title"],
)

UPDATE_GOAL = _tool(
    "update_goal",
    "Update a goal (identify by title or id via 'goal'). status 'Done' "
    "completes it; 'renegotiated' is the no-shame way to let one go.",
    {"goal": {"type": "string", "description": "Title or id of the goal."},
     "new_title": {"type": "string"},
     "status": {"type": "string",
                "enum": ["Not started", "In progress", "Done", "renegotiated"]},
     "target": _ISO_DATE, "done_means": {"type": "string"}},
    _update_goal, required=["goal"],
)

LIST_GOALS = _tool(
    "list_goals",
    "List goals, optionally for one plan.",
    {"plan": {"type": "string"}, "include_closed": _INCLUDE_CLOSED},
    _list_goals, record_event=False,
)

LOG_WIN = _tool(
    "log_win",
    "Log a win in the user's ledger - past-tense and concrete, with their "
    "own words as evidence when you have them. Log liberally: sparks count. "
    "The ledger exists so accomplishments can't be diminished later.",
    {"title": {"type": "string", "description": "Past-tense, concrete."},
     "date": _ISO_DATE,
     "evidence": {"type": "string", "description": "The user's words, verbatim."},
     "weight": {"type": "string", "enum": _WIN_WEIGHT},
     "plan": {"type": "string", "description": "Plan name or id it advanced."},
     "task": {"type": "string", "description": "Task id it grew from."}},
    _log_win, required=["title"],
)

LIST_WINS = _tool(
    "list_wins",
    "List recorded wins, newest first. Filter by since-date, plan, or weight.",
    {"since": _ISO_DATE, "plan": {"type": "string"},
     "weight": {"type": "string", "enum": _WIN_WEIGHT}, "limit": _LIMIT},
    _list_wins, record_event=False,
)

LOG_CHECKIN = _tool(
    "log_checkin",
    "Record a daily check-in (the shared journal). One morning and one "
    "evening per day; adhoc unlimited. Energy is asked, never demanded.",
    {"kind": {"type": "string", "enum": _CHECKIN_KIND},
     "date": _ISO_DATE,
     "energy": {"type": "string", "enum": _CHECKIN_ENERGY},
     "notes": {"type": "string", "description": "What the user said about the day."},
     "plans_touched": {"type": "array", "items": {"type": "string"},
                       "description": "Plan names or ids that came up."}},
    _log_checkin, required=["kind"],
)

LIST_CHECKINS = _tool(
    "list_checkins",
    "List past check-ins, newest first.",
    {"since": _ISO_DATE, "kind": {"type": "string", "enum": _CHECKIN_KIND},
     "limit": _LIMIT},
    _list_checkins, record_event=False,
)

JOT = _tool(
    "jot",
    "Jot down an idea - creative sparks (writing, music, video), or detail "
    "for a plan (a story idea for their book). Only 'body' is needed; "
    "attach to a plan when it clearly belongs to one. Notes are never "
    "tasks: no dates, no pressure, never overdue.",
    {"body": {"type": "string", "description": "The idea, in their words."},
     "title": {"type": "string"},
     "plan": {"type": "string", "description": "Plan name or id to attach to."},
     "tags": {"type": "array", "items": {"type": "string"},
              "description": "Medium tags: writing/music/video/..."}},
    _jot, required=["body"], terminal=True,
)

LIST_NOTES = _tool(
    "list_notes",
    "List jotted notes. Filter by plan, tag, since-date, or a substring "
    "query. Pull a plan's notes when work starts on it.",
    {"plan": {"type": "string"}, "tag": {"type": "string"},
     "since": _ISO_DATE,
     "query": {"type": "string", "description": "Substring over title+body."},
     "include_closed": _INCLUDE_CLOSED, "limit": _LIMIT},
    _list_notes, record_event=False,
)

UPDATE_NOTE = _tool(
    "update_note",
    "Update a note (identify by id like n7, or title): edit, re-tag, attach "
    "to a plan, archive, or record that it grew up (promoted_task/"
    "promoted_plan link the thing it became).",
    {"note": {"type": "string", "description": "Id (n7) or title of the note."},
     "body": {"type": "string"}, "new_title": {"type": "string"},
     "plan": {"type": "string"},
     "tags": {"type": "array", "items": {"type": "string"}},
     "status": {"type": "string", "enum": ["active", "promoted", "archived"]},
     "promoted_task": {"type": "string", "description": "Task it was promoted into."},
     "promoted_plan": {"type": "string", "description": "Plan it was promoted into."}},
    _update_note, required=["note"],
)

LOG_OCCASION = _tool(
    "log_occasion",
    "Note a dated thing that isn't work - a birthday, an appointment, a "
    "flight. Occasions inform, never nag: no status, nothing overdue. "
    "Recurrence keeps birthdays yearly.",
    {"title": {"type": "string"},
     "date": _ISO_DATE,
     "time": {"type": "string", "description": "Freeform ('14:30', 'afternoon')."},
     "recurrence": {"type": "string", "enum": _RECURRENCE},
     "plan": {"type": "string", "description": "Plan it belongs to, if any."},
     "notes": {"type": "string"}},
    _log_occasion, required=["title", "date"],
)

LIST_OCCASIONS = _tool(
    "list_occasions",
    "List upcoming occasions in date order (default: the next 30 days).",
    {"until": _ISO_DATE, "plan": {"type": "string"},
     "include_past": {"type": "boolean",
                      "description": "Include past one-offs (history)."}},
    _list_occasions, record_event=False,
)

UPDATE_OCCASION = _tool(
    "update_occasion",
    "Update an occasion (identify by id like o5, or title). Pass only "
    "fields to change.",
    {"occasion": {"type": "string", "description": "Id (o5) or title."},
     "new_title": {"type": "string"}, "date": _ISO_DATE,
     "time": {"type": "string"},
     "recurrence": {"type": "string", "enum": _RECURRENCE},
     "plan": {"type": "string"}, "notes": {"type": "string"}},
    _update_occasion, required=["occasion"],
)


# the task/plan/cycle surface (the v2-era names plus plan aliases; the
# aliases retire with the v4 persona-prompt pass)
WORKSPACE_CORE_TOOLS = [
    LIST_TASKS, CREATE_TASK, UPDATE_TASK,
    LIST_PROJECTS, CREATE_PROJECT, UPDATE_PROJECT,
    LIST_PLANS, CREATE_PLAN, UPDATE_PLAN,
    LIST_CYCLES, CREATE_CYCLE, UPDATE_CYCLE,
]

# the v3 additions
WORKSPACE_EXTRA_TOOLS = [
    CREATE_GOAL, UPDATE_GOAL, LIST_GOALS,
    LOG_WIN, LIST_WINS,
    LOG_CHECKIN, LIST_CHECKINS,
    JOT, LIST_NOTES, UPDATE_NOTE,
    LOG_OCCASION, LIST_OCCASIONS, UPDATE_OCCASION,
]
