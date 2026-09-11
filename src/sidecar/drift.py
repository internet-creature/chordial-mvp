"""activity + drift: what the collectors report, and the one gentle
check-in it earns (docs/ROOMS_DESIGN.md §5).

the rust collector posts {bundle_id, idle_seconds} every couple of seconds.
this module keeps the latest sample and watches for exactly one pattern in
v1: the person going quiet mid-block (idle past the threshold - ten
minutes, the design's number) and coming back. drift earns AT MOST one
soft check-in per episode, the return earns one welcome, and an unanswered
check-in is silently assumed to be a break - never re-asked, never scored.
app-based drift (a distracting bundle id while working) waits for
user-declared app->task maps in a later phase.

facts (drift.detected / return.detected) always land in the outbox; the
LINES ride the gate stack. a blocklisted frontmost app (meetings) hushes
everything without recording anything extra.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.sidecar.gates import blocklist
from src.sidecar.store import SidecarStore

# ten minutes adrift earns at most one soft check-in - the AUTHORITATIVE
# number from docs/ROOMS_DESIGN.md section 5; change the doc first
DRIFT_IDLE_MINUTES = float(os.getenv("SIDECAR_DRIFT_IDLE_MINUTES", "10"))
# activity within this many seconds counts as "back at the desk"
_RETURN_IDLE_SECONDS = 10.0
# a collector gone quiet this long means signals are stale, not the person
_SAMPLE_STALE_SECONDS = 30.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ActivityState:
    """the latest collector sample, with honesty about staleness."""

    def __init__(self, clock: Callable[[], datetime] = _utc_now):
        self.clock = clock
        self._bundle_id: Optional[str] = None
        self._idle_seconds: float = 0.0
        self._at: Optional[datetime] = None
        self._blocklist = blocklist()

    def observe(self, bundle_id: Optional[str], idle_seconds: float) -> None:
        self._bundle_id = bundle_id
        self._idle_seconds = max(0.0, float(idle_seconds))
        self._at = self.clock()

    @property
    def sample(self) -> Optional[tuple[datetime, float]]:
        """the latest collector sample as REPORTED (when, idle seconds) -
        the attention seam reads renewals from consecutive samples, never
        from the computed idle that keeps climbing on its own."""
        if self._at is None:
            return None
        return (self._at, self._idle_seconds)

    @property
    def fresh(self) -> bool:
        return (self._at is not None and
                (self.clock() - self._at).total_seconds()
                < _SAMPLE_STALE_SECONDS)

    @property
    def idle_seconds(self) -> float:
        """idle as of NOW: the last sample plus the time since it arrived
        (idleness keeps accruing between heartbeats)."""
        if self._at is None:
            return 0.0
        return (self._idle_seconds
                + (self.clock() - self._at).total_seconds())

    @property
    def blocked(self) -> bool:
        """a meeting-shaped app is frontmost - the deer hushes entirely.
        only trusted while fresh; a dead collector must not mute forever."""
        return (self.fresh and self._bundle_id is not None
                and self._bundle_id in self._blocklist)

    def snapshot(self) -> dict:
        """what the deer window may see: derived flags only, never the
        bundle id itself - the ui needs moods, not surveillance."""
        return {
            "fresh": self.fresh,
            "idle_seconds": int(self.idle_seconds) if self.fresh else 0,
            "blocked": self.blocked,
        }


class DriftWatch:
    """the drift episode machine: armed only while a block runs."""

    def __init__(self, store: SidecarStore, activity: ActivityState,
                 clock: Callable[[], datetime] = _utc_now,
                 threshold_minutes: float = DRIFT_IDLE_MINUTES):
        self.store = store
        self.activity = activity
        self.clock = clock
        # the drift threshold must sit comfortably above the return
        # threshold or an idle value between them would fire both moments
        # back to back; the floor keeps even a pathological config sane
        self.threshold_seconds = max(threshold_minutes * 60,
                                     _RETURN_IDLE_SECONDS * 2)
        self._in_drift = False
        self._drift_started: Optional[datetime] = None

    @property
    def drifting(self) -> bool:
        return self._in_drift

    def hydrate_in_drift(self) -> None:
        """a sidecar restart mid-episode (an open rewind offer proves it):
        resume the drift already in progress so the check-in isn't
        re-asked; the return will still fire normally."""
        self._in_drift = True
        self._drift_started = None

    def tick(self, run_active: bool) -> Optional[str]:
        """called every second by the server loop. returns the line moment
        to (maybe) speak: 'drift_checkin' once at the start of an episode,
        'drift_return' once when the person comes back. facts enqueue here
        regardless of whether the line survives the gates."""
        if not run_active or not self.activity.fresh:
            self._in_drift = False
            self._drift_started = None
            return None

        idle = self.activity.idle_seconds
        now = self.clock()

        if not self._in_drift and idle >= self.threshold_seconds:
            self._in_drift = True
            self._drift_started = now - timedelta(seconds=idle)
            self.store.enqueue("drift.detected",
                               {"idle_seconds": int(idle)},
                               occurred_at=now.isoformat())
            return "drift_checkin"

        if self._in_drift and idle < _RETURN_IDLE_SECONDS:
            away = int((now - self._drift_started).total_seconds()
                       ) if self._drift_started else 0
            self._in_drift = False
            self._drift_started = None
            self.store.enqueue("return.detected",
                               {"away_seconds": max(0, away)},
                               occurred_at=now.isoformat())
            return "drift_return"

        return None
