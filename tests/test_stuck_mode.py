"""stuck mode, the server half (docs/STUCK_MODE_DESIGN.md sections 5-7,
build slice 1): the validator's promises, the fallback ladder's shape, the
episode's state machine, and that nothing outside the stuck tables moves
until a proposal is accepted."""
import asyncio
import sys
import tempfile
import uuid as uuid_mod
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.database as db_mod  # noqa: E402
from src.database.models import (  # noqa: E402
    Base, Plan, StuckEpisode, StuckReaction, Task, User)
from src.services import focus_day, stuck  # noqa: E402
from src.services import stuck_turns  # noqa: E402
from src.services.workspace import agenda as agenda_mod  # noqa: E402
from src.services.workspace.store import WorkspaceStore  # noqa: E402

U1 = "u1"
U2 = "u2"
# a june tuesday in US/Pacific; local midnight is 07:00 utc
LOCAL_MIDNIGHT = datetime(2026, 6, 16, 7, 0)
TODAY = date(2026, 6, 16)
NOW = LOCAL_MIDNIGHT + timedelta(hours=15, minutes=32)   # 3:32pm local
NOW_LOCAL = datetime(2026, 6, 16, 15, 32)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    monkeypatch.setattr(focus_day, "utc_now", lambda: NOW)
    monkeypatch.setattr(agenda_mod, "utc_now", lambda: NOW)
    monkeypatch.setattr(stuck, "utc_now", lambda: NOW)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="megan", timezone="US/Pacific"))
        s.add(User(uuid=U2, preferred_name="guest", timezone="UTC"))
        s.commit()
    yield TestSession
    engine.dispose()


def task(db, title, *, user=U1, scheduled=TODAY, status="todo", **cols):
    with db() as s:
        row = Task(user_uuid=user, title=title, status=status,
                   scheduled=scheduled, **cols)
        s.add(row)
        s.commit()
        return row.id


def plan(db, title, why):
    with db() as s:
        row = Plan(user_uuid=U1, title=title, helper="pip", status="active",
                   why=why)
        s.add(row)
        s.commit()
        return row.id


def evidence(**over) -> stuck.Evidence:
    ev = stuck.Evidence(now_local=NOW_LOCAL)
    for k, v in over.items():
        setattr(ev, k, v)
    return ev


def listed(task_id, title="write the thing", next_action=None, **over):
    row = {"id": task_id, "title": title, "priority": None, "scheduled": None,
           "pom_estimate": 1, "next_action": next_action, "banked_seconds": 0,
           "reschedules": 0, "set_aside_count": 0, "plan_id": None,
           "plan_title": None, "signals": []}
    row.update(over)
    return row


def proposal(kind="step", **over):
    base = {
        "step": {"kind": "step", "action": "start_task",
                 "line": "open the doc and write one sentence.",
                 "enough": "one sentence, any sentence",
                 "thing": {"task_id": 1},
                 "prepared_step": {"task_id": 1, "next_action": "write one sentence"}},
        "body": {"kind": "body", "action": "body_here",
                 "line": "stand up and stretch.", "enough": "you stood up"},
        "rest": {"kind": "rest", "action": "rest_here",
                 "line": "close the list for now.", "enough": "you closed this"},
        "company": {"kind": "company", "action": "start_company",
                    "line": "sit here five minutes.", "enough": "five minutes"},
        "sound": {"kind": "sound", "action": "point_to_sound",
                  "line": "put on the rain playlist.", "enough": "it's playing"},
    }[kind]
    return {**base, **over}


# --- the validator (5.3) --------------------------------------------------------

def test_validate_accepts_three_distinct_kinds_and_normalizes():
    ev = evidence(tasks=[listed(1)])
    out = stuck.validate([proposal("step"), proposal("body"), proposal("rest")], ev)
    assert [p["kind"] for p in out] == ["step", "body", "rest"]
    assert out[0]["minutes"] == 2 and out[2]["minutes"] is None
    assert out[0]["why_register"] == "matters"
    assert "proposal_id" not in out[0]      # the store assigns ids


@pytest.mark.parametrize("bad,reason", [
    ([proposal("step"), proposal("body")], "exactly 3"),
    ([proposal("step"), proposal("step", line="another"), proposal("rest")], "repeats"),
    ([proposal("step", action="rest_here"), proposal("body"), proposal("rest")],
     "does not match"),
    ([proposal("step", thing={"task_id": 99},
               prepared_step={"task_id": 99, "next_action": "x"}),
      proposal("body"), proposal("rest")], "not on today's list"),
    ([proposal("step", prepared_step=None), proposal("body"), proposal("rest")],
     "needs a prepared_step"),
    ([proposal("step", line="x" * 141), proposal("body"), proposal("rest")],
     "over 140"),
    ([proposal("step", minutes=7), proposal("body"), proposal("rest")],
     "minutes must be"),
    ([proposal("step"), proposal("body", rationale_codes=["late_last_night"]),
      proposal("rest")], "not in the brief"),
    ([proposal("step", thing={"task_id": 1, "label": "two"}), proposal("body"),
      proposal("rest")], "exactly one"),
])
def test_validate_refuses_with_the_reason(bad, reason):
    ev = evidence(tasks=[listed(1)])
    with pytest.raises(stuck.ProposalError, match=reason):
        stuck.validate(bad, ev)


def test_validate_refuses_preparation_on_rest():
    ev = evidence(tasks=[listed(1)])
    with pytest.raises(stuck.ProposalError, match="rest changes nothing"):
        stuck.validate([proposal("step"), proposal("body"),
                        proposal("rest", prepared_step={"task_id": 1,
                                                        "next_action": "sneaky"})],
                       ev)


def test_validate_refuses_kinds_already_turned_down():
    ev = evidence(tasks=[listed(1)])
    with pytest.raises(stuck.ProposalError, match="turned down"):
        stuck.validate([proposal("step"), proposal("body"), proposal("rest")],
                       ev, rejected_kinds=["body"])


def test_validate_checks_evidence_codes_against_the_snapshot():
    ev = evidence(tasks=[listed(1)], late_last_night_seconds=45 * 60)
    out = stuck.validate([proposal("step"), proposal("body"),
                          proposal("rest", rationale_codes=["late_last_night"])], ev)
    assert out[2]["rationale_codes"] == ["late_last_night"]


def test_no_step_when_the_list_is_empty():
    ev = evidence(tasks=[])
    assert stuck.CODE_NO_SUITABLE_TASK in ev.codes
    with pytest.raises(stuck.ProposalError):
        stuck.validate([proposal("step"), proposal("body"), proposal("rest")], ev)
    out = stuck.validate([proposal("company"), proposal("body"), proposal("rest")], ev)
    assert out[0]["kind"] == "company"


# --- the fallback ladder (section 6) ------------------------------------------

def test_fallback_is_three_kinds_and_task_free_when_it_must_be():
    out, exhausted = stuck.fallback(evidence(tasks=[]))
    assert not exhausted
    assert [p["kind"] for p in out] == ["company", "body", "rest"]
    assert all(p["thing"] is None for p in out)
    # the ladder's own output passes the validator it will be judged beside
    assert stuck.validate(out, evidence(tasks=[]))


def test_fallback_prefers_the_flagged_task_with_a_first_piece():
    ev = evidence(tasks=[
        listed(1, "big scary", next_action=None, pom_estimate=4),
        listed(2, "moved thrice", next_action="open the draft",
               signals=["moved 3 times"]),
    ])
    out, _ = stuck.fallback(ev)
    assert out[0]["kind"] == "step"
    assert out[0]["prepared_step"] == {"task_id": 2, "next_action": "open the draft"}
    assert out[0]["line"] == "open the draft"
    assert stuck.validate(out, ev)


def test_fallback_opens_the_smallest_untouched_task_otherwise():
    ev = evidence(tasks=[listed(1, "big", pom_estimate=4),
                         listed(2, "small", pom_estimate=1)])
    out, _ = stuck.fallback(ev)
    assert out[0]["thing"] == {"task_id": 2}
    assert 'open "small"' in out[0]["line"]


def test_fallback_starts_from_the_row_the_button_was_pressed_on():
    ev = evidence(tasks=[listed(1, "small", pom_estimate=1),
                         listed(2, "the one i meant", pom_estimate=4,
                                next_action="reopen the outline")],
                  focus_task_id=2)
    out, _ = stuck.fallback(ev)
    assert out[0]["thing"] == {"task_id": 2}
    assert out[0]["line"] == "reopen the outline"


def test_fallback_second_generation_is_three_fresh_kinds_or_exhausted():
    # a rich day: two tasks and a last run -> thread and switch are honest
    ev = evidence(tasks=[listed(1, "alpha", pom_estimate=1),
                         listed(2, "beta", pom_estimate=2)],
                  last_run_task_id=2, last_run_label="beta: outline")
    out, exhausted = stuck.fallback(ev, exclude=["step", "body", "rest"])
    assert not exhausted
    assert [p["kind"] for p in out] == ["company", "thread", "switch"]
    assert out[1]["thing"] == {"task_id": 2} and "beta: outline" in out[1]["line"]
    assert out[2]["thing"] == {"task_id": 2}       # the other task than the step's
    assert stuck.validate(out, ev, rejected_kinds=["step", "body", "rest"])
    # a bare day: nothing ran, one task -> only company and sound remain
    bare = evidence(tasks=[listed(1)])
    out, exhausted = stuck.fallback(bare, exclude=["step", "body", "rest"])
    assert exhausted and [p["kind"] for p in out] == ["company", "sound"]


def test_fallback_body_and_rest_read_the_evidence():
    quiet = evidence(tasks=[], minutes_since_last_run=200, in_quiet=True)
    out, _ = stuck.fallback(quiet)
    assert out[1]["action"] == "pause_and_away" and out[1]["minutes"] == 8
    assert out[2]["line"] == stuck.FALLBACK_COPY["rest_sleep_line"]
    assert "past_quiet_hours" in out[2]["rationale_codes"]
    fresh = evidence(tasks=[], minutes_since_last_run=20, runs_today=2)
    out, _ = stuck.fallback(fresh)
    assert out[1]["action"] == "body_here"
    assert out[2]["line"] == stuck.FALLBACK_COPY["rest_here_line"]
    assert out[2]["rationale_codes"] == []


# --- the evidence read (5.2) --------------------------------------------------

def test_late_last_night_is_only_last_nights_window(db, monkeypatch):
    from src.services.focus_day import Run
    def fd_with(runs_today, runs_yesterday):
        class FD:
            day = TODAY
            now = NOW_LOCAL
            runs = runs_today
        snaps = {TODAY - timedelta(days=1): type("Y", (), {"runs": runs_yesterday})()}
        monkeypatch.setattr(focus_day, "snapshot",
                            lambda user, day=None: snaps[day])
        return FD()
    def run_at(end_local, minutes, task_id=1):
        return Run(task_id=task_id, label="x", seconds=minutes * 60,
                   ended_at=end_local, reason="paused")
    # 23:10 -> 00:10 last night: 60 min inside the window
    last_night = run_at(datetime(2026, 6, 16, 0, 10), 60)
    assert stuck._late_seconds_around(fd_with([], [last_night]), U1) == 3600
    # 02:00 the night BEFORE last (yesterday's 2am run) is not last night
    before_last = run_at(datetime(2026, 6, 15, 2, 0), 60)
    assert stuck._late_seconds_around(fd_with([], [before_last]), U1) == 0
    # this morning 05:30 -> 06:30: only the half hour before 06:00 counts
    dawn = run_at(datetime(2026, 6, 16, 6, 30), 60)
    assert stuck._late_seconds_around(fd_with([dawn], []), U1) == 1800


def test_gather_reads_the_list_the_plan_why_and_skips_parked(db):
    pid = plan(db, "willowden", "so the next stuck afternoon has a door")
    t1 = task(db, "stuck-mode design", plan_id=pid, next_action="one sentence")
    task(db, "parked today", set_aside_on=TODAY)
    task(db, "not mine", user=U2)
    ev = stuck.gather(U1)
    assert ev.task_ids == {t1}
    assert ev.plans[pid]["why"] == "so the next stuck afternoon has a door"
    assert ev.clock == "idle" and ev.runs_today == 0
    brief = stuck.render_brief(ev, surface="companion")
    assert f'#{t1} "stuck-mode design"' in brief
    assert "their why, in their words" in brief
    assert "no runs today" in brief


def test_brief_names_what_was_turned_down():
    brief = stuck.render_brief(evidence(tasks=[]), rejected_kinds=["step", "body"])
    assert "do not offer these kinds again" in brief and "step, body" in brief
    assert "nothing on today's list" in brief


# --- the episode store (5.1 + 7) ----------------------------------------------

@pytest.fixture()
def store(db):
    return stuck.StuckStore()


def _settle_model(store, ep, ev=None, kinds=("step", "body", "rest")):
    ev = ev or evidence(tasks=[listed(1)])
    props = stuck.validate([proposal(k) for k in kinds], ev)
    return store.settle(ep["episode_id"], ep["generation"], props, "model")


def test_open_is_idempotent_per_device_and_request(store):
    req = str(uuid_mod.uuid4())
    ep, created = store.open(U1, request_uuid=req, surface="companion", device_id=7)
    again, created2 = store.open(U1, request_uuid=req, surface="companion", device_id=7)
    assert created and not created2
    assert ep["episode_id"] == again["episode_id"]
    assert ep["status"] == stuck.THINKING and ep["generation"] == 1
    other, created3 = store.open(U1, request_uuid=req, surface="companion", device_id=8)
    assert created3 and other["episode_id"] != ep["episode_id"]


def test_open_refuses_a_task_that_is_not_theirs(store, db):
    theirs = task(db, "not mine", user=U2)
    with pytest.raises(ValueError, match="task not found"):
        store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home",
                   task_id=theirs)


def test_settle_assigns_ids_and_refuses_late_or_stale_writes(store):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    ready = _settle_model(store, ep)
    assert ready["status"] == stuck.READY and ready["source"] == "model"
    assert len(ready["proposals"]) == 3
    assert all(p["proposal_id"] and p["generation"] == 1 for p in ready["proposals"])
    assert len({p["proposal_id"] for p in ready["proposals"]}) == 3
    # a second write for the same generation is refused - the card stands
    assert _settle_model(store, ep) is None
    # so is a fallback arriving after the model
    assert store.settle(ep["episode_id"], 1, stuck.fallback(evidence())[0], "fallback") is None


def test_fallback_becomes_terminal_for_the_model(store):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    fb = store.settle(ep["episode_id"], 1, stuck.fallback(evidence())[0], "fallback")
    assert fb["status"] == stuck.FALLBACK
    assert _settle_model(store, ep) is None      # the late model write is refused


def test_generating_and_browsing_proposals_mutates_nothing(store, db):
    t1 = task(db, "the thing", next_action="the old scope")
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    ev = evidence(tasks=[listed(t1)])
    ready = store.settle(ep["episode_id"], 1, stuck.validate(
        [proposal("step", thing={"task_id": t1},
                  prepared_step={"task_id": t1, "next_action": "a NEW step"}),
         proposal("body"), proposal("rest")], ev), "model")
    first = ready["proposals"][0]["proposal_id"]
    # rotate once, then rest: still nothing outside the stuck tables moved
    store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                generation=1, reaction="different", proposal_id=first)
    after, outcome = store.react(U1, ep["episode_id"],
                                 request_uuid=str(uuid_mod.uuid4()),
                                 generation=1, reaction="too_much")
    assert outcome == "rested" and after["status"] == stuck.RESTED
    with db() as s:
        row = s.get(Task, t1)
        assert row.next_action == "the old scope"
        assert row.set_aside_on is None and row.status == "todo"


def test_accept_claims_and_writes_the_one_next_action_atomically(store, db):
    t1 = task(db, "the thing")
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    ev = evidence(tasks=[listed(t1)])
    ready = store.settle(ep["episode_id"], 1, stuck.validate(
        [proposal("step", thing={"task_id": t1},
                  prepared_step={"task_id": t1, "next_action": "  write   one sentence "}),
         proposal("body"), proposal("rest")], ev), "model")
    step = ready["proposals"][0]
    req = str(uuid_mod.uuid4())
    done, outcome = store.react(U1, ep["episode_id"], request_uuid=req,
                                generation=1, reaction="accepted",
                                proposal_id=step["proposal_id"])
    assert outcome == "accepted" and done["status"] == stuck.ACCEPTED
    ex = done["execution"]
    assert ex["action"] == "start_task" and ex["task_id"] == t1
    assert ex["next_action"] == "write one sentence" and ex["minutes"] == 2
    assert ex["execution_id"]
    with db() as s:
        assert s.get(Task, t1).next_action == "write one sentence"
    # the same request replays the same execution id - never a second block
    replay, outcome2 = store.react(U1, ep["episode_id"], request_uuid=req,
                                   generation=1, reaction="accepted",
                                   proposal_id=step["proposal_id"])
    assert outcome2 == "replay"
    assert replay["execution"]["execution_id"] == ex["execution_id"]
    # and a fresh accept on a settled episode is refused
    with pytest.raises(ValueError, match="settled"):
        store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                    generation=1, reaction="accepted",
                    proposal_id=step["proposal_id"])


def test_accept_refuses_a_task_finished_or_parked_since_the_card(store, db):
    t1 = task(db, "the thing")
    t2 = task(db, "the other")
    ev = evidence(tasks=[listed(t1), listed(t2)])
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    ready = store.settle(ep["episode_id"], 1, stuck.validate(
        [proposal("step", thing={"task_id": t1},
                  prepared_step={"task_id": t1, "next_action": "a step"}),
         {"kind": "switch", "action": "start_task", "line": "open the other.",
          "enough": "you see it", "thing": {"task_id": t2},
          "prepared_step": {"task_id": t2, "next_action": "open it"}},
         proposal("rest")], ev), "model")
    step, switch = ready["proposals"][0], ready["proposals"][1]
    with db() as s:
        s.get(Task, t1).status = "done"
        s.get(Task, t2).set_aside_on = TODAY
        s.commit()
    with pytest.raises(ValueError, match="finished since the card"):
        store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                    generation=1, reaction="accepted", proposal_id=step["proposal_id"])
    with pytest.raises(ValueError, match="set aside since the card"):
        store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                    generation=1, reaction="accepted", proposal_id=switch["proposal_id"])
    # the refused accepts rolled back: no reaction rows, card still showing
    with db() as s:
        assert s.query(StuckReaction).count() == 0
        assert s.get(Task, t1).next_action is None
    assert store.get(U1, ep["episode_id"])["status"] == stuck.READY


def test_accepting_rest_writes_nothing_even_with_a_task_named(store, db):
    t1 = task(db, "the thing", next_action="old")
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    ev = evidence(tasks=[listed(t1)])
    ready = store.settle(ep["episode_id"], 1, stuck.validate(
        [proposal("step", thing={"task_id": t1},
                  prepared_step={"task_id": t1, "next_action": "new"}),
         proposal("body"),
         proposal("rest", thing={"task_id": t1})], ev), "model")
    rest = ready["proposals"][2]
    done, outcome = store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                                generation=1, reaction="accepted",
                                proposal_id=rest["proposal_id"])
    assert outcome == "accepted" and done["execution"]["action"] == "rest_here"
    with db() as s:
        assert s.get(Task, t1).next_action == "old"


def test_conflicting_reactions_from_two_windows(store, db):
    """both windows read the same card; the second transition finds the
    row already moved and conflicts, its reaction rolled back."""
    t1 = task(db, "the thing")
    ev = evidence(tasks=[listed(t1)])
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    ready = _settle_model(store, ep, ev)
    pid = ready["proposals"][1]["proposal_id"]     # body: no task to re-check
    # window A accepts
    store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                generation=1, reaction="accepted", proposal_id=pid)
    # window B, holding the stale card, tries too_much -> settled (terminal)
    with pytest.raises(ValueError, match="settled"):
        store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                    generation=1, reaction="too_much")
    # and the conditional guard itself: force the row past the read
    ep2, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    ready2 = _settle_model(store, ep2, ev)
    pid2 = ready2["proposals"][0]["proposal_id"]
    original = store._prepare
    def sneak(db_, user, proposal):
        # another window closes the episode between B's read and B's write
        with db() as s:
            s.query(StuckEpisode).filter(
                StuckEpisode.episode_uuid == ep2["episode_id"]).update(
                {StuckEpisode.status: stuck.CLOSED, StuckEpisode.closed_at: NOW})
            s.commit()
        original(db_, user, proposal)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(store, "_prepare", sneak)
    try:
        with pytest.raises(ValueError, match="changed under you"):
            store.react(U1, ep2["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                        generation=1, reaction="accepted", proposal_id=pid2)
    finally:
        monkey.undo()
    with db() as s:
        assert s.query(StuckReaction).filter(
            StuckReaction.reaction == "accepted").count() == 1   # window A's only


def test_settle_can_mark_a_generation_exhausted_and_list_thinking(store):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    assert [e["episode_id"] for _, e in store.list_thinking()] == [ep["episode_id"]]
    thin, exhausted = stuck.fallback(evidence(tasks=[]), exclude=["step", "body", "rest"])
    assert exhausted and len(thin) == 2
    card = store.settle(ep["episode_id"], 1, thin, "fallback", exhausted=True)
    assert card["exhausted"] is True and card["status"] == stuck.FALLBACK
    assert store.list_thinking() == []


def test_reactions_check_generation_and_proposal(store):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    with pytest.raises(ValueError, match="no card"):
        store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                    generation=1, reaction="different", proposal_id="x")
    ready = _settle_model(store, ep)
    pid = ready["proposals"][0]["proposal_id"]
    with pytest.raises(ValueError, match="stale generation"):
        store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                    generation=2, reaction="different", proposal_id=pid)
    with pytest.raises(ValueError, match="unknown proposal"):
        store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                    generation=1, reaction="different", proposal_id="nope")
    with pytest.raises(ValueError, match="not found"):
        store.react(U2, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                    generation=1, reaction="closed")


def test_third_different_owes_one_new_generation_then_exhausts(store):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    ready = _settle_model(store, ep)
    outcomes = []
    for p in ready["proposals"]:
        cur, outcome = store.react(U1, ep["episode_id"],
                                   request_uuid=str(uuid_mod.uuid4()),
                                   generation=1, reaction="different",
                                   proposal_id=p["proposal_id"])
        outcomes.append(outcome)
    assert outcomes == ["recorded", "recorded", "regenerate"]
    assert cur["status"] == stuck.THINKING and cur["generation"] == 2
    assert cur["rejected_kinds"] == ["step", "body", "rest"]
    assert cur["proposals"] == []          # the new generation has no card yet
    # generation two: the other kinds; a model write for gen 1 is now stale
    assert store.settle(ep["episode_id"], 1, stuck.fallback(evidence())[0], "fallback") is None
    ev = evidence(tasks=[listed(1)])
    gen2 = store.settle(ep["episode_id"], 2, stuck.validate(
        [proposal("company"), proposal("sound"),
         {"kind": "thread", "action": "start_task",
          "line": "reopen the draft where you stopped.", "enough": "you see it",
          "thing": {"task_id": 1},
          "prepared_step": {"task_id": 1, "next_action": "reopen the draft"}}],
        ev, rejected_kinds=cur["rejected_kinds"]), "model")
    assert gen2["status"] == stuck.READY and gen2["generation"] == 2
    for p in gen2["proposals"]:
        cur, outcome = store.react(U1, ep["episode_id"],
                                   request_uuid=str(uuid_mod.uuid4()),
                                   generation=2, reaction="different",
                                   proposal_id=p["proposal_id"])
    assert outcome == "exhausted" and cur["exhausted"] is True
    assert cur["status"] == stuck.READY        # the card stays; the client shows rest


def test_recent_summaries_are_raw_evidence(store):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    ready = _settle_model(store, ep)
    body = next(p for p in ready["proposals"] if p["kind"] == "body")
    store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                generation=1, reaction="accepted", proposal_id=body["proposal_id"])
    store.record_outcome(U1, ep["episode_id"], banked_seconds=540,
                         stopped_at_boundary=True)
    ep2, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    lines = store.recent_summaries(U1, exclude_uuid=ep2["episode_id"])
    assert len(lines) == 1
    assert "accepted body" in lines[0] and "banked 9 min" in lines[0]
    assert "stopped at the boundary" in lines[0]


def test_sweep_closes_abandoned_episodes(store, db, monkeypatch):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    ready_ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    _settle_model(store, ready_ep)
    assert store.sweep(ttl_minutes=30, now=NOW + timedelta(minutes=10)) == 0
    assert store.sweep(ttl_minutes=30, now=NOW + timedelta(minutes=31)) == 2
    assert store.get(U1, ep["episode_id"])["status"] == stuck.FAILED
    assert store.get(U1, ready_ep["episode_id"])["status"] == stuck.CLOSED
    with db() as s:
        assert s.query(StuckReaction).count() == 0


# --- the turn runner (2.1): the ladder stands in when the house can't ----------

def test_turn_falls_back_when_there_is_no_orchestrator(store, db):
    task(db, "small thing", pom_estimate=1)
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    turns = stuck_turns.StuckTurns(store=store, orchestrator=lambda: None)
    run(turns.run(U1, ep))
    after = store.get(U1, ep["episode_id"])
    assert after["status"] == stuck.FALLBACK and after["source"] == "fallback"
    assert [p["kind"] for p in after["proposals"]] == ["step", "body", "rest"]


def test_turn_times_out_into_the_fallback_and_a_late_write_is_refused(store, db):
    tid = task(db, "the one i meant")
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion",
                       task_id=tid)

    class SlowHouse:
        async def handle(self, stimulus):
            assert stimulus.kind == "stuck"
            assert stimulus.extras["stuck_episode_id"] == ep["episode_id"]
            assert stimulus.extras["stuck_task_id"] == ep["task_id"]
            await asyncio.sleep(5)

    turns = stuck_turns.StuckTurns(store=store, orchestrator=lambda: SlowHouse(),
                                   timeout=0.05)
    run(turns.run(U1, ep))
    after = store.get(U1, ep["episode_id"])
    assert after["status"] == stuck.FALLBACK
    assert _settle_model(store, ep) is None


def test_turn_leaves_a_model_card_alone(store, db):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")

    class House:
        async def handle(self, stimulus):
            # the tool wrote during the turn
            _settle_model(store, ep)

    turns = stuck_turns.StuckTurns(store=store, orchestrator=lambda: House())
    run(turns.run(U1, ep))
    after = store.get(U1, ep["episode_id"])
    assert after["status"] == stuck.READY and after["source"] == "model"


def test_second_generation_drops_turned_down_kinds_from_the_ladder(store, db):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    ready = _settle_model(store, ep)
    for p in ready["proposals"]:
        cur, _ = store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                             generation=1, reaction="different",
                             proposal_id=p["proposal_id"])
    turns = stuck_turns.StuckTurns(store=store, orchestrator=lambda: None)
    run(turns.run(U1, cur))
    after = store.get(U1, ep["episode_id"])
    # step/body/rest were turned down; with nothing on the list the ladder
    # has only company and sound left - a thin card, honestly marked
    assert after["status"] == stuck.FALLBACK
    assert [p["kind"] for p in after["proposals"]] == ["company", "sound"]
    assert after["exhausted"] is True


def test_ensure_runs_at_most_one_runner_per_generation(store, db):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    turns = stuck_turns.StuckTurns(store=store, orchestrator=lambda: None)

    async def scenario():
        first = turns.ensure(U1, ep)
        second = turns.ensure(U1, ep)          # the poll, a moment later
        assert first and not second
        await asyncio.gather(*turns._running.values())
        assert store.get(U1, ep["episode_id"])["status"] == stuck.FALLBACK
        assert not turns.ensure(U1, store.get(U1, ep["episode_id"]))   # settled
    run(scenario())


def test_resume_all_picks_up_persisted_thinking_episodes(store, db):
    """a restart between 'episode committed' and 'turn spawned' leaves a
    thinking row with no runner; resume_all is the durable claim."""
    a, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    b, _ = store.open(U2, request_uuid=str(uuid_mod.uuid4()), surface="home")
    turns = stuck_turns.StuckTurns(store=store, orchestrator=lambda: None)

    async def scenario():
        assert await turns.resume_all() == 2
        await asyncio.gather(*turns._running.values())
        assert await turns.resume_all() == 0
    run(scenario())
    assert store.get(U1, a["episode_id"])["status"] == stuck.FALLBACK
    assert store.get(U2, b["episode_id"])["status"] == stuck.FALLBACK



# --- outcomes folded from the device stream (section 7) ------------------------

from src.database.models import Device  # noqa: E402
from src.services import device_sync, focus_flow  # noqa: E402
from src.web import device_auth  # noqa: E402


@pytest.fixture()
def device(db):
    code = device_auth.mint_device_link_code(U1)
    device_uuid, token = device_auth.link_device(code, "desk")
    with db() as s:
        pk = s.query(Device.id).filter(Device.device_uuid == device_uuid).scalar()
    return {"pk": pk, "seq": 0}


def _land(device, etype, payload, occurred):
    device["seq"] += 1
    result = device_sync.apply_events(device["pk"], [{
        "id": str(uuid_mod.uuid4()), "seq": device["seq"], "type": etype,
        "payload": payload, "occurred_at": occurred.isoformat()}])
    assert not result.rejected
    focus_flow.drain()


def _accepted_episode(store, db):
    t1 = task(db, "the thing")
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")
    ready = store.settle(ep["episode_id"], 1, stuck.validate(
        [proposal("step", thing={"task_id": t1},
                  prepared_step={"task_id": t1, "next_action": "one sentence"}),
         proposal("body"), proposal("rest")], evidence(tasks=[listed(t1)])), "model")
    done, _ = store.react(U1, ep["episode_id"], request_uuid=str(uuid_mod.uuid4()),
                          generation=1, reaction="accepted",
                          proposal_id=ready["proposals"][0]["proposal_id"])
    return done


def test_outcomes_fold_from_the_run_that_carries_the_execution(store, db, device):
    done = _accepted_episode(store, db)
    ids = {"episode_id": done["episode_id"],
           "execution_id": done["execution"]["execution_id"], "run_mode": "stuck"}
    _land(device, "session.started",
          {"task_id": 1, "label": "the thing: one sentence", "target_minutes": 2.0,
           **ids}, NOW)
    _land(device, "session.ended",
          {"task_id": 1, "label": "the thing: one sentence", "seconds": 130,
           "reason": "boundary", "gross_seconds": 130, "excised_seconds": 0,
           "target_minutes": 2.0, **ids}, NOW + timedelta(minutes=2, seconds=10))
    after = store.get(U1, done["episode_id"])
    with db() as s:
        row = s.query(StuckEpisode).filter(
            StuckEpisode.episode_uuid == done["episode_id"]).one()
        assert row.run_ref["device_id"] == device["pk"] and row.run_ref["event_uuid"]
        assert row.outcome == {"started": True, "banked_seconds": 130,
                               "reason": "boundary", "stopped_at_boundary": True,
                               "kept_going": False}
    assert after["status"] == stuck.ACCEPTED
    ep2, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    lines = store.recent_summaries(U1, exclude_uuid=ep2["episode_id"])
    assert "banked 2 min" in lines[0] and "stopped at the boundary" in lines[0]


def test_kept_going_is_read_from_the_target(store, db, device):
    done = _accepted_episode(store, db)
    ids = {"episode_id": done["episode_id"],
           "execution_id": done["execution"]["execution_id"], "run_mode": "stuck"}
    _land(device, "session.ended",
          {"task_id": 1, "label": "x", "seconds": 900, "reason": "paused",
           "target_minutes": 2.0, **ids}, NOW + timedelta(minutes=15))
    with db() as s:
        row = s.query(StuckEpisode).filter(
            StuckEpisode.episode_uuid == done["episode_id"]).one()
        assert row.outcome["kept_going"] is True
        assert row.outcome["stopped_at_boundary"] is False


def test_plain_and_foreign_runs_never_fold(store, db, device):
    done = _accepted_episode(store, db)
    # a plain run: no ids, not ours
    _land(device, "session.ended",
          {"task_id": 1, "label": "x", "seconds": 600, "reason": "paused"}, NOW)
    # a run claiming the episode with the wrong execution id
    _land(device, "session.ended",
          {"task_id": 1, "label": "x", "seconds": 600, "reason": "boundary",
           "episode_id": done["episode_id"], "execution_id": "not-it"}, NOW)
    with db() as s:
        row = s.query(StuckEpisode).filter(
            StuckEpisode.episode_uuid == done["episode_id"]).one()
        assert row.outcome is None and row.run_ref is None
