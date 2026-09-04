"""the live workspace agenda: digest + payload straight from the store.

queries are local microseconds, so there's no cache, ttl, or background
refresh - just two live reads:

- get_digest(user_uuid)  -> the compact promptable "today picture" (digest
  v2: buckets, cycle + focus, plans by helper, wins this week, occasions
  within 3 days). rides in the VOLATILE prompt zone, so building it live
  has zero prompt-cache impact.
- get_payload(user_uuid) -> the structured buckets the completion
  reconciler reads. task ids are public ids (t42), which the reconciler
  round-trips into update_task - the native resolver parses them.

timezone semantics: "today" is the user-local calendar date, and
task.scheduled is a plain user-local date.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from src.database.database import get_db
from src.database.models import User
from src.services.workspace import get_store, vocab
from src.utils.timezone_utils import utc_now, to_user_timezone

logger = logging.getLogger(__name__)

# per-section caps so the digest stays ~150-400 tokens regardless of backlog
# (unchanged from the snapshot service)
_MAX_TODAY = 8
_MAX_OVERDUE = 6
_MAX_IN_PROGRESS = 5
_MAX_PLANS = 6
_MAX_OCCASIONS = 3
_OCCASION_HORIZON_DAYS = 3

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]


def _pretty_date(iso: str) -> str:
    """'2026-07-14' -> 'jul 14'. returns the input unchanged if unparseable."""
    try:
        y, m, d = iso[:10].split("-")
        return f"{_MONTHS[int(m) - 1]} {int(d)}"
    except (ValueError, IndexError):
        return iso


def user_today(user_uuid: str) -> date:
    """the user's local calendar date - the reference point for every
    scheduled/occasion comparison. shared with the workspace tools."""
    with get_db() as db:
        user = db.query(User).filter(User.uuid == user_uuid).first()
        tz = user.timezone if user and user.timezone else "UTC"
    return to_user_timezone(utc_now(), tz).date()


class WorkspaceAgenda:
    """digest + payload reads for the chat path and the reconciler."""

    def __init__(self):
        self.store = get_store()

    # --- payload (the reconciler's view) ------------------------------------

    def get_payload(self, user_uuid: str) -> Optional[dict]:
        """the structured buckets, live. same keys and row shape the
        snapshot payload had (id/title/status/priority/scheduled/project/
        cycle/pom) plus window/helper; `id` is the public id (t42) so the
        reconciler can echo it straight into update_task."""
        today = user_today(user_uuid)
        today_iso = today.isoformat()

        tasks = self.store.list_tasks(user_uuid)   # open only, scheduled order
        tasks_today, tasks_overdue, tasks_in_progress = [], [], []
        tasks_set_aside = []
        for t in tasks:
            sched = t["scheduled"]
            row = self._payload_row(t)
            if t.get("set_aside_on") == today_iso:
                # consciously parked for today (FOCUS_DOGFOOD_DESIGN section
                # 2): out of the live buckets so nobody nudges about it
                tasks_set_aside.append(row)
            elif sched == today_iso:
                tasks_today.append(row)
            elif sched and sched < today_iso:
                tasks_overdue.append(row)
            elif t["status"] == "in_progress":
                # started but not dated for today - active context (the same
                # "else" bucket the snapshot's agenda filter produced)
                tasks_in_progress.append(row)

        active = self.store.active_cycle(user_uuid)
        return {
            "cycle": self._cycle_row(active) if active else None,
            "plans": self.store.list_plans(user_uuid, status="active"),
            "tasks_today": tasks_today,
            "tasks_overdue": tasks_overdue,
            "tasks_in_progress": tasks_in_progress,
            "tasks_set_aside": tasks_set_aside,
            "done_today": [],   # kept for shape-compat; the wins ledger owns this now
        }

    @staticmethod
    def _payload_row(t: dict) -> dict:
        return {
            "id": t["public_id"],
            "title": t["title"],
            "status": vocab.display(t["status"]),
            "priority": t["priority"],
            "scheduled": t["scheduled"],
            "project": t["plan_title"],       # legacy key name, plan title
            "cycle": t["cycle_title"],
            "pom": t["pom_estimate"],
            "window": t["window"],
            "helper": t["helper"],
            "next_action": t.get("next_action"),
        }

    @staticmethod
    def _cycle_row(c: dict) -> dict:
        dates = ""
        if c["start_date"] or c["end_date"]:
            dates = f'{c["start_date"] or ""}→{c["end_date"] or ""}'
        return {"id": c["public_id"], "title": c["title"], "dates": dates,
                "goal": c["goal"], "focus": c["focus"]}

    # --- digest v2 (the promptable today picture) ---------------------------

    def get_digest(self, user_uuid: str) -> Optional[str]:
        today = user_today(user_uuid)
        payload = self.get_payload(user_uuid)
        t, o, p = (payload["tasks_today"], payload["tasks_overdue"],
                   payload["tasks_in_progress"])
        parked = payload["tasks_set_aside"]
        cycle = payload["cycle"]
        plans = payload["plans"]
        wins_week = len(self.store.list_wins(
            user_uuid, since=(today - timedelta(days=6)).isoformat()))
        occasions = self.store.list_occasions(
            user_uuid, today=today.isoformat(),
            until=(today + timedelta(days=_OCCASION_HORIZON_DAYS)).isoformat())

        if not (t or o or p or parked or cycle or plans or wins_week or occasions):
            # a genuinely empty workspace (e.g. a brand-new user): no agenda
            # note at all rather than an empty-state line on every turn
            return None

        lines = ["workspace agenda (background awareness - the user hasn't seen this):"]

        if cycle:
            bits = [f'cycle: "{cycle["title"]}"']
            if "→" in (cycle["dates"] or ""):
                bits.append(f"ends {_pretty_date(cycle['dates'].split('→')[1])}")
            if cycle.get("focus"):
                bits.append(f"- focus: {cycle['focus']}")
            elif cycle.get("goal"):
                bits.append(f"- goal: {cycle['goal']}")
            lines.append(" ".join(bits))

        if t:
            lines.append(f"today ({len(t)}): " + self._join(t, _MAX_TODAY, self._fmt_today))
        if o:
            lines.append(f"overdue ({len(o)}): " + self._join(o, _MAX_OVERDUE, self._fmt_overdue))
        if p:
            lines.append(f"also in progress ({len(p)}): "
                         + self._join(p, _MAX_IN_PROGRESS, self._fmt_in_progress))
        elif not (t or o) and (cycle or plans):
            lines.append("nothing scheduled today, nothing overdue.")
        if parked:
            # the person parked these on purpose - name them so nobody
            # nudges, and don't count them under today
            lines.append("set aside today (their call, don't nudge): "
                         + ", ".join(f'"{r["title"]}"' for r in parked[:_MAX_TODAY]))

        if plans:
            by_helper: dict[str, list[str]] = {}
            for plan in plans:
                by_helper.setdefault(plan["helper"], []).append(f'"{plan["title"]}"')
            shown = list(by_helper.items())[:_MAX_PLANS]
            lines.append("active plans: " + " / ".join(
                f"{helper}: {', '.join(titles)}" for helper, titles in shown))

        if wins_week:
            lines.append(f"wins this week: {wins_week}")

        if occasions:
            lines.append("coming up: " + " / ".join(
                self._fmt_occasion(oc) for oc in occasions[:_MAX_OCCASIONS]))

        return "\n".join(lines)

    @staticmethod
    def _join(rows, cap, fmt) -> str:
        shown = " / ".join(fmt(r) for r in rows[:cap])
        extra = len(rows) - cap
        return f"{shown} …and {extra} more" if extra > 0 else shown

    @staticmethod
    def _fmt_today(row) -> str:
        meta = [m for m in (row.get("status"), row.get("priority")) if m]
        base = f'"{row.get("title", "")}"'
        if meta:
            base += f" [{', '.join(meta)}]"
        if row.get("next_action"):
            base += f" - next: {row['next_action']}"   # the scope, if they set one
        if row.get("window"):
            base += f" ({row['window']})"    # the day's window layout, inline
        return base

    @staticmethod
    def _fmt_overdue(row) -> str:
        base = f'"{row.get("title", "")}"'
        sched = row.get("scheduled")
        return f"{base} (was {_pretty_date(sched)})" if sched else base

    @staticmethod
    def _fmt_in_progress(row) -> str:
        base = f'"{row.get("title", "")}"'
        proj = row.get("project")
        return f"{base} (plan: {proj})" if proj else base

    @staticmethod
    def _fmt_occasion(oc: dict) -> str:
        base = f'"{oc["title"]}" {_pretty_date(oc["date"])}'
        if oc.get("time"):
            base += f" {oc['time']}"
        return base
