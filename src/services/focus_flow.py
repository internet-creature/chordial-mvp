"""applied device events become consequences: pip's noticing, exactly once.

the sync module (device_sync.py) only LANDS events safely; this processor
walks rows whose `processed_at` is still null and turns them into their
downstream consequences, stamping `processed_at` in the SAME transaction -
so apply-then-process is crash-safe: a crash between the two simply leaves
an unstamped row for the next pass, and a pass is always safe to re-run.

v1 consequences (ROOMS_DESIGN sections 5 + 12, the vertical slice):
- `focus_block.completed` becomes ONE of pip's progress observations -
  the squirrel keeps the ledger of landed blocks; evidence carries the
  event uuid and the honest numbers.
- every other known type is just stamped: per-run banks (session.ended)
  and drift facts feed the cycle projection and future reviews straight
  from the event rows themselves - cycle progress needs no processor.

safety, three layers deep:
- each row is CLAIMED atomically (an UPDATE conditioned on processed_at
  still being null) before its consequence is written, in the same
  transaction - concurrent passes race for the claim, exactly one wins
- the observation carries the event uuid under a UNIQUE index
  (source_event_uuid) - the hard floor: one fact never becomes two
  noticings even if the claim discipline ever breaks
- `drain()` loops until the queue is empty and also runs on a background
  sweep in the web server - a pass that failed (or a batch bigger than one
  pass's limit) is retried on the next sync AND on the sweep, so a
  device that trims its outbox after the ACK never strands consequences.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.exc import IntegrityError

from src.database.database import get_db
from src.database.models import ConversationEvent, DeviceEvent, Observation
from src.services import rewind_tether
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

# the squirrel has focus and cycles - completed blocks are her lane
PIP_ID = "pip"

_BATCH_LIMIT = 200


def drain(user_uuid: Optional[str] = None, limit: int = _BATCH_LIMIT) -> int:
    """process passes until the pending queue is empty (a pass that sees
    fewer rows than its limit has drained it). returns rows seen in total."""
    total = 0
    while True:
        seen = process_pending(user_uuid, limit)
        total += seen
        if seen < limit:
            return total


def process_pending(user_uuid: Optional[str] = None,
                    limit: int = _BATCH_LIMIT) -> int:
    """one pass: up to `limit` unprocessed applied events (optionally one
    user's), consequences and claim-stamps committed atomically. returns
    how many pending rows the pass SAW (drain loops while that hits the
    limit); rows another concurrent pass claimed first are skipped."""
    now = utc_now()
    with get_db() as db:
        q = db.query(DeviceEvent).filter(
            DeviceEvent.processed_at.is_(None),
            DeviceEvent.rejected.is_(False),
        )
        if user_uuid is not None:
            q = q.filter(DeviceEvent.user_uuid == user_uuid)
        rows = q.order_by(DeviceEvent.id).limit(limit).all()
        for row in rows:
            # the atomic claim: only the pass whose UPDATE finds
            # processed_at still null owns this row's consequence
            claimed = db.query(DeviceEvent).filter(
                DeviceEvent.id == row.id,
                DeviceEvent.processed_at.is_(None),
            ).update({DeviceEvent.processed_at: now},
                     synchronize_session=False)
            if not claimed:
                continue
            try:
                if row.event_type == "focus_block.completed":
                    with db.begin_nested():
                        # one savepoint for both consequences: the
                        # observation's unique source_event_uuid floor
                        # guards the presence row too
                        db.add(_pip_noticing(row))
                        db.add(_presence_event(row))
                elif row.event_type in rewind_tether.FOLDED_TYPES:
                    # the tether's shadow rows (REWIND_DESIGN section 8):
                    # same claim discipline, so each event folds exactly once
                    with db.begin_nested():
                        rewind_tether.fold_event(db, row)
            except IntegrityError:
                # the unique source_event_uuid floor: the consequence
                # already exists (a racing pass got there) - the savepoint
                # rolled back only the insert, the claim stamp stands
                pass
            except Exception:
                # a malformed payload must not wedge the pipeline: log it,
                # leave it stamped, move on - the event row keeps the truth
                logger.exception("focus_flow: event %s consequence failed",
                                 row.event_uuid)
        db.commit()
        return len(rows)


def _pip_noticing(row: DeviceEvent) -> Observation:
    """one progress observation per landed block: structured noticing for
    reviews and edwin's scorecards, never replayed as conversation."""
    payload = row.payload or {}
    label = payload.get("label")
    run_seconds = _seconds(payload.get("run_seconds"))
    banked = _seconds(payload.get("banked_seconds_today"))
    what = f'"{label}"' if isinstance(label, str) and label.strip() else \
        "a focus block"
    content = (f"landed {what} - {_minutes(run_seconds)} min this run, "
               f"{_minutes(banked)} min banked today")
    return Observation(
        user_uuid=row.user_uuid,
        helper_id=PIP_ID,
        kind="progress",
        content=content,
        source_event_uuid=row.event_uuid,
        evidence={
            "event_uuid": row.event_uuid,
            "task_id": payload.get("task_id"),
            "run_seconds": run_seconds,
            "banked_seconds_today": banked,
            "occurred_at": (row.occurred_at.isoformat()
                            if row.occurred_at else None),
        },
    )


def _presence_event(row: DeviceEvent) -> ConversationEvent:
    """the user showed up: a landed block is presence, recorded as a
    user-authored ACTION event so the outreach cadence resets - the ladder
    must not keep escalating at someone who is banking blocks all week
    without chatting. written to the legacy per-user stream on purpose:
    the pulse's gate reads user-WIDE (SqlUserEvents) so it counts there,
    while room prompts replay only their own stream - pip's observation
    stays the promptable noticing, and history stays byte-stable."""
    payload = row.payload or {}
    label = payload.get("label")
    what = f'"{label}"' if isinstance(label, str) and label.strip() else \
        "a focus block"
    minutes = _minutes(_seconds(payload.get("run_seconds")))
    return ConversationEvent(
        user_uuid=row.user_uuid,
        stream_id=row.user_uuid,
        platform="app",
        author_type="user",
        author="user",
        kind="action",
        content=f"landed {what} - {minutes} min",
        message_type=None,
        event_metadata={"source_event_uuid": row.event_uuid},
    )


def _seconds(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _minutes(seconds: int) -> int:
    return max(1, round(seconds / 60)) if seconds else 0
