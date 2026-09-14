"""WorkspaceStore invariants (docs/NATIVE_WORKSPACE_DESIGN.md sections 2-3).

covers: the section-2.0 lifecycle convention (closed_at stamping/clearing,
open-by-default listing), plans.last_activity_at side effects, goal/plan
consistency, reschedule accounting, occasion recurrence roll-forward, the
name-or-id resolution ladder, public-id parsing, vocab round-trips, and -
in its own class, not as an afterthought - cross-user isolation.
"""
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from src.database.models import Base, User
from src.services.workspace import vocab
from src.services.workspace.store import WorkspaceStore

U1, U2 = "user-one", "user-two"


@pytest.fixture()
def store(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="dain"))
        s.add(User(uuid=U2, preferred_name="other"))
        s.commit()
    yield WorkspaceStore()
    engine.dispose()


def _plan(store, user=U1, title="finish the album", helper="aria", **kw):
    return store.create_plan(user, title, helper, **kw)


# --- lifecycle convention (section 2.0) --------------------------------------


def test_closing_stamps_closed_at_and_reopening_clears_it(store):
    p = _plan(store)
    assert p["closed_at"] is None
    p = store.update_plan(U1, p["id"], status="complete")
    assert p["closed_at"] is not None
    p = store.update_plan(U1, p["id"], status="active")
    assert p["closed_at"] is None


def test_released_is_a_distinct_terminal_ending(store):
    p = _plan(store)
    p = store.update_plan(U1, p["id"], status="released")
    assert p["status"] == "released" and p["closed_at"] is not None


def test_every_closable_entity_stamps_closed_at(store):
    p = _plan(store)
    g = store.create_goal(U1, p["id"], "master track 1")
    t = store.create_task(U1, "bounce stems")
    c = store.create_cycle(U1, "cycle 12")
    n = store.jot(U1, "bridge idea")
    assert store.update_goal(U1, g["id"], status="renegotiated")["closed_at"] is not None
    assert store.update_task(U1, t["id"], status="deprioritized")["closed_at"] is not None
    assert store.update_cycle(U1, c["id"], status="complete")["closed_at"] is not None
    assert store.update_note(U1, n["id"], status="archived")["closed_at"] is not None


def test_lists_default_to_open_items_only(store):
    p = _plan(store)
    t_open = store.create_task(U1, "open task", plan_id=p["id"])
    t_done = store.create_task(U1, "done task", plan_id=p["id"])
    store.update_task(U1, t_done["id"], status="done")

    listed = {t["id"] for t in store.list_tasks(U1)}
    assert listed == {t_open["id"]}
    everything = {t["id"] for t in store.list_tasks(U1, include_closed=True)}
    assert everything == {t_open["id"], t_done["id"]}
    # asking for a closed status explicitly shows those rows
    done_only = store.list_tasks(U1, status="Done")
    assert [t["id"] for t in done_only] == [t_done["id"]]


def test_invalid_status_names_the_valid_options(store):
    p = _plan(store)
    with pytest.raises(ValueError, match="Released"):
        store.update_plan(U1, p["id"], status="abandoned")


# --- vocab round-trips -------------------------------------------------------


def test_legacy_notion_display_strings_canonicalize(store):
    assert vocab.canonical_status("task", "To do") == "todo"
    assert vocab.canonical_status("task", "IN PROGRESS") == "in_progress"
    assert vocab.canonical_status("plan", "Not started") == "proposed"
    assert vocab.canonical_status("plan", "recurring") == "active"
    assert vocab.canonical_status("plan", "Done") == "complete"
    assert vocab.display("todo") == "To do"
    assert vocab.display("in_progress") == "In progress"


def test_public_id_render_and_parse(store):
    assert vocab.public_id("task", 42) == "t42"
    assert vocab.parse_public_id("t42") == ("task", 42)
    assert vocab.parse_public_id("CI4") == ("checkin", 4)   # ci wins over c
    assert vocab.parse_public_id("c4") == ("cycle", 4)
    assert vocab.parse_public_id("p") is None
    assert vocab.parse_public_id("x9") is None
    assert vocab.parse_public_id("finish the album") is None


# --- last_activity_at side effects -------------------------------------------


def _activity(store, plan_id):
    return store.get_plan(U1, plan_id)["last_activity_at"]


def test_related_writes_stamp_plan_activity(store):
    p = _plan(store)
    stamps = [_activity(store, p["id"])]
    assert stamps[0] is not None  # creation itself stamps

    g = store.create_goal(U1, p["id"], "goal")
    stamps.append(_activity(store, p["id"]))
    store.create_task(U1, "task", plan_id=p["id"])
    stamps.append(_activity(store, p["id"]))
    store.log_win(U1, "did a thing", date.today(), "aria", plan_id=p["id"])
    stamps.append(_activity(store, p["id"]))
    store.jot(U1, "idea", plan_id=p["id"])
    stamps.append(_activity(store, p["id"]))
    store.log_checkin(U1, date.today(), "adhoc", "chordial", plan_ids=[p["id"]])
    stamps.append(_activity(store, p["id"]))
    store.update_goal(U1, g["id"], status="in_progress")
    stamps.append(_activity(store, p["id"]))

    assert stamps == sorted(stamps), "each related write must move the stamp forward"
    assert len(set(stamps)) == len(stamps), "every write path must stamp"


def test_unrelated_writes_do_not_touch_the_plan(store):
    p = _plan(store)
    before = _activity(store, p["id"])
    store.create_task(U1, "loose task")           # no plan link
    store.jot(U1, "loose idea")                    # no plan link
    assert _activity(store, p["id"]) == before


# --- goal/plan consistency + reschedules -------------------------------------


def test_task_inherits_plan_from_goal(store):
    p = _plan(store)
    g = store.create_goal(U1, p["id"], "goal")
    t = store.create_task(U1, "task", goal_id=g["id"])
    assert t["plan_id"] == p["id"]


def test_task_goal_plan_mismatch_is_rejected(store):
    p1, p2 = _plan(store), _plan(store, title="second plan", helper="pep")
    g = store.create_goal(U1, p1["id"], "goal")
    with pytest.raises(ValueError, match="belongs to plan"):
        store.create_task(U1, "task", goal_id=g["id"], plan_id=p2["id"])


def test_reschedules_count_only_later_slips(store):
    t = store.create_task(U1, "task", scheduled="2026-07-20")
    t = store.update_task(U1, t["id"], scheduled="2026-07-22")   # slip -> +1
    assert t["reschedules"] == 1
    t = store.update_task(U1, t["id"], scheduled="2026-07-21")   # earlier -> free
    assert t["reschedules"] == 1
    t2 = store.create_task(U1, "unscheduled")
    t2 = store.update_task(U1, t2["id"], scheduled="2026-08-01")  # first date -> free
    assert t2["reschedules"] == 0


# --- check-ins ---------------------------------------------------------------


def test_morning_checkin_unique_per_day_but_adhoc_unlimited(store):
    store.log_checkin(U1, "2026-07-20", "morning", "chordial")
    with pytest.raises(ValueError, match="already exists"):
        store.log_checkin(U1, "2026-07-20", "morning", "chordial")
    store.log_checkin(U1, "2026-07-21", "morning", "chordial")   # next day fine
    store.log_checkin(U1, "2026-07-20", "adhoc", "pep")
    store.log_checkin(U1, "2026-07-20", "adhoc", "pep")           # unlimited
    assert len(store.list_checkins(U1)) == 4


def test_checkin_accepts_public_id_plan_refs(store):
    p = _plan(store)
    ci = store.log_checkin(U1, "2026-07-20", "evening", "chordial",
                           plan_ids=[p["public_id"], p["id"]])
    assert ci["plan_ids"] == [p["id"], p["id"]]


# --- notes -------------------------------------------------------------------


def test_jot_requires_a_body(store):
    with pytest.raises(ValueError, match="body"):
        store.jot(U1, "   ")


def test_note_filters_tag_query_and_plan(store):
    p = _plan(store)
    store.jot(U1, "melody in 7/8", tags=["music"])
    store.jot(U1, "chapter three: the storm", tags=["writing"], plan_id=p["id"])
    store.jot(U1, "video idea: studio tour", tags=["video"])

    assert len(store.list_notes(U1, tag="music")) == 1
    assert len(store.list_notes(U1, plan_id=p["id"])) == 1
    hits = store.list_notes(U1, query="STORM")
    assert len(hits) == 1 and hits[0]["plan_title"] == "finish the album"


def test_note_promotion_links_and_closes(store):
    t = store.create_task(U1, "write the bridge")
    n = store.jot(U1, "bridge idea")
    n = store.update_note(U1, n["id"], status="promoted", promoted_task_id=t["id"])
    assert n["promoted_task_id"] == t["id"] and n["closed_at"] is not None
    assert store.list_notes(U1) == []   # promoted leaves the open set


# --- occasions ---------------------------------------------------------------


def test_recurrence_rolls_forward_past_occurrences(store):
    store.create_occasion(U1, "weekly sync", "2026-07-01", recurrence="weekly")
    store.create_occasion(U1, "rent", "2026-05-31", recurrence="monthly")
    store.create_occasion(U1, "moms birthday", "2025-07-04", recurrence="yearly")
    rows = {o["title"]: o["date"] for o in store.list_occasions(U1, today="2026-07-20")}
    assert rows["weekly sync"] == "2026-07-22"
    # clamped to jun 30 in passing, but the 31st anchor is preserved
    assert rows["rent"] == "2026-07-31"
    assert rows["moms birthday"] == "2027-07-04"   # 2026-07-04 already passed


def test_yearly_feb29_clamps(store):
    store.create_occasion(U1, "leap day", "2024-02-29", recurrence="yearly")
    rows = store.list_occasions(U1, today="2024-06-01")
    assert rows[0]["date"] == "2025-02-28"


def test_one_offs_pass_quietly_into_history(store):
    store.create_occasion(U1, "dentist", "2026-07-10")
    store.create_occasion(U1, "flight", "2026-08-02")
    upcoming = [o["title"] for o in store.list_occasions(U1, today="2026-07-20")]
    assert upcoming == ["flight"]
    everything = [o["title"] for o in store.list_occasions(U1, today="2026-07-20",
                                                           include_past=True)]
    assert everything == ["dentist", "flight"]


def test_until_window(store):
    store.create_occasion(U1, "soon", "2026-07-22")
    store.create_occasion(U1, "later", "2026-09-01")
    within = [o["title"] for o in store.list_occasions(U1, today="2026-07-20",
                                                       until="2026-07-23")]
    assert within == ["soon"]


# --- resolution ladder -------------------------------------------------------


def test_exact_match_beats_substring(store):
    _plan(store, title="album")
    _plan(store, title="album artwork", helper="pep")
    r = store.resolve(U1, "plan", "album")
    assert r.match is not None and r.match["title"] == "album"


def test_case_insensitive_exact_is_second_tier(store):
    _plan(store, title="Album")
    _plan(store, title="album artwork", helper="pep")
    r = store.resolve(U1, "plan", "ALBUM")
    assert r.match is not None and r.match["title"] == "Album"


def test_ambiguous_substring_returns_candidates_not_a_guess(store):
    _plan(store, title="album artwork", helper="pep")
    _plan(store, title="album release", helper="aria")
    r = store.resolve(U1, "plan", "album")
    assert r.match is None
    assert {c["title"] for c in r.candidates} == {"album artwork", "album release"}


def test_nothing_found_is_empty_not_error(store):
    r = store.resolve(U1, "plan", "does not exist")
    assert r.match is None and r.candidates == []


def test_public_id_short_circuits_resolution(store):
    p = _plan(store, title="p1 is also a weird title")
    r = store.resolve(U1, "plan", p["public_id"])
    assert r.match is not None and r.match["id"] == p["id"]


def test_like_wildcards_in_titles_are_literal(store):
    _plan(store, title="100% done")
    r = store.resolve(U1, "plan", "100% done")
    assert r.match is not None
    assert store.resolve(U1, "plan", "100_ done").match is None


# --- cross-user isolation ----------------------------------------------------
# public numeric ids are only unique per user; every path must scope by
# user_uuid. u2's rows deliberately collide with u1's titles.


class TestCrossUserIsolation:

    @pytest.fixture(autouse=True)
    def _two_users(self, store):
        self.store = store
        self.mine = _plan(store, user=U1, title="album")
        self.theirs = _plan(store, user=U2, title="album")

    def test_get_and_update_are_scoped(self):
        assert self.mine["id"] != self.theirs["id"]
        # same error shape for foreign and missing ids - existence is never revealed
        with pytest.raises(ValueError, match="not found"):
            self.store.get_plan(U2, self.mine["id"])
        foreign_task = self.store.create_task(U1, "secret task")
        with pytest.raises(ValueError, match="not found"):
            self.store.update_task(U2, foreign_task["id"], status="done")

    def test_lists_are_scoped(self):
        self.store.create_task(U1, "mine", plan_id=self.mine["id"])
        assert store_titles(self.store.list_tasks(U2)) == []
        assert store_titles(self.store.list_plans(U2)) == ["album"]
        assert len(self.store.list_plans(U1)) == 1

    def test_resolution_never_crosses_users(self):
        r = self.store.resolve(U2, "plan", "album")
        assert r.match is not None and r.match["id"] == self.theirs["id"]

    def test_task_links_cannot_reference_foreign_rows(self):
        with pytest.raises(ValueError, match="not found"):
            self.store.create_task(U2, "sneaky", plan_id=self.mine["id"])
        g = self.store.create_goal(U1, self.mine["id"], "goal")
        with pytest.raises(ValueError, match="not found"):
            self.store.create_task(U2, "sneaky", goal_id=g["id"])

    def test_checkin_plan_ids_json_column_is_scoped(self):
        # no FK protects the JSON list - the store must resolve entries
        # against the owner's plans only
        with pytest.raises(ValueError, match="not found"):
            self.store.log_checkin(U2, "2026-07-20", "adhoc", "chordial",
                                   plan_ids=[self.mine["id"]])

    def test_win_and_note_links_are_scoped(self):
        with pytest.raises(ValueError, match="not found"):
            self.store.log_win(U2, "stolen", date.today(), "aria",
                               plan_id=self.mine["id"])
        n = self.store.jot(U2, "their idea")
        with pytest.raises(ValueError, match="not found"):
            self.store.update_note(U2, n["id"], plan_id=self.mine["id"])


def store_titles(rows):
    return [r["title"] for r in rows]


def test_resolve_blank_ref_matches_nothing(store):
    # a blank would otherwise fall to the substring tier ('%%') and match
    # every row - the padded-call failure mode
    store.create_plan(U1, "a", helper="vel")
    store.create_plan(U1, "b", helper="vel")
    for ref in ("", "   "):
        result = store.resolve(U1, "plan", ref)
        assert result.match is None and not result.candidates


def test_list_tasks_title_contains_is_case_insensitive_and_literal(store):
    store.create_task(U1, "Pomodoro 1: a")
    store.create_task(U1, "pomodoro 2: b")
    store.create_task(U1, "walk 100% outside")
    assert len(store.list_tasks(U1, title_contains="POMODORO")) == 2
    assert [r["title"] for r in store.list_tasks(U1, title_contains="100%")] == [
        "walk 100% outside"]
    assert len(store.list_tasks(U1, title_contains="  ")) == 3
