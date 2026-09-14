"""the model-facing cycle-spine surface (docs/ROOMS_DESIGN.md section 6).

pip's lane: commitments, the freeze, scope changes, and the live cycle
picture. same conventions as workspace_tools - friendly refs (titles or
public ids like cm3/c2), display vocab, ambiguity comes back as candidates,
and CycleStore enforces every invariant while handlers only translate.

the shape the tools teach: plan freely before the freeze; after it, changes
of plan are welcome but explicit (change_scope with the user's reason) -
honest data for edwin, zero guilt for the human.
"""
from __future__ import annotations

import logging
from typing import Optional

from dainframe.providers.types import ToolDef
from dainframe.tools.context import ToolContext
from dainframe.tools.registry import Tool

from src.services.cycles import CycleStore, CycleStoreError
from src.services.identity import user_of_context
from src.services.tools.inputs import blank_tolerant
from src.services.workspace import get_store, vocab

logger = logging.getLogger(__name__)

_COMMITMENT_STATUS = ["Active", "Completed", "Released"]
_PRIORITY = list(vocab.TASK_PRIORITY)


def _cycles() -> CycleStore:
    return CycleStore()


# --- resolution ---------------------------------------------------------------

def _candidates_msg(plural: str, name: str, candidates: list[dict]) -> str:
    shown = candidates[:5]
    listing = ", ".join(f'"{c["title"]}" (id={c["public_id"]})' for c in shown)
    extra = len(candidates) - len(shown)
    more = f", and {extra} more" if extra > 0 else ""
    return (f"multiple {plural} match '{name}': {listing}{more} - "
            "retry with the id, or ask which one they meant.")


def _resolve_workspace_id(user_uuid: str, entity: str, ref,
                          plural: str):
    """resolve a plan/task/cycle ref via the workspace resolver.
    returns (id or None, promptable-error or None); None refs pass through."""
    if ref is None:
        return None, None
    result = get_store().resolve(user_uuid, entity, str(ref).strip())
    if result.match is not None:
        return result.match["id"], None
    if result.candidates:
        return None, _candidates_msg(plural, str(ref), result.candidates)
    return None, f"no {entity} matching '{ref}'."


def _target_cycle(user_uuid: str, ref) -> tuple[Optional[dict], Optional[str]]:
    """the cycle a tool call is about: an explicit ref, else the active
    cycle. returns (cycle-dict, None) or (None, promptable-error)."""
    store = get_store()
    if ref is not None:
        cycle_id, err = _resolve_workspace_id(user_uuid, "cycle", ref,
                                              "cycles")
        if err:
            return None, err
        for row in store.list_cycles(user_uuid, include_closed=True):
            if row["id"] == cycle_id:
                return row, None
        return None, f"no cycle matching '{ref}'."
    row = store.active_cycle(user_uuid)
    if row is None:
        return None, ("no active cycle - create one (create_cycle) and mark "
                      "it Active first, or name the cycle explicitly.")
    return row, None


def _resolve_commitment(user_uuid: str, ref: str):
    """a commitment by public id (cm3) or title. returns (dict, None) or
    (None, promptable-error)."""
    ref = (ref or "").strip()
    if not ref:
        return None, "which commitment? pass a title or id (like cm3)."
    parsed = vocab.parse_public_id(ref)
    rows = _cycles().list_commitments(user_uuid)
    if parsed is not None and parsed[0] == "commitment":
        for row in rows:
            if row["id"] == parsed[1]:
                return row, None
        return None, f"no commitment with id {ref}."
    matches = [r for r in rows if r["title"].lower() == ref.lower()]
    if not matches:
        matches = [r for r in rows if ref.lower() in r["title"].lower()]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, _candidates_msg("commitments", ref, matches)
    return None, f"no commitment matching '{ref}'."


# --- rendering ----------------------------------------------------------------

def _render_projection(view: dict) -> str:
    cycle = view["cycle"]
    head = [f'cycle "{cycle["title"]}" [{vocab.display(cycle["status"])}]']
    if cycle.get("start_date") or cycle.get("end_date"):
        head.append(f'{cycle.get("start_date") or "?"}..'
                    f'{cycle.get("end_date") or "?"}')
    if cycle.get("theme"):
        head.append(f'theme="{cycle["theme"]}"')
    if cycle.get("capacity_blocks") is not None:
        head.append(f'capacity={cycle["capacity_blocks"]} blocks')
    head.append(f'id={cycle["public_id"]}')
    head.append("(baseline frozen)" if view["frozen"]
                else "(not yet frozen - freeze_cycle locks the plan)")
    lines = [" ".join(head)]

    if view["commitments"]:
        lines.append("commitments:")
        for c in view["commitments"]:
            parts = [f'- "{c["title"]}" [{vocab.display(c["status"])}]']
            if c.get("priority"):
                parts.append(f'prio={c["priority"]}')
            planned = c.get("blocks_planned")
            baseline = c.get("baseline_blocks")
            if planned is not None:
                blocks = f"blocks={planned}"
                if baseline is not None and baseline != planned:
                    blocks += f" (baseline {baseline})"
                parts.append(blocks)
            parts.append(f'done={c["blocks_done"]}')
            if c.get("next_action"):
                parts.append(f'next="{c["next_action"]}"')
            if c.get("plan_title"):
                parts.append(f'plan={c["plan_title"]}')
            if c.get("task_title"):
                parts.append(f'task={c["task_title"]}')
            parts.append(f'id={c["public_id"]}')
            lines.append(" ".join(parts))
    else:
        lines.append("no commitments yet.")

    totals = view["totals"]
    total_bits = []
    if totals["capacity_blocks"] is not None:
        total_bits.append(f'planned {totals["planned_blocks"]}'
                          f'/{totals["capacity_blocks"]} blocks')
        total_bits.append(f'unallocated {totals["unallocated_blocks"]}')
    else:
        total_bits.append(f'planned {totals["planned_blocks"]} blocks')
    total_bits.append(f'done {totals["done_blocks"]}')
    if totals["unattributed_seconds"]:
        total_bits.append(
            f'(+{round(totals["unattributed_seconds"] / 60)} min '
            "not tied to any commitment)")
    lines.append("totals: " + ", ".join(total_bits))

    if view["scope_changes"]:
        lines.append(f'scope changes ({len(view["scope_changes"])}):')
        for s in view["scope_changes"][-5:]:
            action = (s.get("deltas") or {}).get("action", "change")
            lines.append(f'- {action}: "{s["reason"]}"')
    return "\n".join(lines)


# --- handlers -------------------------------------------------------------------

async def _view_cycle(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    cycle, err = _target_cycle(user_uuid, tool_input.get("sprint"))
    if err:
        return err
    view = _cycles().projection(user_uuid, cycle["id"])
    return _render_projection(view)


async def _create_commitment(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    cycle, err = _target_cycle(user_uuid, tool_input.get("sprint"))
    if err:
        return err
    plan_id, err = _resolve_workspace_id(
        user_uuid, "plan", tool_input.get("project"), "plans")
    if err:
        return err
    task_id, err = _resolve_workspace_id(
        user_uuid, "task", tool_input.get("task"), "tasks")
    if err:
        return err
    try:
        row = _cycles().create_commitment(
            user_uuid, cycle["id"], tool_input.get("title") or "",
            priority=tool_input.get("priority"),
            blocks_planned=tool_input.get("blocks_planned"),
            next_action=tool_input.get("next_action"),
            plan_id=plan_id, task_id=task_id,
            reason=tool_input.get("reason"))
    except (CycleStoreError, ValueError) as e:
        return str(e)
    return (f'committed "{row["title"]}" to cycle "{cycle["title"]}" '
            f'(id={row["public_id"]}).')


async def _update_commitment(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    target, err = _resolve_commitment(user_uuid,
                                      tool_input.get("commitment") or "")
    if err:
        return err
    changes: dict = {}
    if tool_input.get("new_title"):
        changes["title"] = tool_input["new_title"]
    for key in ("priority", "blocks_planned", "next_action", "status"):
        if key in tool_input and tool_input[key] is not None:
            changes[key] = tool_input[key]
    if tool_input.get("project") is not None:
        plan_id, err = _resolve_workspace_id(
            user_uuid, "plan", tool_input["project"], "plans")
        if err:
            return err
        changes["plan_id"] = plan_id
    if tool_input.get("task") is not None:
        task_id, err = _resolve_workspace_id(
            user_uuid, "task", tool_input["task"], "tasks")
        if err:
            return err
        changes["task_id"] = task_id
    if not changes:
        return "nothing to change - pass at least one field."
    try:
        row = _cycles().update_commitment(user_uuid, target["id"], **changes)
    except (CycleStoreError, ValueError) as e:
        return str(e)
    return (f'updated "{row["title"]}" '
            f'({", ".join(sorted(changes))}) (id={row["public_id"]}).')


async def _freeze_cycle(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    cycle, err = _target_cycle(user_uuid, tool_input.get("sprint"))
    if err:
        return err
    try:
        result = _cycles().freeze_baseline(user_uuid, cycle["id"])
    except CycleStoreError as e:
        return str(e)
    return (f'baseline frozen for cycle "{cycle["title"]}" - '
            f'{result["commitments"]} commitment(s) locked in. changes of '
            "plan from here are scope changes (change_scope), never guilt.")


async def _change_scope(tool_input: dict, context: ToolContext) -> str:
    user_uuid = user_of_context(context)
    reason = tool_input.get("reason") or ""
    commitment_ref = tool_input.get("commitment")
    kwargs: dict = {}
    if commitment_ref:
        target, err = _resolve_commitment(user_uuid, commitment_ref)
        if err:
            return err
        kwargs["commitment_id"] = target["id"]
        if tool_input.get("release"):
            kwargs["release"] = True
        if tool_input.get("blocks_planned") is not None:
            kwargs["blocks_planned"] = tool_input["blocks_planned"]
    elif tool_input.get("capacity_blocks") is not None:
        cycle, err = _target_cycle(user_uuid, tool_input.get("sprint"))
        if err:
            return err
        kwargs["cycle_id"] = cycle["id"]
        kwargs["capacity_blocks"] = tool_input["capacity_blocks"]
    try:
        change = _cycles().change_scope(user_uuid, reason=reason, **kwargs)
    except (CycleStoreError, ValueError) as e:
        return str(e)
    action = (change.get("deltas") or {}).get("action", "change")
    return f'scope change recorded ({action}): "{change["reason"]}".'


# ============================ TOOL DEFS ====================================

def _tool(name, description, properties, handler, *, required=None,
          record_event=True) -> Tool:
    return Tool(
        definition=ToolDef(
            name=name, description=description,
            input_schema={"type": "object", "properties": properties,
                          **({"required": required} if required else {})},
        ),
        # a blank argument is an omitted one (tools/inputs.py)
        handler=blank_tolerant(handler), record_event=record_event,
    )


_SPRINT = {"type": "string",
           "description": "Cycle name or id; defaults to the active cycle."}

VIEW_CYCLE = _tool(
    "view_cycle",
    "The live cycle picture: theme, capacity, every commitment with planned "
    "vs done focus blocks and its next action, scope changes, and whether "
    "the baseline is frozen. Progress comes from real focus sessions on the "
    "user's devices. Use this before planning talk - it is the shared state.",
    {"sprint": _SPRINT},
    _view_cycle, record_event=False,
)

CREATE_COMMITMENT = _tool(
    "create_commitment",
    "Add an explicit commitment to a cycle ('room runtime v1 - 5 blocks - "
    "high'). Link a plan (project) and/or a task so finished focus blocks "
    "attribute their minutes to it. Give it ONE clear next_action - the "
    "activation-energy field. On a frozen cycle this moves planned "
    "capacity, so 'reason' (the user's words) is required and it is "
    "recorded as a scope change.",
    {
        "title": {"type": "string", "description": "The promise, short."},
        "sprint": _SPRINT,
        "priority": {"type": "string", "enum": _PRIORITY},
        "blocks_planned": {"type": "integer",
                           "description": "Focus blocks (25 min) allocated."},
        "next_action": {"type": "string",
                        "description": "The one clear next step."},
        "project": {"type": "string", "description": "Plan name or id to anchor to."},
        "task": {"type": "string", "description": "Task name or id to anchor to."},
        "reason": {"type": "string",
                   "description": "Why (required only on a frozen cycle)."},
    },
    _create_commitment, required=["title"],
)

UPDATE_COMMITMENT = _tool(
    "update_commitment",
    "Update a commitment (identify by title or id via 'commitment'). "
    "Renaming, re-prioritizing, setting the next_action, re-linking, and "
    "completing (status='Completed') are always ordinary updates. On a "
    "frozen cycle, resizing blocks or releasing must go through "
    "change_scope instead - this tool will say so.",
    {
        "commitment": {"type": "string",
                       "description": "Title or id (like cm3)."},
        "new_title": {"type": "string"},
        "priority": {"type": "string", "enum": _PRIORITY},
        "blocks_planned": {"type": "integer"},
        "next_action": {"type": "string",
                        "description": "The one clear next step."},
        "status": {"type": "string", "enum": _COMMITMENT_STATUS},
        "project": {"type": "string", "description": "Plan name or id."},
        "task": {"type": "string", "description": "Task name or id."},
    },
    _update_commitment, required=["commitment"],
)

FREEZE_CYCLE = _tool(
    "freeze_cycle",
    "Freeze the cycle's baseline: an immutable snapshot of the accepted "
    "commitments and allocations. Do this once, when the user has agreed to "
    "the plan. Afterwards the plan can still change - but through explicit, "
    "reasoned scope changes, so the history stays honest.",
    {"sprint": _SPRINT},
    _freeze_cycle,
)

CHANGE_SCOPE = _tool(
    "change_scope",
    "The sanctioned way plans move after the freeze - guilt-free but "
    "explicit. Resize a commitment (commitment + blocks_planned), release "
    "one (commitment + release=true), or change the cycle's own capacity "
    "(capacity_blocks). 'reason' is the user's words and is required.",
    {
        "reason": {"type": "string",
                   "description": "Why the plan is changing, user's words."},
        "commitment": {"type": "string",
                       "description": "Title or id of the commitment moving."},
        "blocks_planned": {"type": "integer",
                           "description": "New block allocation (resize)."},
        "release": {"type": "boolean",
                    "description": "Release the commitment (drop it, kept in history)."},
        "capacity_blocks": {"type": "integer",
                            "description": "New cycle capacity (cycle-level change)."},
        "sprint": _SPRINT,
    },
    _change_scope, required=["reason"],
)

CYCLE_SPINE_TOOLS = [VIEW_CYCLE, CREATE_COMMITMENT, UPDATE_COMMITMENT,
                     FREEZE_CYCLE, CHANGE_SCOPE]
