"""workspace tools: the native replacement for the notion tool surface.

contract assertions first (the 9 legacy names survive with compatible
required fields and flags), then behavior through the real registry +
store: display-vocab round-trips, resolution-ladder candidates, link
inheritance, graceful constraint errors, persona allowlists under both
backends.
"""
import asyncio
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.database as db_mod
from config import Config
from src.database.models import Base, User
from dainframe.providers.types import ToolCall
from dainframe.tools.context import ToolContext
from src.services.tools import build_default_registry

U1 = "user-one"

# the byte contract: every legacy notion tool name, with the required
# fields its schema declared. phase D may drop the *_project aliases, but
# these nine survive the backend swap untouched.
LEGACY_CONTRACTS = {
    "list_tasks": [],
    "create_task": ["title"],
    "update_task": ["task"],
    "list_projects": [],
    "create_project": ["title"],
    "update_project": ["project"],
    "list_cycles": [],
    "create_cycle": ["title"],
    "update_cycle": ["cycle"],
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def registry(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    with TestSession() as s:
        s.add(User(uuid=U1, preferred_name="dain", timezone="UTC"))
        s.commit()
    yield build_default_registry()
    engine.dispose()


def call(registry, name, **tool_input):
    result = run(registry.execute(
        ToolCall(id="t", name=name, input=tool_input),
        ToolContext(stream_id=U1, activation_id="test-act", actor="vel")))
    return result


# --- contract assertions -----------------------------------------------------


def test_nine_legacy_tool_names_survive_with_compatible_contracts(registry):
    defs = {d.name: d for d in registry.definitions()}
    for name, required in LEGACY_CONTRACTS.items():
        assert name in defs, f"legacy tool {name} missing under native backend"
        assert defs[name].input_schema.get("required", []) == required


def test_plan_named_aliases_are_registered(registry):
    names = {d.name for d in registry.definitions()}
    assert {"list_plans", "create_plan", "update_plan"} <= names


def test_list_tools_dont_record_events_and_mutations_do(registry):
    for name in ("list_tasks", "list_projects", "list_cycles", "list_goals",
                 "list_wins", "list_checkins", "list_notes", "list_occasions"):
        assert registry.should_record(name) is False, name
    for name in ("create_task", "update_task", "create_project", "jot",
                 "log_win", "log_checkin", "log_occasion", "update_note"):
        assert registry.should_record(name) is True, name


def test_jot_is_terminal_like_save_memory(registry):
    assert registry.is_terminal("jot") is True
    assert registry.is_terminal("create_task") is False


# --- display vocab round-trips -----------------------------------------------


def test_task_status_display_round_trip(registry):
    call(registry, "create_task", title="bounce stems", status="To do")
    listing = call(registry, "list_tasks").content
    assert "[To do]" in listing


def test_legacy_project_statuses_still_accepted(registry):
    call(registry, "create_project", title="old style", status="Not started")
    call(registry, "create_project", title="rolling", status="recurring")
    listing = call(registry, "list_plans", include_closed=True).content
    assert "[Proposed]" in listing and "[Active]" in listing


# --- resolution + links ------------------------------------------------------


def test_ambiguity_returns_candidates_never_a_guess(registry):
    call(registry, "create_project", title="album artwork")
    call(registry, "create_project", title="album release")
    result = call(registry, "update_plan", plan="album", status="Active")
    assert "multiple plans match" in result.content
    assert "p1" in result.content and "p2" in result.content
    listing = call(registry, "list_plans").content
    assert "[Active]" not in listing   # nothing was written


def test_goal_link_implies_its_plan(registry):
    call(registry, "create_project", title="finish the album", helper="juniper")
    call(registry, "create_goal", plan="finish the album", title="mix track one")
    call(registry, "create_task", title="bounce stems", goal="mix track one")
    listing = call(registry, "list_tasks").content
    assert "plan=finish the album" in listing and "goal=mix track one" in listing


def test_win_inherits_plan_from_task(registry):
    call(registry, "create_project", title="finish the album", helper="juniper")
    call(registry, "create_task", title="bounce stems", project="finish the album")
    call(registry, "log_win", title="bounced the stems", task="bounce stems")
    wins = call(registry, "list_wins").content
    assert "plan=finish the album" in wins


def test_public_id_round_trip_the_reconciler_contract(registry):
    """the reconciler echoes payload ids (t42) into update_task verbatim -
    that exact shape must resolve and write."""
    call(registry, "create_task", title="walk outside")
    result = call(registry, "update_task", task="t1", status="Done")
    assert not result.is_error and "updated task (id=t1)" in result.content
    assert "walk outside" in call(registry, "list_tasks", status="Done").content


def test_duplicate_morning_checkin_is_a_graceful_tool_error(registry):
    today = date.today().isoformat()
    ok = call(registry, "log_checkin", kind="morning", date=today)
    assert not ok.is_error
    dupe = call(registry, "log_checkin", kind="morning", date=today)
    assert dupe.is_error and "already exists" in dupe.content


def test_note_promotion_via_tool(registry):
    call(registry, "create_task", title="write the bridge")
    call(registry, "jot", body="bridge idea: modulate up a third")
    result = call(registry, "update_note", note="n1", promoted_task="write the bridge")
    assert not result.is_error
    open_notes = call(registry, "list_notes").content
    assert open_notes == "no notes matched."   # promoted leaves the open set
    archived = call(registry, "list_notes", include_closed=True).content
    assert "[Promoted]" in archived


# --- allowlists under both backends ------------------------------------------


def test_mabel_allowlist_resolves_under_native(registry):
    from src.personas import load_personas
    view = registry.view(load_personas()["mabel"].tools)
    names = {d.name for d in view.definitions()}
    assert {"jot", "log_occasion", "list_wins", "list_checkins"} <= names
    assert not names & set(LEGACY_CONTRACTS), "mabel must never see task tools"


def test_every_persona_allowlist_resolves():
    """every persona card's tool allowlist must resolve against the default
    registry - an unknown name is a wiring-time crash, so catch it here."""
    reg = build_default_registry()
    from src.personas import load_personas
    for card in load_personas().values():
        if card.tools is not None:
            reg.view(card.tools)   # must not raise


# --- steward validation (sol's P2: retired ids must not orphan plans) --------


def test_plan_steward_must_be_a_deployed_helper(registry):
    """the schema used to advertise the retired cast (chordial/tempo/...) and
    the handlers persisted whatever id arrived - a plan stewarded by a helper
    who no longer exists is orphaned the moment it's written."""
    rejected = call(registry, "create_plan", title="finish the album",
                    helper="aria").content
    assert "isn't a helper in this council" in rejected
    assert "vel" in rejected  # the corrective lists who IS valid
    assert "no plans matched." in call(registry, "list_plans").content

    accepted = call(registry, "create_plan", title="finish the album",
                    helper="juniper").content
    assert "steward=juniper" in accepted

    re_steward = call(registry, "update_plan", plan="finish the album",
                      helper="chordial").content
    assert "isn't a helper in this council" in re_steward


def test_plan_steward_defaults_to_the_acting_helper(registry):
    result = call(registry, "create_plan", title="ship the rooms").content
    assert "steward=vel" in result  # the test context's actor


def test_plan_steward_schema_names_the_live_council(registry):
    from src.services.tools.workspace_tools import _PLAN_WRITE_PROPS
    description = _PLAN_WRITE_PROPS["helper"]["description"]
    for retired in ("chordial", "tempo", "aria", "pep", "poet"):
        assert retired not in description
    assert "vel" in description and "pip" in description


# --- cycle planning fields (phase 5a review round) -----------------------------


def test_cycle_tools_carry_theme_and_capacity(registry):
    """sol's find on #65: theme/capacity_blocks existed in the schema but
    no model-facing door could set them."""
    out = call(registry, "create_cycle", title="cycle t", status="Active",
               theme="build rhythm", capacity_blocks=24).content
    assert "created cycle" in out
    out = call(registry, "list_cycles").content
    assert "theme=build rhythm" in out and "capacity=24" in out

    out = call(registry, "update_cycle", cycle="cycle t",
               theme="steadier now", capacity_blocks=20).content
    assert "updated cycle" in out
    out = call(registry, "list_cycles").content
    assert "theme=steadier now" in out and "capacity=20" in out


def test_update_cycle_redirects_frozen_capacity_to_change_scope(registry):
    """after the freeze, the general update door bounces capacity to the
    scope ledger - promptably, so pip self-corrects."""
    call(registry, "create_cycle", title="cycle t", status="Active",
         capacity_blocks=10)
    call(registry, "create_commitment", title="one promise",
         blocks_planned=3)
    out = call(registry, "freeze_cycle").content
    assert "baseline frozen" in out

    out = call(registry, "update_cycle", cycle="cycle t", capacity_blocks=99).content
    assert "change_scope" in out and "updated cycle" not in out

    out = call(registry, "change_scope", reason="the week collapsed",
               capacity_blocks=8).content
    assert "scope change recorded" in out
    out = call(registry, "view_cycle").content
    assert "capacity=8 blocks" in out


# --- blank arguments are omissions (tools/inputs.py) -------------------------
#
# the "archive the pomodoro tasks" morning: gpt-5.6-terra padded every
# optional field it wasn't setting ("" for the links and the scope, 0 for
# the estimate) and the handlers applied the blanks - a blank project matched
# every plan, so six status updates failed at the resolver and vel blamed the
# workspace. the dainframe's openai provider now prevents the padding at the
# wire (strict tools); these pin the product-side defense for whatever still
# arrives blank.

PADDED_BLANKS = dict(project="", sprint="", goal="", description="",
                     next_action="", new_title="", scheduled_date="", helper="")


def test_padded_blank_arguments_are_ignored_and_the_status_still_lands(registry):
    call(registry, "create_project", title="YouTube Video Essay")
    call(registry, "create_project", title="Cadenza")
    call(registry, "create_task", title="Pomodoro 2: record the real voiceover",
         next_action="open the script")
    result = call(registry, "update_task", task="Pomodoro 2: record the real voiceover",
                  status="deprioritized", **PADDED_BLANKS)
    assert not result.is_error, result.content
    assert result.content == "updated task (id=t1): status."
    listing = call(registry, "list_tasks", include_closed=True).content
    assert "[deprioritized]" in listing
    # the blank next_action did NOT clear the scope, the blank title didn't rename
    assert 'next="open the script"' in listing
    assert "Pomodoro 2: record the real voiceover" in listing


def test_whitespace_only_link_is_an_omission_too(registry):
    call(registry, "create_project", title="a")
    call(registry, "create_project", title="b")
    call(registry, "create_task", title="walk outside")
    result = call(registry, "update_task", task="walk outside", status="Done",
                  project="   ")
    assert not result.is_error and "multiple plans" not in result.content


def test_clearing_the_scope_is_explicit(registry):
    call(registry, "create_task", title="walk outside", next_action="shoes on")
    assert 'next="shoes on"' in call(registry, "list_tasks").content
    result = call(registry, "update_task", task="walk outside", clear_next_action=True)
    assert not result.is_error and "next_action" in result.content
    assert "next=" not in call(registry, "list_tasks").content


def test_blank_list_filters_are_ignored(registry):
    call(registry, "create_project", title="a")
    call(registry, "create_project", title="b")
    call(registry, "create_task", title="walk outside")
    listing = call(registry, "list_tasks", project="", sprint="",
                   scheduled_on_or_after="", title_contains="")
    assert not listing.is_error and "walk outside" in listing.content


def test_malformed_date_is_a_promptable_error_not_an_exception(registry):
    call(registry, "create_task", title="walk outside")
    result = call(registry, "update_task", task="walk outside",
                  scheduled_date="tomorrow")
    assert not result.is_error
    assert result.content == (
        "scheduled_date must be an ISO date (YYYY-MM-DD), not 'tomorrow'.")
    listing = call(registry, "list_tasks", scheduled_on_or_before="soon")
    assert "scheduled_on_or_before must be an ISO date" in listing.content


def test_title_contains_finds_the_whole_sweep(registry):
    for n in range(1, 8):
        call(registry, "create_task", title=f"Pomodoro {n}: video bit {n}")
    call(registry, "create_task", title="walk outside")
    listing = call(registry, "list_tasks", title_contains="pomodoro").content
    assert listing.startswith("7 task(s):")
    assert "walk outside" not in listing
    # like-pattern characters in the fragment are literal
    assert "no tasks matched." == call(registry, "list_tasks",
                                       title_contains="%").content


def test_cycle_spine_tools_share_the_blank_seam(registry):
    call(registry, "create_project", title="a")
    call(registry, "create_project", title="b")
    call(registry, "create_cycle", title="week one", status="Active")
    call(registry, "create_task", title="walk outside")
    made = call(registry, "create_commitment", title="walk every day", project="")
    assert not made.is_error and "multiple plans" not in made.content, made.content
    result = call(registry, "update_commitment", commitment="walk every day",
                  priority="high", project="", task="")
    assert not result.is_error and "multiple" not in result.content, result.content


def test_clearing_a_commitment_next_step_is_explicit(registry):
    call(registry, "create_cycle", title="week one", status="Active")
    call(registry, "create_commitment", title="walk every day",
         next_action="shoes on")
    assert 'next="shoes on"' in call(registry, "view_cycle").content
    # a blank is an omission: the step survives
    kept = call(registry, "update_commitment", commitment="walk every day",
                priority="high", next_action="")
    assert not kept.is_error and "next_action" not in kept.content
    assert 'next="shoes on"' in call(registry, "view_cycle").content
    # the flag is the way
    cleared = call(registry, "update_commitment", commitment="walk every day",
                   clear_next_action=True)
    assert not cleared.is_error and "next_action" in cleared.content
    assert "next=" not in call(registry, "view_cycle").content
