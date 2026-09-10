"""rewind slice A (docs/REWIND_DESIGN.md sections 4-5): the ledger and
the question, on synthetic streams.

everything here is pure - no db, no sidecar process, no network. the
ledger tests feed collector-shaped (t, idle) samples and assert the
reconstructed input-moments; the candidate tests build moment streams
directly and assert the ranked boundaries. times are minutes past a
fixed 2pm so the scenarios read like the design doc's diagrams.

sol's slice-A review (PR #70) added two contracts, both regression-
tested here: the whole-block fallback needs a witness (a restarted
ledger must not recommend removing time it never saw), and impact is
computed at ask/apply time from the boundary, never persisted stale.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import TestClient, TestServer

from src.sidecar.focus import FocusEngine
from src.sidecar.offers import OfferError, RewindOffers
from src.sidecar.rewind import (
    ActivityLedger,
    Moment,
    _MOMENT_CAP,
    removal_impact,
    rewind_candidates,
)
from src.sidecar.store import SidecarStore

T0 = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def moments_every(start_min: float, end_min: float,
                  step_s: float = 10.0) -> list[Moment]:
    """a solid stretch of typing: input-moments every `step_s` seconds."""
    out, t = [], at(start_min)
    while t <= at(end_min):
        out.append(Moment(t, "input"))
        t += timedelta(seconds=step_s)
    return out


# --- the ledger: reconstruction, not collection ------------------------------


def test_ledger_reconstructs_last_input_from_idle():
    """a sample (t, idle) means the last input landed at exactly t - idle."""
    ledger = ActivityLedger()
    assert ledger.observe(at(1), idle_seconds=30) is True
    assert ledger.moments() == [Moment(at(1) - timedelta(seconds=30), "input")]


def test_pure_quiet_adds_nothing():
    """while nobody touches anything, idle grows in lockstep with t - the
    implied last-input stands still and the ledger learns nothing new."""
    ledger = ActivityLedger()
    ledger.observe(at(0), idle_seconds=0)
    for m in range(1, 6):
        assert ledger.observe(at(m), idle_seconds=60.0 * m) is False
    assert len(ledger.moments()) == 1


def test_activity_between_heartbeats_is_recovered():
    """input that lands between samples still gets its exact timestamp:
    the next sample's t - idle points straight at it."""
    ledger = ActivityLedger()
    ledger.observe(at(0), idle_seconds=0)
    # quiet for 4 minutes, then a keystroke at 4:30, sampled at 5:00
    assert ledger.observe(at(5), idle_seconds=30) is True
    assert ledger.moments()[-1].t == at(5) - timedelta(seconds=30)


def test_jitter_on_the_same_keystroke_dedupes():
    """two samples disagreeing by sub-second float noise describe one
    keystroke, not two moments."""
    ledger = ActivityLedger()
    ledger.observe(at(1), idle_seconds=10.0)
    assert ledger.observe(at(1.05), idle_seconds=12.6) is False   # ~0.4s later
    assert len(ledger.moments()) == 1


def test_a_backwards_sample_is_ignored():
    """a stale or clock-skewed sample implying an EARLIER last-input than
    we already know must not corrupt the story - the ledger only learns."""
    ledger = ActivityLedger()
    ledger.observe(at(5), idle_seconds=0)
    assert ledger.observe(at(6), idle_seconds=300) is False   # implies 1:00
    assert ledger.moments() == [Moment(at(5), "input")]


def test_clear_starts_a_new_story():
    ledger = ActivityLedger()
    ledger.observe(at(1), idle_seconds=0)
    ledger.clear()
    assert ledger.moments() == []
    assert ledger.covers_from is None


def test_the_cap_trims_the_old_end():
    """marathon sessions stay bounded, and it's the tail (what candidates
    read) that survives."""
    ledger = ActivityLedger()
    t = T0
    for _ in range(_MOMENT_CAP + 100):
        t += timedelta(seconds=2)
        ledger.observe(t, idle_seconds=0)
    kept = ledger.moments()
    assert len(kept) == _MOMENT_CAP
    assert kept[-1].t == t                     # newest moment intact


# --- the witness stand (sol #70 finding 1) -----------------------------------


def test_coverage_starts_at_the_first_implied_input():
    """a fresh ledger can speak for nothing; its first sample (t, idle)
    proves quiet across [t - idle, t], so the witness stand opens at
    t - idle."""
    ledger = ActivityLedger()
    assert ledger.covers_from is None
    ledger.observe(at(21), idle_seconds=25 * 60)
    assert ledger.covers_from == at(-4)


def test_the_idle_counter_carries_coverage_across_restarts():
    """a sidecar restart during a LONG quiet stretch loses no truth: the
    os idle counter spans the restart, coverage reaches back past the
    run's start, and the whole-block fallback is legitimately on the
    table."""
    ledger = ActivityLedger()
    ledger.observe(at(21), idle_seconds=25 * 60)     # restarted at ~14:20
    got = rewind_candidates(ledger.moments(), now=at(21), floor=at(0),
                            covered_from=ledger.covers_from)
    assert [(c.kind, c.at) for c in got] == [("session_start", at(0))]


def test_a_restarted_ledger_never_recommends_the_session_start():
    """the runs table survives a restart; the ledger doesn't. a lone
    reconstructed moment after the restart is a micro-run - and the old
    algorithm's fallback would have recommended removing the ENTIRE
    session, hours of real work the ledger simply never saw. the witness
    rule prefers the observed edge instead."""
    got = rewind_candidates([Moment(at(21), "input")], now=at(31),
                            floor=at(0), covered_from=at(20.9))
    assert [(c.kind, c.at, c.rank) for c in got] == [("run_end", at(21), 0)]


def test_partial_coverage_with_nothing_observed_asks_nothing():
    """mid-run restart, not one moment since: the ledger can testify to
    nothing, so the safe answer is no candidates - the offer layer then
    has nothing to ask."""
    assert rewind_candidates([], now=at(31), floor=at(0),
                             covered_from=at(20)) == []


def test_an_unknown_witness_is_a_missing_witness():
    """covered_from=None (a caller that forgot the ledger's stand) must
    behave like partial coverage, never like full - the fallback that
    removes everything needs positive proof."""
    assert rewind_candidates([], now=at(15), floor=at(0)) == []


# --- the question: sessionization + ranked boundaries ------------------------


def test_the_docs_own_diagram():
    """the worked example from section 5: solid typing 2:00-2:14, two
    stray moments around 2:20, return at 2:31. A = the strong run's end
    (recommended), B = the blip's edge (disclosure)."""
    stream = moments_every(0, 14) + [Moment(at(20), "input"),
                                     Moment(at(20.05), "input")]
    got = rewind_candidates(stream, now=at(31), floor=at(0),
                            covered_from=at(0))
    assert [(c.kind, c.rank) for c in got] == [("run_end", 0), ("run_end", 1)]
    assert got[0].at == at(14)
    assert got[1].at == at(20.05)
    assert removal_impact(got[0].at, at(31)) == 17 * 60


def test_impact_is_computed_at_resolution_time():
    """sol #70 finding 2: a candidate minted when drift was detected must
    not freeze its amount. the boundary persists; the removal is derived
    from whenever the question is actually asked or answered."""
    got = rewind_candidates(moments_every(0, 14), now=at(25), floor=at(0),
                            covered_from=at(0))
    boundary = got[0].at
    assert removal_impact(boundary, at(25)) == 11 * 60   # at detection
    assert removal_impact(boundary, at(45)) == 31 * 60   # answered late
    assert removal_impact(boundary, at(10)) == 0         # never negative
    assert not hasattr(got[0], "removed_seconds")        # amounts don't persist


def test_the_mouse_jiggle_is_not_a_comeback():
    """weak evidence never earns the recommendation - the jiggle ranks
    behind the real run no matter how recent it is."""
    stream = moments_every(0, 10) + [Moment(at(18), "input")]
    got = rewind_candidates(stream, now=at(25), floor=at(0),
                            covered_from=at(0))
    assert got[0].at == at(10) and got[0].rank == 0
    assert got[1].at == at(18) and got[1].rank == 1


def test_thinking_pauses_dont_split_the_run():
    """a 90s stare at the wall is part of working - the run survives it
    and the boundary lands at the run's true end, not the pause."""
    stream = (moments_every(0, 5)
              + moments_every(6.5, 12))       # 90s gap < gap_break (120s)
    got = rewind_candidates(stream, now=at(30), floor=at(0),
                            covered_from=at(0))
    assert len(got) == 1
    assert got[0].at == at(12)


def test_a_long_gap_does_split_the_run():
    """three minutes of quiet is two runs, and only the later one's edge
    is the boundary."""
    stream = moments_every(0, 5) + moments_every(8, 14)
    got = rewind_candidates(stream, now=at(30), floor=at(0),
                            covered_from=at(0))
    assert got[0].at == at(14)


def test_sleep_reads_as_one_long_gap():
    """v1 needs no lock/wake wiring: a closed laptop is just a very long
    gap, and the boundary lands on the last moment before the lid."""
    stream = moments_every(0, 20)
    got = rewind_candidates(stream, now=at(140), floor=at(0),
                            covered_from=at(0))                 # 2h asleep
    assert got[0].at == at(20)
    assert removal_impact(got[0].at, at(140)) == 120 * 60


def test_an_empty_tail_recommends_the_session_start():
    """no moments at all - but the ledger watched from the start, so the
    quiet is PROVEN and the only certain engagement is the intentional
    act of starting the block."""
    got = rewind_candidates([], now=at(15), floor=at(0), covered_from=at(0))
    assert [(c.kind, c.rank) for c in got] == [("session_start", 0)]
    assert got[0].at == at(0)


def test_only_jiggles_still_recommend_the_session_start():
    """a fully-witnessed session holding nothing but micro-runs has no
    strong evidence - rank 0 falls back to the floor, the freshest blip
    is the disclosure."""
    stream = [Moment(at(3), "input"), Moment(at(3.05), "input"),
              Moment(at(9), "input")]
    got = rewind_candidates(stream, now=at(15), floor=at(0),
                            covered_from=at(0))
    assert got[0].kind == "session_start" and got[0].at == at(0)
    assert got[1].kind == "run_end" and got[1].at == at(9)


def test_a_short_burst_is_not_strong_evidence():
    """mass needs BOTH enough moments and enough span: a dense 30-second
    burst is a blip by span, and must not earn the recommendation."""
    stream = moments_every(5, 5.5, step_s=2)      # ~16 moments over 30s
    got = rewind_candidates(stream, now=at(20), floor=at(0),
                            covered_from=at(0))
    assert got[0].kind == "session_start"
    assert got[1].at == at(5.5)


def test_at_most_two_candidates_surface():
    """recovery is never bookkeeping: many trailing blips still collapse
    to the recommendation plus one disclosure."""
    stream = moments_every(0, 10) + [Moment(at(13), "input"),
                                     Moment(at(16), "input"),
                                     Moment(at(19), "input")]
    got = rewind_candidates(stream, now=at(25), floor=at(0),
                            covered_from=at(0))
    assert len(got) == 2
    assert got[0].at == at(10)
    assert got[1].at == at(19)      # the most recent blip wins the slot


def test_future_context_kinds_dont_join_runs():
    """forward-compat: a non-presence kind (a lock event, an app switch)
    in the stream labels a boundary someday - today it must not
    manufacture activity."""
    stream = moments_every(0, 10) + [Moment(at(18), "lock")]
    got = rewind_candidates(stream, now=at(25), floor=at(0),
                            covered_from=at(0))
    assert len(got) == 1
    assert got[0].at == at(10)


def test_moments_before_the_floor_are_another_story():
    """the ledger is cleared on run start, but the function still clips
    defensively - a stale pre-run tail must never place a boundary
    before the block existed."""
    stream = moments_every(-10, -5) + moments_every(0, 8)
    got = rewind_candidates(stream, now=at(20), floor=at(0),
                            covered_from=at(-10))
    assert len(got) == 1
    assert got[0].at == at(8)


def test_future_moments_are_not_evidence():
    """sol #70 finding 4: a moment from past `now` (clock rollback, a
    malformed sample) must never place a boundary in the future and
    reverse an excision interval."""
    stream = moments_every(0, 10) + [Moment(at(99), "input")]
    got = rewind_candidates(stream, now=at(25), floor=at(0),
                            covered_from=at(0))
    assert len(got) == 1
    assert got[0].at == at(10)
    assert all(c.at <= at(25) for c in got)


def test_an_active_tail_yields_a_zero_removal():
    """called while the person is typing (no drift), the recommendation
    is the newest moment and removes ~nothing - the offer layer simply
    has nothing to ask about."""
    stream = moments_every(0, 10)
    got = rewind_candidates(stream, now=at(10), floor=at(0),
                            covered_from=at(0))
    assert got[0].at == at(10)
    assert removal_impact(got[0].at, at(10)) == 0


# =============================================================================
# slice B: the offer machine, freeze semantics, and the honest ding
# (docs/REWIND_DESIGN.md section 6) - store + engine + offers, hand-cranked
# clock, no sleeping.
# =============================================================================


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture()
def rig(tmp_path):
    """a live-ish sidecar core: store, ledger, engine, offers - one clock."""
    store = SidecarStore(tmp_path / "sidecar.db")
    clock = Clock()
    ledger = ActivityLedger()
    engine = FocusEngine(store, clock=clock)
    offers = RewindOffers(store, ledger, clock=clock)
    yield store, clock, ledger, engine, offers
    store.close()


def _type_through(ledger, clock, minutes: float, step_s: float = 60.0):
    """the person works: a sample with idle 0 every `step_s` seconds."""
    steps = int(minutes * 60 / step_s)
    for _ in range(steps):
        clock.advance(seconds=step_s)
        ledger.observe(clock.now, 0.0)


def _outbox_types(store):
    return [e["type"] for e in store.pending()]


def _start_and_type(rig_tuple, minutes=14, target=25.0, task_id=7):
    store, clock, ledger, engine, offers = rig_tuple
    engine.start(task_id=task_id, label="novel", target_minutes=target)
    ledger.clear()
    ledger.observe(clock.now, 0.0)
    _type_through(ledger, clock, minutes)
    return store.active_run()


def test_drift_mints_one_offer_and_extends_never_stacks(rig):
    store, clock, ledger, engine, offers = rig
    run = _start_and_type(rig)                      # typing 0..14
    clock.advance(minutes=10)                       # quiet 14..24
    offer = offers.on_drift(store.active_run())
    assert offer is not None and offer["status"] == "open"
    assert offer["candidates"][0]["kind"] == "run_end"

    again = offers.on_drift(store.active_run())     # a second detection
    assert again["offer_uuid"] == offer["offer_uuid"]
    assert _outbox_types(store).count("rewind.offered") == 1
    offered = [e for e in store.pending()
               if e["type"] == "rewind.offered"][0]
    assert offered["payload"]["contested_seconds"] == 10 * 60
    # data minimization: the event carries length, never boundaries
    assert "candidates" not in offered["payload"]
    assert "at" not in offered["payload"]
    assert run["id"] == offer["run_id"]


def test_keep_all_time_credits_everything(rig):
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)
    clock.advance(minutes=10)
    offer = offers.on_drift(store.active_run())
    offers.on_return(store.active_run())
    resolved = offers.resolve(offer["offer_uuid"], "keep")
    assert resolved["status"] == "kept"

    clock.advance(minutes=6)
    run = engine.pause()
    assert run["seconds"] == 30 * 60                # every minute credited
    ended = [e for e in store.pending() if e["type"] == "session.ended"][0]
    assert ended["payload"]["seconds"] == 30 * 60
    assert ended["payload"]["gross_seconds"] == 30 * 60
    assert ended["payload"]["excised_seconds"] == 0
    assert "rewind.kept" in _outbox_types(store)


def test_two_truths_ride_session_ended(rig):
    """remove-and-continue: the excision lands, the comeback minutes
    count, and session.ended carries credited + gross + excised."""
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)                            # typing 0..14
    clock.advance(minutes=17)                       # quiet 14..31
    offer = offers.on_drift(store.active_run())     # detected mid-quiet
    ledger.observe(clock.now, 1.0)                  # the returning touch
    offers.on_return(store.active_run())            # return at 31
    resolved = offers.resolve(offer["offer_uuid"], "remove")
    assert resolved["status"] == "applied"

    state = engine.state()
    assert state["run_seconds"] == 14 * 60          # the clock rewound
    assert state["excised_seconds"] == 17 * 60

    clock.advance(minutes=9)                        # comeback work 31..40
    run = engine.pause()
    assert run["seconds"] == 23 * 60                # 14 + 9, quiet gone
    ended = [e for e in store.pending() if e["type"] == "session.ended"][0]
    assert ended["payload"]["seconds"] == 23 * 60
    assert ended["payload"]["gross_seconds"] == 40 * 60
    assert ended["payload"]["excised_seconds"] == 17 * 60
    assert "rewind.applied" in _outbox_types(store)


def test_impact_renders_from_the_quiet_end_not_the_minting(rig):
    """sol #70 finding 2, end to end: detected at ten quiet minutes,
    surfaced at seventeen - the payload says seventeen."""
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)
    clock.advance(minutes=10)
    offers.on_drift(store.active_run())             # minted at 10 quiet min
    clock.advance(minutes=7)
    offers.on_return(store.active_run())            # returned at 17
    payload = offers.payload()
    assert payload["candidates"][0]["removed_seconds"] == 17 * 60
    assert payload["candidates"][0]["credited_seconds"] == 14 * 60
    assert payload["contested_seconds"] == 17 * 60


def test_a_bare_pause_still_stops_the_clock(rig):
    """sol's finding 3: the safety net freezes unbanked - the timer
    stops, nothing banks, the question stays open."""
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)
    clock.advance(minutes=10)
    offers.on_drift(store.active_run())
    held = engine.pause()
    assert held["frozen"] is True and held["reason"] == "paused"

    frozen_state = engine.state()
    assert frozen_state["running"] is False
    assert frozen_state["frozen"][0]["run_seconds"] == 24 * 60
    assert "session.ended" not in _outbox_types(store)   # unbanked
    # but the server hears the clock stop (sol, #88): a started with no
    # transition after it would read as a running clock all afternoon
    frozen = [e for e in store.pending() if e["type"] == "session.frozen"]
    assert len(frozen) == 1
    assert frozen[0]["payload"] == {
        "task_id": frozen_state["frozen"][0]["task_id"],
        "label": frozen_state["frozen"][0]["label"], "reason": "paused"}
    assert frozen[0]["occurred_at"] == held["frozen_at"]
    clock.advance(minutes=30)
    assert engine.state()["frozen"][0]["run_seconds"] == 24 * 60  # stopped


def test_a_frozen_run_banks_at_its_freeze_instant(rig):
    """the answer arrives late; the run still ends where it froze, with
    the excision clamped inside it - a late answer never inflates."""
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)                            # typing 0..14
    clock.advance(minutes=10)                       # quiet 14..24
    offer = offers.on_drift(store.active_run())
    held = engine.pause()                           # frozen at 24
    clock.advance(minutes=45)                       # ...much later
    offers.resolve(offer["offer_uuid"], "remove")
    summary = engine.close_frozen(held["run_id"])
    assert summary["ended_at"] == held["frozen_at"]
    assert summary["seconds"] == 14 * 60            # excised [14, 24-frozen]
    assert summary["reason"] == "paused"
    ended = [e for e in store.pending() if e["type"] == "session.ended"][0]
    assert ended["payload"]["gross_seconds"] == 24 * 60
    assert ended["payload"]["excised_seconds"] == 10 * 60


def test_switch_is_a_stopping_transition_too(rig):
    """a bare start freezes the old run (question intact) and begins the
    new block immediately."""
    store, clock, ledger, engine, offers = rig
    old = _start_and_type(rig)
    clock.advance(minutes=10)
    offer = offers.on_drift(store.active_run())
    state = engine.start(task_id=9, label="stems")
    assert state["running"] is True and state["task_id"] == 9
    frozen = store.frozen_runs()
    assert [r["id"] for r in frozen] == [old["id"]]
    assert frozen[0]["frozen_reason"] == "switched"
    assert store.get_offer(offer["offer_uuid"])["status"] == "open"
    # the chip payload still carries the OLD run's context
    assert offers.payload()["run_id"] == old["id"]


def test_a_frozen_finish_still_celebrates_after_the_answer(rig):
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig, minutes=26)                # past the 25m target
    clock.advance(minutes=10)
    offer = offers.on_drift(store.active_run())
    held = engine.finish()
    assert held["frozen"] is True and held["reason"] == "finished"
    assert "focus_block.completed" not in _outbox_types(store)
    offers.resolve(offer["offer_uuid"], "remove")
    summary = engine.close_frozen(held["run_id"])
    assert summary["reason"] == "finished"
    assert summary["seconds"] == 26 * 60
    assert "focus_block.completed" in _outbox_types(store)


def test_undo_restores_the_minutes_and_the_question(rig):
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)
    clock.advance(minutes=17)
    offer = offers.on_drift(store.active_run())
    ledger.observe(clock.now, 1.0)
    offers.on_return(store.active_run())
    offers.resolve(offer["offer_uuid"], "remove")
    assert engine.state()["run_seconds"] == 14 * 60

    reopened = offers.undo(offer["offer_uuid"])
    assert reopened["status"] == "open"
    assert engine.state()["run_seconds"] == 31 * 60      # minutes restored
    assert offers.payload() is not None                  # the chip is back
    undone = [e for e in store.pending()
              if e["type"] == "rewind.undone"][0]
    assert undone["payload"]["restored_seconds"] == 17 * 60


def test_contested_time_never_dings(rig):
    """an open question's quiet span can't inflate the clock into a false
    celebration; keeping the time lets the crossing fire honestly."""
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)                            # typing 0..14, target 25
    clock.advance(minutes=10)
    offer = offers.on_drift(store.active_run())
    clock.advance(minutes=3)                        # gross 27 > target
    assert engine.crossed_target() is False         # uncontested is only 14
    offers.on_return(store.active_run())
    offers.resolve(offer["offer_uuid"], "keep")
    assert engine.crossed_target() is True          # honest now
    assert engine.crossed_target() is False         # and only once


def test_the_ding_rearms(rig):
    """the false-ding case: the target lands inside a quiet stretch the
    watcher hasn't detected yet, the ding fires into an empty room, and
    the correction takes credited time back under target - so the ding
    re-arms and fires again when the time is real."""
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig, minutes=14, target=20.0)   # typing 0..14
    clock.advance(minutes=7)                        # quiet, undetected
    assert engine.crossed_target() is True          # gross 21 >= 20: false ding
    clock.advance(minutes=3)                        # drift detected at 24
    offer = offers.on_drift(store.active_run())
    ledger.observe(clock.now, 1.0)
    offers.on_return(store.active_run())
    offers.resolve(offer["offer_uuid"], "remove")   # credited back to 14
    engine.rearm_after_excision(offer["run_id"])
    _type_through(ledger, clock, 7)                 # honest work to 21
    assert engine.crossed_target() is True          # the ding re-fires
    # and undo past the target never dings on the way back up
    clock.advance(minutes=1)
    offers.undo(offer["offer_uuid"])
    engine.suppress_ding(offer["run_id"])
    assert engine.crossed_target() is False


def test_resolving_a_banked_run_is_refused(rig):
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)
    clock.advance(minutes=10)
    offer = offers.on_drift(store.active_run())
    offers.resolve(offer["offer_uuid"], "keep")
    engine.pause()                                  # banks (offer resolved)
    with pytest.raises(OfferError):
        offers.resolve(offer["offer_uuid"], "remove")
    with pytest.raises(OfferError):
        offers.undo(offer["offer_uuid"])


def test_an_arbitrary_boundary_is_refused(rig):
    """the deferred scrubber stays deferred: only the offer's own
    candidates may be applied."""
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)
    clock.advance(minutes=10)
    offer = offers.on_drift(store.active_run())
    with pytest.raises(OfferError):
        offers.resolve(offer["offer_uuid"], "remove",
                       at=(T0 + timedelta(minutes=2)).isoformat())


def test_offer_survives_a_sidecar_restart(rig, tmp_path):
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)
    clock.advance(minutes=10)
    offer = offers.on_drift(store.active_run())

    # a fresh engine + offers over the same store IS the restart; the
    # ledger is gone, the question is not
    engine2 = FocusEngine(store, clock=clock)
    offers2 = RewindOffers(store, ActivityLedger(), clock=clock)
    payload = offers2.payload()
    assert payload["offer_uuid"] == offer["offer_uuid"]
    held = engine2.pause()                          # still freeze-guarded
    assert held["frozen"] is True


def test_rewind_event_types_are_known_to_the_server():
    from src.services.device_sync import KNOWN_EVENT_TYPES
    assert {"rewind.offered", "rewind.applied", "rewind.kept",
            "rewind.undone"} <= KNOWN_EVENT_TYPES


# --- the sidecar api, end to end ---------------------------------------------


def test_the_offer_rides_the_wire(tmp_path):
    """the endpoint vertical: type, drift, return (the card pushes),
    bare-pause (held, clock stopped), answer (banks at the freeze,
    credited seconds, both truths in the outbox)."""
    from src.sidecar.drift import ActivityState, DriftWatch
    from src.sidecar.lines import LINE_POOLS
    from src.sidecar.server import SidecarService

    store = SidecarStore(tmp_path / "sidecar.db")
    clock = Clock(start=datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc))
    activity = ActivityState(clock=clock)
    drift = DriftWatch(store, activity, clock=clock, threshold_minutes=5)
    service = SidecarService(store, engine=FocusEngine(store, clock=clock),
                             activity=activity, drift=drift)

    async def flow():
        client = TestClient(TestServer(service.build_app()))
        await client.start_server()
        try:
            ws = await client.ws_connect("/v1/ws")
            await ws.receive_json()                       # hello

            resp = await client.post("/v1/focus/start",
                                     json={"task_id": 7, "label": "novel"})
            assert resp.status == 200
            await ws.receive_json(); await ws.receive_json()

            # ten minutes of typing, sampled every minute
            for _ in range(10):
                clock.advance(minutes=1)
                await client.post("/v1/activity", json={"idle_seconds": 0})

            # six and a half quiet minutes -> the drift watcher fires
            clock.advance(seconds=390)
            await client.post("/v1/activity", json={"idle_seconds": 390})
            await service._tick_once()
            state = await (await client.get("/v1/state")).json()
            assert state["offer"] is not None
            assert state["offer"]["candidates"][0]["kind"] == "run_end"

            # the returning touch: the card is the welcome
            clock.advance(seconds=30)
            await client.post("/v1/activity", json={"idle_seconds": 1})
            await service._tick_once()
            pushes = []
            while True:
                try:
                    pushes.append(await asyncio.wait_for(
                        ws.receive_json(), timeout=0.2))
                except asyncio.TimeoutError:
                    break
            assert any(p.get("type") == "rewind_offer" for p in pushes)
            assert not any(p.get("type") == "line"
                           and p.get("moment") == "drift_return"
                           for p in pushes)

            # bare pause: held, not banked - and the clock stopped
            clock.advance(seconds=10)
            resp = await client.post("/v1/focus/pause")
            assert resp.status == 200
            body = await resp.json()
            assert body["held"]["frozen"] is True
            assert body["line"] in LINE_POOLS["focus_hold"]
            assert body["offer"] is not None
            offer_uuid = body["offer"]["offer_uuid"]
            types = [e["type"] for e in store.pending()]
            assert "session.ended" not in types

            # a second bare pause finds no clock, names the held block
            resp = await client.post("/v1/focus/pause")
            assert resp.status == 409
            assert "held block" in (await resp.json())["error"]

            # the answer: remove the quiet -> the run banks at its freeze
            clock.advance(minutes=5)
            resp = await client.post("/v1/focus/rewind", json={
                "offer_uuid": offer_uuid, "action": "remove"})
            assert resp.status == 200
            body = await resp.json()
            assert body["run"]["reason"] == "paused"
            assert body["line"] in LINE_POOLS["focus_pause"]
            # the timeline: typing 600s, quiet 390s, return, 30s + 10s to
            # the freeze -> gross 1030s; excised [end-of-typing, return] =
            # 390 + 30 = 420s; credited 610s
            ended = [e for e in store.pending()
                     if e["type"] == "session.ended"][0]
            assert ended["payload"]["gross_seconds"] == 1030
            assert ended["payload"]["excised_seconds"] == 420
            assert ended["payload"]["seconds"] == 610
            assert body["run"]["seconds"] == 610

            # the answered question is spent
            resp = await client.post("/v1/focus/rewind", json={
                "offer_uuid": offer_uuid, "action": "keep"})
            assert resp.status == 400
            await ws.close()
        finally:
            await client.close()
    asyncio.run(flow())
    store.close()


# --- sol's slice-B review round (PR #71) -------------------------------------


def test_a_second_drift_joins_the_first_unresolved_gap(rig):
    """sol #71 finding 1: return -> work -> second drift. the single
    question accumulates BOTH gaps as segments; 'remove' excises their
    union - the first unresolved gap never silently credits. the grown
    span re-emits rewind.offered."""
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)                            # typing 0..14
    clock.advance(minutes=10)                       # quiet 14..24
    offer = offers.on_drift(store.active_run())
    clock.advance(minutes=1)
    ledger.observe(clock.now, 1.0)                  # the return at 25
    offers.on_return(store.active_run())
    _type_through(ledger, clock, 15)                # work 25..40
    clock.advance(minutes=10)                       # quiet again 40..50
    extended = offers.on_drift(store.active_run())

    assert extended["offer_uuid"] == offer["offer_uuid"]
    assert len(extended["segments"]) == 1           # [14, 25] joined the ask
    assert _outbox_types(store).count("rewind.offered") == 2
    grown = [e for e in store.pending()
             if e["type"] == "rewind.offered"][-1]
    assert grown["payload"]["contested_seconds"] == 21 * 60  # 11 + 10 so far

    clock.advance(minutes=1)
    ledger.observe(clock.now, 1.0)                  # second return at 51
    offers.on_return(store.active_run())
    payload = offers.payload()
    assert payload["contested_seconds"] == 22 * 60  # 11 + 11, the union

    offers.resolve(offer["offer_uuid"], "remove")
    assert engine.state()["run_seconds"] == 29 * 60  # exactly the typing
    applied = [e for e in store.pending()
               if e["type"] == "rewind.applied"][0]
    assert applied["payload"]["removed_seconds"] == 22 * 60


def test_undo_restores_every_gap_of_the_union(rig):
    """undo after a multi-segment apply lifts the WHOLE union and the
    re-opened question still covers both gaps."""
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)
    clock.advance(minutes=10)
    offer = offers.on_drift(store.active_run())
    clock.advance(minutes=1)
    ledger.observe(clock.now, 1.0)
    offers.on_return(store.active_run())
    _type_through(ledger, clock, 15)
    clock.advance(minutes=10)
    offers.on_drift(store.active_run())
    clock.advance(minutes=1)
    ledger.observe(clock.now, 1.0)
    offers.on_return(store.active_run())
    offers.resolve(offer["offer_uuid"], "remove")
    assert engine.state()["run_seconds"] == 29 * 60

    reopened = offers.undo(offer["offer_uuid"])
    assert engine.state()["run_seconds"] == 51 * 60      # all restored
    assert len(reopened["segments"]) == 1                # still covering
    assert offers.payload()["contested_seconds"] == 22 * 60
    undone = [e for e in store.pending()
              if e["type"] == "rewind.undone"][0]
    assert undone["payload"]["restored_seconds"] == 22 * 60


def test_hydration_respects_a_recorded_return(rig):
    """sol #71 finding 3: an offer whose return already happened must not
    re-arm the drift episode on restart - the re-detected return would
    fold legitimate post-return work into the proposed removal. the
    store guard backs it: a recorded return is never overwritten."""
    from src.sidecar.server import SidecarService
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)
    clock.advance(minutes=10)
    offer = offers.on_drift(store.active_run())
    offers.on_return(store.active_run())
    returned = store.get_offer(offer["offer_uuid"])["returned_at"]

    service = SidecarService(store, engine=FocusEngine(store, clock=clock))
    assert service.drift.drifting is False               # not re-armed

    clock.advance(minutes=30)
    store.mark_offer_returned(offer["offer_uuid"], clock.now.isoformat())
    assert store.get_offer(offer["offer_uuid"])["returned_at"] == returned


def test_hydration_resumes_an_unreturned_episode(rig):
    """...while a restart mid-quiet (no return yet) still resumes the
    episode so the check-in isn't re-asked."""
    from src.sidecar.server import SidecarService
    store, clock, ledger, engine, offers = rig
    _start_and_type(rig)
    clock.advance(minutes=10)
    offers.on_drift(store.active_run())
    service = SidecarService(store, engine=FocusEngine(store, clock=clock))
    assert service.drift.drifting is True


def test_a_held_blocks_question_cannot_ride_another_runs_transition(tmp_path):
    """sol #71 findings 2 + 4, on the wire: after a switch, the held
    run's question must not resolve through the new run's pause (that
    would orphan the frozen run unbanked forever) - and resolving the
    active run's own question hands back the held block's question as
    the next authoritative offer."""
    from src.sidecar.server import SidecarService

    store = SidecarStore(tmp_path / "sidecar.db")
    clock = Clock()
    service = SidecarService(store, engine=FocusEngine(store, clock=clock))

    async def flow():
        client = TestClient(TestServer(service.build_app()))
        await client.start_server()
        try:
            # run 1: type, drift, question minted
            await client.post("/v1/focus/start",
                              json={"task_id": 7, "label": "novel"})
            for _ in range(10):
                clock.advance(minutes=1)
                await client.post("/v1/activity", json={"idle_seconds": 0})
            clock.advance(minutes=10)
            offer1 = service.offers.on_drift(store.active_run())

            # the switch: run 1 freezes with its question open
            resp = await client.post("/v1/focus/start",
                                     json={"task_id": 9, "label": "stems"})
            assert resp.status == 200
            held = store.frozen_runs()
            assert len(held) == 1 and held[0]["frozen_reason"] == "switched"

            # finding 2: run 2's pause must refuse run 1's question
            resp = await client.post("/v1/focus/pause", json={
                "resolution": {"offer_uuid": offer1["offer_uuid"],
                               "action": "keep"}})
            assert resp.status == 400
            assert "held block" in (await resp.json())["error"]
            assert store.get_offer(offer1["offer_uuid"])["status"] == "open"
            assert service.engine.state()["running"] is True   # untouched

            # run 2 earns its own question
            for _ in range(10):
                clock.advance(minutes=1)
                await client.post("/v1/activity", json={"idle_seconds": 0})
            clock.advance(minutes=10)
            offer2 = service.offers.on_drift(store.active_run())

            # finding 4: resolving run 2's question surfaces run 1's as
            # the next authoritative offer - no broadcast race, no
            # invisible held run
            resp = await client.post("/v1/focus/rewind", json={
                "offer_uuid": offer2["offer_uuid"], "action": "keep"})
            assert resp.status == 200
            body = await resp.json()
            assert body["resolved"]["status"] == "kept"
            assert body["offer"] is not None
            assert body["offer"]["offer_uuid"] == offer1["offer_uuid"]
            assert body["offer"]["frozen"] is True

            # and the held question, answered on the card, banks its run
            resp = await client.post("/v1/focus/rewind", json={
                "offer_uuid": offer1["offer_uuid"], "action": "remove"})
            body = await resp.json()
            assert body["run"]["reason"] == "switched"
            assert store.frozen_runs() == []
            assert body["offer"] is None                   # nothing left
        finally:
            await client.close()
    asyncio.run(flow())
    store.close()


# =============================================================================
# slice C: the tether's sidecar half (docs/REWIND_DESIGN.md section 8) -
# phone answers arrive through the pump's decision poll; the sidecar stays
# authoritative and presence-aware. the server half (fold/sweep/intake/
# endpoint) lives in tests/test_rewind_tether.py.
# =============================================================================


def _tether_service(tmp_path, threshold_minutes=5):
    from src.sidecar.drift import ActivityState, DriftWatch
    from src.sidecar.server import SidecarService

    store = SidecarStore(tmp_path / "sidecar.db")
    clock = Clock(start=datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc))
    activity = ActivityState(clock=clock)
    drift = DriftWatch(store, activity, clock=clock,
                       threshold_minutes=threshold_minutes)
    service = SidecarService(store, engine=FocusEngine(store, clock=clock),
                             activity=activity, drift=drift)
    return store, clock, service


def _drift_into_offer(store, clock, service):
    """start, type ten minutes, go quiet past the threshold: one open
    offer, nobody back at the desk yet."""

    async def flow():
        client = TestClient(TestServer(service.build_app()))
        await client.start_server()
        try:
            await client.post("/v1/focus/start",
                              json={"task_id": 7, "label": "novel"})
            for _ in range(10):
                clock.advance(minutes=1)
                await client.post("/v1/activity", json={"idle_seconds": 0})
            clock.advance(seconds=390)
            await client.post("/v1/activity", json={"idle_seconds": 390})
            await service._tick_once()
        finally:
            await client.close()
    asyncio.run(flow())
    offer = store.newest_open_offer()
    assert offer is not None
    return offer


def test_a_phone_answer_while_still_away_removes_and_pauses(tmp_path):
    """the away case the tether exists for: remove excises the quiet and
    the clock pauses - the run banks with only the typed minutes."""
    store, clock, service = _tether_service(tmp_path)
    offer = _drift_into_offer(store, clock, service)

    async def flow():
        applied = await service._apply_tether_decision(
            {"offer_uuid": offer["offer_uuid"], "choice": "remove"})
        assert applied is True
    asyncio.run(flow())

    assert store.active_run() is None
    assert store.frozen_runs() == []
    assert store.get_offer(offer["offer_uuid"])["status"] == "applied"
    ended = [e for e in store.pending() if e["type"] == "session.ended"][0]
    # typing 600s + quiet 390s = gross 990s; the quiet comes off
    assert ended["payload"]["gross_seconds"] == 990
    assert ended["payload"]["seconds"] == 600
    applied_event = [e for e in store.pending()
                     if e["type"] == "rewind.applied"][0]
    assert applied_event["payload"]["surface"] == "tether"


def test_a_phone_answer_after_the_return_keeps_the_clock_running(tmp_path):
    """sliced-c presence rule: if the person is back and working, a late
    phone tap resolves the question but must NOT stop live work - the
    correction never becomes the interruption."""
    store, clock, service = _tether_service(tmp_path)
    offer = _drift_into_offer(store, clock, service)

    async def flow():
        client = TestClient(TestServer(service.build_app()))
        await client.start_server()
        try:
            # the comeback: the card is now showing on the desk
            clock.advance(seconds=30)
            await client.post("/v1/activity", json={"idle_seconds": 1})
            await service._tick_once()
            # ...and the phone answer arrives late
            applied = await service._apply_tether_decision(
                {"offer_uuid": offer["offer_uuid"], "choice": "keep"})
            assert applied is True
        finally:
            await client.close()
    asyncio.run(flow())

    assert store.get_offer(offer["offer_uuid"])["status"] == "kept"
    assert store.active_run() is not None          # the clock kept going


def test_first_answer_wins(tmp_path):
    """the doc's race: the card tap lands first, the tether decision
    arrives after - refused as stale, the terminal state stands."""
    store, clock, service = _tether_service(tmp_path)
    offer = _drift_into_offer(store, clock, service)
    service.offers.resolve(offer["offer_uuid"], "keep")   # the card tap

    async def flow():
        applied = await service._apply_tether_decision(
            {"offer_uuid": offer["offer_uuid"], "choice": "remove"})
        assert applied is False
    asyncio.run(flow())

    assert store.get_offer(offer["offer_uuid"])["status"] == "kept"
    assert store.excised_seconds(offer["run_id"]) == 0


def test_a_held_blocks_phone_answer_banks_at_the_freeze(tmp_path):
    """pause at the desk without answering (held), walk away, answer from
    the phone: the run banks at its freeze instant, same as the card path."""
    store, clock, service = _tether_service(tmp_path)
    offer = _drift_into_offer(store, clock, service)

    async def flow():
        client = TestClient(TestServer(service.build_app()))
        await client.start_server()
        try:
            clock.advance(seconds=30)
            await client.post("/v1/activity", json={"idle_seconds": 1})
            await service._tick_once()
            resp = await client.post("/v1/focus/pause")     # bare: holds
            assert (await resp.json())["held"]["frozen"] is True
            clock.advance(minutes=20)                       # gone again
            applied = await service._apply_tether_decision(
                {"offer_uuid": offer["offer_uuid"], "choice": "remove"})
            assert applied is True
        finally:
            await client.close()
    asyncio.run(flow())

    assert store.frozen_runs() == []
    ended = [e for e in store.pending() if e["type"] == "session.ended"][0]
    # banks at the freeze instant: the 20 late minutes never count
    assert ended["payload"]["gross_seconds"] == 600 + 390 + 30
    assert ended["payload"]["seconds"] == 600


def test_the_pump_polls_only_while_a_question_is_open(tmp_path):
    """the decision poll is gated hard on an open offer, and hands each
    fetched decision to the apply hook."""
    from aiohttp import web as aioweb

    from src.sidecar.sync import OutboxPump

    calls = {"n": 0}

    async def decisions(request):
        calls["n"] += 1
        assert request.headers["Authorization"] == "Bearer dev.tok"
        return aioweb.json_response({"decisions": [
            {"offer_uuid": "o1", "choice": "keep"},
            "garbage",
        ]})

    async def flow():
        app = aioweb.Application()
        app.router.add_get("/api/v1/sync/decisions", decisions)
        server = TestServer(app)
        await server.start_server()
        store = SidecarStore(tmp_path / "pump.db")
        try:
            store.put("device_token", "dev.tok")
            store.put("server_url", str(server.make_url("")).rstrip("/"))
            pump = OutboxPump(store)
            applied = []

            async def apply(decision):
                applied.append(decision)
                return True

            # no hooks at all: the bare pump is exactly what it was
            assert await pump.pull_decisions() == 0
            pump.decision_apply = apply
            pump.offer_gate = lambda: False
            assert await pump.pull_decisions() == 0
            assert calls["n"] == 0                # never even asked
            pump.offer_gate = lambda: True
            assert await pump.pull_decisions() == 1
            assert applied == [{"offer_uuid": "o1", "choice": "keep"}]
            await pump.close()
        finally:
            store.close()
            await server.close()
    asyncio.run(flow())


def test_a_revoked_decision_poll_parks_the_pump(tmp_path):
    """sol #72 finding 5: a 401 on the decision poll parks exactly like
    the push path - a revoked sidecar with an open offer and an empty
    outbox must not keep knocking every cycle."""
    from aiohttp import web as aioweb

    from src.sidecar.sync import OutboxPump

    async def revoked(request):
        return aioweb.json_response({"error": "revoked"}, status=401)

    async def flow():
        app = aioweb.Application()
        app.router.add_get("/api/v1/sync/decisions", revoked)
        server = TestServer(app)
        await server.start_server()
        store = SidecarStore(tmp_path / "pump.db")
        try:
            store.put("device_token", "dev.tok")
            store.put("server_url", str(server.make_url("")).rstrip("/"))
            pump = OutboxPump(store)
            pump.offer_gate = lambda: True

            async def apply(decision):
                raise AssertionError("nothing to apply on a 401")

            pump.decision_apply = apply
            assert await pump.pull_decisions() == 0
            # parked: the run loop's sync_error check now backs off, and
            # only a fresh handshake un-parks
            assert store.get("sync_error")
            await pump.close()
        finally:
            store.close()
            await server.close()
    asyncio.run(flow())
