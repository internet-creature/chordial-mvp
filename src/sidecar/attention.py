"""attention: away, and the one return stuck mode needs
(docs/STUCK_MODE_DESIGN.md section 4.1).

the drift detector is the wrong primitive for the return bridge: it is
armed only by a running block, and a body proposal usually pauses that
block. this is the seam beneath both: `AttentionState` turns the signals
the sidecar already has (input idle + sample freshness, an explicit away
intent, the surface being looked at) into legible transitions -
present / possibly_away / away / returned - with hysteresis so a mouse
twitch never manufactures a return. `AwayEpisodeWatch` is stuck mode's
consumer: a durable away episode (the step waiting underneath), the
departure, the ONE return event and the re-offer. `DriftWatch` stays a
consumer of its own for running blocks.

rules, in v0: RENEWED input (or the surface coming back into view) is a
return - renewed means the collector's reported idle reset between two
samples, held across a debounce; the computed idle climbing on its own
after one sample is not input (sol, #93). during an explicit away episode
sustained idle establishes departure quickly; ordinary idle with no
episode and no block manufactures nothing; stale samples move nothing.
screen lock/wake and opt-in app categories plug into the same machine
later.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.sidecar.drift import ActivityState
from src.sidecar.store import SidecarStore

# sustained idle during an EXPLICIT away episode = they left (the drift
# threshold, ten minutes, is for a running block - here they said they'd go)
AWAY_IDLE_SECONDS = float(os.getenv("SIDECAR_AWAY_IDLE_SECONDS", "90"))
# activity within this many seconds counts as "at the desk" (drift's number)
RETURN_IDLE_SECONDS = 10.0
# fresh input must HOLD this long before it is a return - a twitch isn't one
RETURN_DEBOUNCE_SECONDS = float(os.getenv("SIDECAR_RETURN_DEBOUNCE_SECONDS", "3"))
# the surface coming into view counts as input for this long
SURFACE_RECENT_SECONDS = 5.0
# an away episode nobody came back to closes on its own
AWAY_TTL_HOURS = float(os.getenv("SIDECAR_AWAY_TTL_HOURS", "3"))

PRESENT = "present"
POSSIBLY_AWAY = "possibly_away"
AWAY = "away"
RETURNED = "returned"

MOMENT_RETURN = "stuck_return"
# how a waiting step resolves: the choice -> the closed_reason
RESOLUTIONS = {"start": "started", "not_now": "declined", "back": "back"}
DEFAULT_STEP_MINUTES = 2.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AttentionState:
    """the machine. `assess` is called every tick with what the world
    says (an explicit away intent, a running block) and answers with a
    transition when one happens: "away" or "returned"; None otherwise."""

    def __init__(self, activity: ActivityState,
                 clock: Callable[[], datetime] = _utc_now):
        self.activity = activity
        self.clock = clock
        self.phase = PRESENT
        self._input_since: Optional[datetime] = None
        self._surface_seen: Optional[datetime] = None
        self._prev_sample: Optional[tuple[datetime, float]] = None

    def observe_surface(self, visible: bool) -> None:
        """the person looked at (or away from) a chordial surface. coming
        into view is evidence of presence as good as a keystroke."""
        if visible:
            self._surface_seen = self.clock()

    def hydrate_away(self) -> None:
        """a restart mid-episode: the durable row says they left, so the
        machine starts there and the return can still fire normally."""
        self.phase = AWAY
        self._input_since = None

    def _surface_recent(self, now: datetime) -> bool:
        return (self._surface_seen is not None
                and (now - self._surface_seen).total_seconds() <= SURFACE_RECENT_SECONDS)

    def _renewed(self, sample: Optional[tuple[datetime, float]]) -> tuple[bool, bool]:
        """(new_sample, renewed): did a NEW collector sample arrive, and
        did it show new input since the previous one? the reported idle
        resets on input; otherwise it grows by exactly the gap between
        samples. one sample after a restart counts when it says the desk
        is live. the same sample seen again on the next tick is neither."""
        prev = self._prev_sample
        if sample is None:
            return False, False
        at, idle = sample
        if prev is not None and at <= prev[0]:
            return False, False     # the same sample, seen again
        if idle >= RETURN_IDLE_SECONDS:
            return True, False
        if prev is None:
            return True, True
        expected = prev[1] + (at - prev[0]).total_seconds()
        return True, idle < expected - 0.5

    def assess(self, explicit_away: bool, run_active: bool) -> Optional[str]:
        now = self.clock()
        surface = self._surface_recent(now)
        sample = self.activity.sample
        if not self.activity.fresh and not surface:
            # a dead collector says nothing about the person
            return None
        idle = 0.0 if surface else self.activity.idle_seconds
        new_sample, renewed = self._renewed(sample)
        self._prev_sample = sample

        if self.phase in (PRESENT, POSSIBLY_AWAY):
            self._input_since = None
            if not explicit_away:
                # no intent to leave: a running block's idleness is drift's
                # business, an idle desk with nothing going on is nobody's
                self.phase = PRESENT
                return None
            if idle >= AWAY_IDLE_SECONDS:
                self.phase = AWAY
                return AWAY
            self.phase = POSSIBLY_AWAY if idle >= AWAY_IDLE_SECONDS / 2 else PRESENT
            return None

        # AWAY: only RENEWED input (or the surface) brings them back, and
        # renewals must span the debounce - one twitch opens a window that
        # only a second renewal can close
        if surface:
            self.phase = PRESENT
            self._input_since = None
            return RETURNED
        if idle >= RETURN_IDLE_SECONDS:
            self._input_since = None    # the twitch ended; still away
            return None
        if not renewed:
            if new_sample:
                # a fresh sample with no input behind it: whatever window
                # a twitch opened is closed - typing renews every sample
                self._input_since = None
            return None
        if self._input_since is None:
            self._input_since = sample[0]
            return None
        if (sample[0] - self._input_since).total_seconds() >= RETURN_DEBOUNCE_SECONDS:
            self.phase = PRESENT
            self._input_since = None
            return RETURNED
        return None


class AwayEpisodeWatch:
    """stuck mode's away episode: durable, one at a time, the step
    waiting underneath. `tick` runs every second beside the drift watch."""

    def __init__(self, store: SidecarStore, attention: AttentionState,
                 clock: Callable[[], datetime] = _utc_now):
        self.store = store
        self.attention = attention
        self.clock = clock
        current = self.store.open_away()
        if current and current.get("departed_at") and not current.get("returned_at"):
            self.attention.hydrate_away()

    # --- opening and closing ----------------------------------------------------

    def open(self, execution: dict, *, task_id: Optional[int], label: Optional[str],
             next_action: Optional[str], minutes: Optional[float],
             step_minutes: Optional[float] = None) -> tuple[dict, bool]:
        """(episode, created). dedupes on execution_id: the same accepted
        proposal opens one episode. any other open episode is superseded."""
        now = self.clock()
        prior = self.store.away_by_execution(execution["execution_id"])
        if prior is not None:
            if prior["closed_at"] is None:
                return self._payload(prior), False
            raise ValueError("that step's away time already happened")
        current = self.store.open_away()
        with self.store.transaction():
            if current is not None:
                self.store.close_away(current["id"], now.isoformat(), "superseded")
            row_id = self.store.insert_away(
                execution_id=execution["execution_id"],
                episode_id=execution["episode_id"],
                task_id=task_id, label=label, next_action=next_action,
                minutes=float(minutes) if minutes else None,
                step_minutes=float(step_minutes) if step_minutes else None,
                opened_at=now.isoformat())
            self.store.enqueue("attention.away", {
                "episode_id": execution["episode_id"],
                "execution_id": execution["execution_id"],
                "task_id": task_id, "minutes": minutes,
            }, occurred_at=now.isoformat())
        self.attention.phase = PRESENT
        return self._payload(self.store.away(row_id)), True

    def resolve(self, choice: str) -> Optional[dict]:
        """the person answered the re-offer (or the card): "start" closes
        as started (the caller starts the run, in the same transaction),
        "not_now" as declined, "back" as back with nothing to start."""
        current = self.store.open_away()
        if current is None:
            return None
        reason = RESOLUTIONS.get(choice)
        if reason is None:
            raise ValueError("choice must be one of " + ", ".join(RESOLUTIONS))
        self.store.close_away(current["id"], self.clock().isoformat(), reason)
        self.attention.phase = PRESENT
        return current

    def supersede(self, reason: str = "superseded") -> None:
        """another run started, or a new press: the waiting step is no
        longer the next thing."""
        current = self.store.open_away()
        if current is not None:
            self.store.close_away(current["id"], self.clock().isoformat(), reason)
            self.attention.phase = PRESENT

    # --- the tick -----------------------------------------------------------------

    def tick(self, run_active: bool) -> Optional[str]:
        """returns the line moment to (maybe) speak: MOMENT_RETURN once,
        when a real return follows a real departure. the fact enqueues
        here regardless of whether the line survives the gates."""
        current = self.store.open_away()
        if current is None:
            self.attention.assess(explicit_away=False, run_active=run_active)
            return None
        now = self.clock()
        opened = _parse(current["opened_at"])
        if now - opened > timedelta(hours=AWAY_TTL_HOURS):
            self.store.close_away(current["id"], now.isoformat(), "expired")
            self.attention.phase = PRESENT
            return None
        if current.get("returned_at"):
            # back, re-offered, undecided: nothing more to detect
            self.attention.assess(explicit_away=False, run_active=run_active)
            return None
        transition = self.attention.assess(explicit_away=True, run_active=run_active)
        if transition == AWAY and not current.get("departed_at"):
            self.store.update_away(current["id"], departed_at=now.isoformat())
        elif transition == RETURNED and current.get("departed_at"):
            away = int((now - _parse(current["departed_at"])).total_seconds())
            with self.store.transaction():
                self.store.update_away(current["id"], returned_at=now.isoformat())
                self.store.enqueue("attention.returned", {
                    "episode_id": current["episode_id"],
                    "execution_id": current["execution_id"],
                    "away_seconds": max(0, away),
                }, occurred_at=now.isoformat())
            return MOMENT_RETURN
        return None

    # --- shapes ------------------------------------------------------------------

    def payload(self) -> Optional[dict]:
        current = self.store.open_away()
        return self._payload(current) if current else None

    def _payload(self, row: dict) -> dict:
        now = self.clock()
        since = _parse(row["departed_at"] or row["opened_at"])
        return {
            "execution_id": row["execution_id"],
            "episode_id": row["episode_id"],
            "task_id": row["task_id"],
            "label": row["label"],
            "next_action": row["next_action"],
            "minutes": row["minutes"],
            # the step underneath, if one was prepared - its own target,
            # never the away duration (sol, #93)
            "step_minutes": row.get("step_minutes"),
            "has_step": bool(row["next_action"] and row["task_id"] is not None),
            "opened_at": row["opened_at"],
            "departed_at": row["departed_at"],
            "returned_at": row["returned_at"],
            "seconds_away": max(0, int((now - since).total_seconds())),
            "phase": self.attention.phase,
        }


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
