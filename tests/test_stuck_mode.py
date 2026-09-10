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
    out = stuck.fallback(evidence(tasks=[]))
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
    out = stuck.fallback(ev)
    assert out[0]["kind"] == "step"
    assert out[0]["prepared_step"] == {"task_id": 2, "next_action": "open the draft"}
    assert out[0]["line"] == "open the draft"
    assert stuck.validate(out, ev)


def test_fallback_opens_the_smallest_untouched_task_otherwise():
    ev = evidence(tasks=[listed(1, "big", pom_estimate=4),
                         listed(2, "small", pom_estimate=1)])
    out = stuck.fallback(ev)
    assert out[0]["thing"] == {"task_id": 2}
    assert 'open "small"' in out[0]["line"]


def test_fallback_body_and_rest_read_the_evidence():
    quiet = evidence(tasks=[], minutes_since_last_run=200, in_quiet=True)
    out = stuck.fallback(quiet)
    assert out[1]["action"] == "pause_and_away" and out[1]["minutes"] == 8
    assert out[2]["line"] == stuck.FALLBACK_COPY["rest_sleep_line"]
    assert "past_quiet_hours" in out[2]["rationale_codes"]
    fresh = evidence(tasks=[], minutes_since_last_run=20, runs_today=2)
    out = stuck.fallback(fresh)
    assert out[1]["action"] == "body_here"
    assert out[2]["line"] == stuck.FALLBACK_COPY["rest_here_line"]
    assert out[2]["rationale_codes"] == []


# --- the evidence read (5.2) --------------------------------------------------

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
    assert store.settle(ep["episode_id"], 1, stuck.fallback(evidence()), "fallback") is None


def test_fallback_becomes_terminal_for_the_model(store):
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="home")
    fb = store.settle(ep["episode_id"], 1, stuck.fallback(evidence()), "fallback")
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
    assert store.settle(ep["episode_id"], 1, stuck.fallback(evidence()), "fallback") is None
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
    ep, _ = store.open(U1, request_uuid=str(uuid_mod.uuid4()), surface="companion")

    class SlowHouse:
        async def handle(self, stimulus):
            assert stimulus.kind == "stuck"
            assert stimulus.extras["stuck_episode_id"] == ep["episode_id"]
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
    # step/body/rest were turned down; the task-free ladder is company/body/rest
    # so only company survives... plus the honest end when nothing does
    assert after["status"] == stuck.FALLBACK
    assert all(p["kind"] not in ("step", "body") for p in after["proposals"])
