"""focus, owned locally (docs/ROOMS_DESIGN.md section 5) - the task-clock
model: one task's clock runs at a time, and time BANKS.

a *run* is one stretch of attention on one task. pausing ends the run and
banks its seconds; switching tasks pauses the old run and starts a new one;
nothing here is ever "abandoned" - stopping is information the bank keeps.
the block target (default 25 minutes) is a rhythm, not a wall: when the
clock crosses it the deer says so ONCE and the run keeps counting overtime
until the person lands it themselves. finishing means the TASK is done -
the celebration moment - and emits focus_block.completed for the server
(pip starts reading these in phase 5).

the clock is wall-time arithmetic on stored timestamps, so it works
offline and a sidecar restart resumes mid-run exactly where reality is.
per-task banked totals reset at the machine's local midnight - the sidecar
is on-device; the device's day IS the person's day.

rewind (docs/REWIND_DESIGN.md section 6) threads through here as three
facts the engine owns:
- CREDITED time is wall time minus the run's applied excisions; it is
  what state() shows, what banks, and what the target ding celebrates.
- a run with an UNRESOLVED correction cannot silently bank: pause /
  finish / switch FREEZE it (frozen_at stops the clock, unbanked) and the
  question stays open; resolution banks it at its freeze instant.
- the ding fires on UNCONTESTED credited time - an open question's quiet
  span never inflates the clock into a false celebration - and an applied
  correction that drops credited time back under the target re-arms it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from src.sidecar.store import SidecarStore

DEFAULT_TARGET_MINUTES = 25.0
MAX_TARGET_MINUTES = 120.0
MIN_TARGET_MINUTES = 0.05     # tiny targets stay possible for dev smoke runs
_LABEL_CAP = 200


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


class FocusError(ValueError):
    """a request the focus state can't honor."""


class FocusEngine:
    """the one-running-clock state machine. `clock` is injectable so tests
    move time instead of sleeping through it."""

    def __init__(self, store: SidecarStore,
                 clock: Callable[[], datetime] = _utc_now):
        self.store = store
        self.clock = clock
        # run ids whose target-crossing line already fired (in-memory: a
        # restart may re-announce once, which is gentler than never)
        self._announced: set[int] = set()

    # --- reads ---------------------------------------------------------------

    def _day_start_iso(self) -> str:
        """local midnight, as utc - banked bars are a per-day story and the
        device's day is the person's day."""
        local = self.clock().astimezone()
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc).isoformat()

    def _credited_seconds(self, run: dict) -> int:
        """wall time minus applied excisions, up to the freeze instant for
        a frozen run - THE number: what state() shows and what banks."""
        end = _parse(run["frozen_at"]) if run.get("frozen_at") else self.clock()
        gross = max(0, int((end - _parse(run["started_at"])).total_seconds()))
        return max(0, gross - self.store.excised_seconds(run["id"]))

    def state(self) -> dict:
        """the deer window's whole world: the running clock (if any),
        today's banked seconds per task, and any frozen run still waiting
        on its correction."""
        banked = self.store.banked_since(self._day_start_iso())
        frozen = [{
            "run_id": run["id"],
            "task_id": run["task_id"],
            "label": run["label"],
            "frozen_at": run["frozen_at"],
            "frozen_reason": run["frozen_reason"],
            "run_seconds": self._credited_seconds(run),
        } for run in self.store.frozen_runs()]
        active = self.store.active_run()
        if active is None:
            return {"running": False, "banked": banked, "frozen": frozen}
        run_seconds = self._credited_seconds(active)
        target_seconds = int(active["target_minutes"] * 60)
        return {
            "running": True,
            "run_id": active["id"],
            "task_id": active["task_id"],
            "label": active["label"],
            "target_minutes": active["target_minutes"],
            "started_at": active["started_at"],
            "run_seconds": run_seconds,
            "excised_seconds": self.store.excised_seconds(active["id"]),
            "over_target": run_seconds >= target_seconds,
            "banked": banked,
            "frozen": frozen,
        }

    # --- transitions -----------------------------------------------------------

    def start(self, task_id: Optional[int] = None,
              label: Optional[str] = None,
              target_minutes: float = DEFAULT_TARGET_MINUTES) -> dict:
        """run the clock on a task. if another task's clock is running this
        IS the switch: the old run pauses (banks) first. starting the task
        that's already running is a no-op question, not an error."""
        if not isinstance(target_minutes, (int, float)) \
                or isinstance(target_minutes, bool) \
                or not (MIN_TARGET_MINUTES <= target_minutes
                        <= MAX_TARGET_MINUTES):
            raise FocusError(
                f"target_minutes must be between {MIN_TARGET_MINUTES} "
                f"and {MAX_TARGET_MINUTES}")
        if task_id is not None and (not isinstance(task_id, int)
                                    or isinstance(task_id, bool)
                                    or task_id < 1):
            raise FocusError("task_id must be a positive integer")
        label = (label or "").strip()[:_LABEL_CAP] or None

        active = self.store.active_run()
        if active is not None:
            if active["task_id"] == task_id and task_id is not None:
                raise FocusError("that task's clock is already running")
            if self.store.open_offer_for_run(active["id"]) is not None:
                # an unresolved correction: the old run freezes unbanked
                # (the question persists) and the new block starts anyway
                self._freeze(active, "switched")
            else:
                self._end_run(active, "switched")

        now = self.clock()
        self.store.insert_run(task_id, label, float(target_minutes),
                              now.isoformat())
        # target_minutes rides along so the server's day digest can say
        # "18 min in, target 25" (docs/FOCUS_DOGFOOD_DESIGN.md section 3)
        self.store.enqueue("session.started",
                           {"task_id": task_id, "label": label,
                            "target_minutes": float(target_minutes)},
                           occurred_at=now.isoformat())
        return self.state()

    def pause(self) -> Optional[dict]:
        """stop the clock. with no open correction the run banks and its
        summary returns; with one, the run FREEZES unbanked (the clock
        stops either way - a safety net that leaves a runaway timer
        running would be the disease pretending to be the cure) and the
        summary says so. None when nothing was running."""
        active = self.store.active_run()
        if active is None:
            return None
        if self.store.open_offer_for_run(active["id"]) is not None:
            return self._freeze(active, "paused")
        return self._end_run(active, "paused")

    def finish(self) -> dict:
        """the task is DONE - the celebration transition. banks the running
        run and emits focus_block.completed with today's whole banked total
        for the task. requires a running clock (finishing is an act of
        landing, not of tidying). an open correction freezes here too -
        the celebration waits one answer."""
        active = self.store.active_run()
        if active is None:
            raise FocusError("no clock is running")
        if self.store.open_offer_for_run(active["id"]) is not None:
            return self._freeze(active, "finished")
        summary = self._end_run(active, "finished")
        self._emit_completed(active, summary)
        return summary

    def close_frozen(self, run_id: int) -> dict:
        """bank a frozen run once its correction resolved: the run ends at
        its FREEZE instant (a late answer never inflates it), with
        credited seconds. emits the events its intended transition owed."""
        run = self.store.run(run_id)
        if run is None or run["ended_at"] is not None \
                or not run.get("frozen_at"):
            raise FocusError("no frozen run to close")
        seconds = self._credited_seconds(run)
        self.store.close_run(run_id, run["frozen_at"], seconds,
                             run["frozen_reason"] or "paused")
        self._announced.discard(run_id)
        summary = {"task_id": run["task_id"], "label": run["label"],
                   "seconds": seconds,
                   "reason": run["frozen_reason"] or "paused",
                   "ended_at": run["frozen_at"]}
        self._emit_ended(run, summary)
        if run["frozen_reason"] == "finished":
            self._emit_completed(run, summary)
        return summary

    def _freeze(self, active: dict, reason: str) -> dict:
        now = self.clock()
        self.store.freeze_run(active["id"], now.isoformat(), reason)
        # the server must see the clock STOP: nothing banks until the
        # correction resolves, but a session.started with no transition
        # after it reads as a running clock (the day digest would report
        # mid-block for hours). the ended event follows from close_frozen
        self.store.enqueue(
            "session.frozen",
            {"task_id": active["task_id"], "label": active["label"],
             "reason": reason},
            occurred_at=now.isoformat())
        return {"frozen": True, "run_id": active["id"],
                "task_id": active["task_id"], "label": active["label"],
                "reason": reason, "frozen_at": now.isoformat()}

    def _emit_ended(self, run: dict, summary: dict) -> None:
        """session.ended carries both truths: `seconds` is credited (what
        banks - attribution reads this untouched), gross and excised ride
        alongside so nothing is laundered."""
        end = _parse(summary["ended_at"])
        gross = max(0, int((end - _parse(run["started_at"])).total_seconds()))
        self.store.enqueue(
            "session.ended",
            {"task_id": run["task_id"], "label": run["label"],
             "seconds": summary["seconds"], "reason": summary["reason"],
             "gross_seconds": gross,
             "excised_seconds": self.store.excised_seconds(run["id"])},
            occurred_at=summary["ended_at"])

    def _emit_completed(self, run: dict, summary: dict) -> None:
        banked = self.store.banked_since(self._day_start_iso())
        self.store.enqueue(
            "focus_block.completed",
            {"task_id": run["task_id"], "label": run["label"],
             "run_seconds": summary["seconds"],
             "banked_seconds_today": banked.get(run["task_id"] or 0, 0)},
            occurred_at=summary["ended_at"])

    def crossed_target(self) -> bool:
        """True exactly once per run, the moment UNCONTESTED credited time
        crosses the target - the ding, without ending anything. an open
        correction's quiet span is subtracted first, so contested minutes
        never ding; keeping them later lets the crossing fire honestly."""
        active = self.store.active_run()
        if active is None or active["id"] in self._announced:
            return False
        uncontested = self._credited_seconds(active)
        offer = self.store.open_offer_for_run(active["id"])
        if offer is not None and offer["candidates"]:
            # contested = every closed segment plus the current episode's
            # quiet [boundary, return-or-now]: clamped at the return, so
            # fresh work with the chip still open keeps counting toward
            # an honest ding
            for seg_start, seg_end in offer.get("segments", []):
                uncontested -= max(0, int(
                    (_parse(seg_end) - _parse(seg_start)).total_seconds()))
            boundary = _parse(offer["candidates"][0]["at"])
            end = _parse(offer["returned_at"]) if offer.get("returned_at") \
                else self.clock()
            uncontested -= max(0, int((end - boundary).total_seconds()))
        if uncontested >= active["target_minutes"] * 60:
            self._announced.add(active["id"])
            return True
        return False

    def rearm_after_excision(self, run_id: int) -> None:
        """an applied correction that drops credited time back under the
        target re-arms the ding - it celebrates credited time, and the
        clock hasn't honestly reached it anymore."""
        run = self.store.run(run_id)
        if run is None or run["ended_at"] is not None:
            return
        if self._credited_seconds(run) < run["target_minutes"] * 60:
            self._announced.discard(run_id)

    def suppress_ding(self, run_id: int) -> None:
        """undo restoring credited time past the target must never fire a
        celebratory ding on the way back up - mark it announced, silently."""
        run = self.store.run(run_id)
        if run is None or run["ended_at"] is not None:
            return
        if self._credited_seconds(run) >= run["target_minutes"] * 60:
            self._announced.add(run_id)

    def _end_run(self, active: dict, reason: str) -> dict:
        now = self.clock()
        seconds = self._credited_seconds(active)
        self.store.close_run(active["id"], now.isoformat(), seconds, reason)
        self._announced.discard(active["id"])
        summary = {"task_id": active["task_id"], "label": active["label"],
                   "seconds": seconds, "reason": reason,
                   "ended_at": now.isoformat()}
        self._emit_ended(active, summary)
        return summary
