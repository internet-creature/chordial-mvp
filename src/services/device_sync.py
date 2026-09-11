"""the sync contract's server side (docs/ROOMS_DESIGN.md section 10).

the sidecar's outbox delivers device-origin events at-least-once; this module
guarantees exactly-once apply. every event carries a client-generated
event_uuid and a per-device monotonic seq; both are unique-constrained, so a
duplicate apply is a no-op that still counts toward the ACK. the ACK is a
durable cursor - the highest CONTIGUOUSLY applied seq for the device - and
the outbox trims at the cursor and retries everything after it.

two kinds of bad event, two different fates:
- SEMANTICALLY invalid (unknown type, wrong payload shape): persisted as a
  rejection TOMBSTONE that consumes its (device, seq) and uuid. the cursor
  advances past it and the outbox moves on - a rejected event must never pin
  the cursor forever. the error rides in the row and the response.
- STRUCTURALLY unparseable (no valid uuid, no valid seq): cannot claim a
  sequence, so it is reported in the response only. the client owns its
  outbox; an unparseable entry there is a client bug the ACK cannot absolve.

and one subtle duplicate: an event uuid that already landed under ANOTHER
device (a relinked outbox re-sending an event whose original ACK was lost).
the fact must not apply twice, but the NEW device's seq must still be
consumed - otherwise its cursor pins at the gap forever - so it lands as a
tombstone row under a minted marker uuid, original uuid in the payload.

quota counts only NEWLY INSERTED rows - retrying already-applied events at
the cap must return the durable ACK, not a 429, or dropped-response recovery
deadlocks exactly when it matters.

events are append-only facts. anything that mutates canonical rows (tasks,
cycles) is NOT a sync write - it goes through the server-side stores, which
own the invariants. later phases subscribe processors to applied events
(pip's observations, cycle progress); this module only lands them safely.
"""
from __future__ import annotations

import logging
import uuid as uuid_mod
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from config import Config
from src.database.database import get_db
from src.database.models import Device, DeviceEvent
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

# event types the server accepts. a typo'd type is a tombstoned event, loudly,
# rather than a silently unprocessable row. grows with the phases.
KNOWN_EVENT_TYPES = frozenset({
    "focus_block.started",
    "focus_block.completed",
    "focus_block.abandoned",
    "drift.detected",
    "return.detected",
    "session.started",
    # a run frozen by an open rewind question: the clock stopped, nothing
    # banked yet (the session.ended follows once the question resolves)
    "session.frozen",
    "session.ended",
    # rewind (docs/REWIND_DESIGN.md section 7): offered carries contested
    # LENGTH only - candidate timestamps stay on-device
    "rewind.offered",
    "rewind.applied",
    "rewind.kept",
    "rewind.undone",
    # stuck mode's away episode (docs/STUCK_MODE_DESIGN.md section 4.1):
    # opened, and the one real return
    "attention.away",
    "attention.returned",
})


class SyncResult:
    """what one batch apply did, in ACK-cursor terms."""

    def __init__(self, acked_seq: int, applied: int, duplicates: int,
                 rejected: list[dict]):
        self.acked_seq = acked_seq
        self.applied = applied
        self.duplicates = duplicates
        self.rejected = rejected

    def as_dict(self) -> dict:
        return {
            "acked_seq": self.acked_seq,
            "applied": self.applied,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
        }


class QuotaExceeded(RuntimeError):
    """the per-user daily event cap; the endpoint maps this to 429."""


def _parse_structural(raw: object) -> tuple[Optional[dict], Optional[str]]:
    """id + seq are the outbox's identity for an event - without them the
    server cannot even say WHICH event it is rejecting. returns
    (parsed, None) or (None, error)."""
    if not isinstance(raw, dict):
        return None, "event must be an object"
    event_id = raw.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None, "id (string event uuid) required"
    try:
        uuid_mod.UUID(event_id)
    except ValueError:
        return None, "id must be a uuid"
    seq = raw.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return None, "seq (positive integer) required"
    return {"id": event_id.lower(), "seq": seq, "raw": raw}, None


def _check_semantic(raw: dict) -> tuple[Optional[dict], Optional[str]]:
    """type/payload/timestamp validity. an error here becomes a tombstone
    row, never a cursor pin. returns (fields, None) or (None, error)."""
    event_type = raw.get("type")
    if event_type not in KNOWN_EVENT_TYPES:
        return None, f"unknown event type: {event_type!r}"
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        return None, "payload must be an object"
    occurred_at = None
    occurred_raw = raw.get("occurred_at")
    if occurred_raw is not None:
        if not isinstance(occurred_raw, str):
            return None, "occurred_at must be an iso datetime string"
        try:
            parsed = datetime.fromisoformat(occurred_raw)
        except ValueError:
            return None, "occurred_at must be an iso datetime string"
        # stored naive utc like every other timestamp in the schema
        occurred_at = (parsed.replace(tzinfo=None) if parsed.tzinfo is None
                       else (parsed - parsed.utcoffset()).replace(tzinfo=None))
    return {"type": event_type, "payload": payload,
            "occurred_at": occurred_at}, None


def _daily_count(db, user_uuid: str, now: datetime) -> int:
    day_start = now - timedelta(days=1)
    return db.query(func.count(DeviceEvent.id)).filter(
        DeviceEvent.user_uuid == user_uuid,
        DeviceEvent.applied_at >= day_start,
    ).scalar() or 0


def apply_events(device_id: int, events: list) -> SyncResult:
    """apply one outbox batch for a device; always returns the durable cursor.

    per-event outcomes: applied (new fact row), tombstoned (semantically
    invalid - lands as a rejected row that consumes its seq and reports its
    error), duplicate (uuid or (device, seq) already landed - no-op that
    still ACKs), or unparseable (no identity to consume - response-only).
    the cursor advances over contiguous seqs; a gap holds it, the outbox
    re-sends from the cursor, and already-landed rows dedupe on the retry.
    """
    if len(events) > Config.SYNC_MAX_BATCH:
        raise ValueError(
            f"batch too large (max {Config.SYNC_MAX_BATCH} events)")

    now = utc_now()
    rejected: list[dict] = []
    parsed: list[dict] = []
    for raw in events:
        structural, error = _parse_structural(raw)
        if structural is None:
            rid = raw.get("id") if isinstance(raw, dict) else None
            rejected.append({"id": rid, "error": error})
        else:
            parsed.append(structural)
    # apply in seq order regardless of batch order - contiguity is the point
    parsed.sort(key=lambda e: e["seq"])

    applied = duplicates = 0
    with get_db() as db:
        device = db.query(Device).filter(Device.id == device_id).one()

        # dedup BEFORE quota: a retry of already-applied events must always
        # get its ACK back, even for a user sitting at the cap
        batch_uuids = [e["id"] for e in parsed]
        batch_seqs = [e["seq"] for e in parsed]
        existing_uuids = {u for (u,) in db.query(DeviceEvent.event_uuid)
                          .filter(DeviceEvent.event_uuid.in_(batch_uuids))
                          .all()} if batch_uuids else set()
        existing_seqs = {s for (s,) in db.query(DeviceEvent.seq)
                         .filter(DeviceEvent.device_id == device.id,
                                 DeviceEvent.seq.in_(batch_seqs))
                         .all()} if batch_seqs else set()
        fresh = []
        cross_device = []
        for event in parsed:
            if event["seq"] in existing_seqs:
                # this device already consumed the seq - pure no-op, the
                # cursor already covers it
                duplicates += 1
            elif event["id"] in existing_uuids:
                # the uuid landed under ANOTHER device (or another seq):
                # a relinked outbox re-sending an event whose original ACK
                # was lost. the fact must not apply twice, but this device's
                # seq must still be consumed - an un-consumed seq would pin
                # the new cursor at zero forever. a tombstone row does both.
                cross_device.append(event)
            else:
                fresh.append(event)

        for event in cross_device:
            row = DeviceEvent(
                event_uuid=f"dup-{uuid_mod.uuid4()}",
                device_id=device.id,
                user_uuid=device.user_uuid,
                seq=event["seq"],
                event_type="duplicate",
                payload={"original_event_uuid": event["id"]},
                rejected=True,
                error="event uuid already applied under another sequence",
                applied_at=now,
            )
            try:
                with db.begin_nested():
                    db.add(row)
                duplicates += 1
            except IntegrityError:
                duplicates += 1     # raced a concurrent batch; seq consumed

        if fresh and (_daily_count(db, device.user_uuid, now) + len(fresh)
                      > Config.SYNC_DAILY_EVENT_CAP):
            raise QuotaExceeded(
                f"daily event cap ({Config.SYNC_DAILY_EVENT_CAP}) exceeded")

        for event in fresh:
            fields, error = _check_semantic(event["raw"])
            if fields is None:
                # tombstone: consume the seq so the cursor can pass it
                raw_type = event["raw"].get("type")
                raw_payload = event["raw"].get("payload")
                row = DeviceEvent(
                    event_uuid=event["id"],
                    device_id=device.id,
                    user_uuid=device.user_uuid,
                    seq=event["seq"],
                    event_type=(str(raw_type)[:100]
                                if isinstance(raw_type, str) else "invalid"),
                    payload=raw_payload if isinstance(raw_payload, dict) else {},
                    rejected=True,
                    error=error,
                    applied_at=now,
                )
                rejected.append({"id": event["id"], "error": error})
            else:
                row = DeviceEvent(
                    event_uuid=event["id"],
                    device_id=device.id,
                    user_uuid=device.user_uuid,
                    seq=event["seq"],
                    event_type=fields["type"],
                    payload=fields["payload"],
                    occurred_at=fields["occurred_at"],
                    rejected=False,
                    applied_at=now,
                )
            try:
                with db.begin_nested():
                    db.add(row)
                if not row.rejected:
                    applied += 1
            except IntegrityError:
                # a race with a concurrent batch; the row is landed either way
                duplicates += 1

        # advance the cursor over everything now contiguous - this batch's
        # rows, tombstones included, and earlier rows past a since-filled gap
        cursor = device.acked_seq
        existing = {
            s for (s,) in db.query(DeviceEvent.seq).filter(
                DeviceEvent.device_id == device.id,
                DeviceEvent.seq > cursor,
            ).all()
        }
        while cursor + 1 in existing:
            cursor += 1
        if cursor != device.acked_seq:
            device.acked_seq = cursor
        db.commit()

    if rejected:
        logger.warning("sync: device %s batch had %d rejected events",
                       device_id, len(rejected))
    return SyncResult(cursor, applied, duplicates, rejected)
