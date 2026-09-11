from sqlalchemy import Column, String, DateTime, Date, JSON, Boolean, ForeignKey, Integer, Float, Index, UniqueConstraint, false, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

Base = declarative_base()

# all DateTime columns below are stored as naive UTC (datetime.utcnow), never
# server-local time - convert to a user's local timezone at the point of use
# via src.utils.timezone_utils

class User(Base):
    """main user table - our source of truth for users across platforms"""
    __tablename__ = 'users'
    
    # primary key is a uuid so we're not tied to any platform's id system
    uuid = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    
    # what the user wants to be called
    preferred_name = Column(String, nullable=True)

    # how the user wants to be referred to, verbatim as they said it
    # ("she/her", "they/them", "he/they", "any"). free text on purpose: an
    # enum here would tell someone their answer wasn't on the list. renders
    # into system block 2 next to preferred_name, so every helper carries it
    # on every turn instead of inferring from a name.
    pronouns = Column(String, nullable=True)

    # when they joined
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # user preferences
    timezone = Column(String, default='UTC')
    
    # schedule preferences stored as json for flexibility
    # example: {"morning_checkin": "08:00", "evening_reflection": "22:00", "checkin_interval_minutes": 180}
    schedule_preferences = Column(JSON, default={})
    
    # personality preferences
    bot_personality = Column(String, default='friendly')  # friendly, professional, cheerful, etc

    # TODO: make preferences generic? or have schedule_preferences and preferences both json
    # preferences can hold personality but also other user customizations for bot behavior
    
    # is the user actively using the bot
    is_active = Column(Boolean, default=True)

    # synthetic/seed account - kept in the db for testing, but never a target
    # for proactive/outbound sends (scheduler skips it before generating)
    is_test = Column(Boolean, default=False)

    # relationships
    platform_identities = relationship("PlatformIdentity", back_populates="user")
    memories = relationship("Memory", back_populates="user")


class PlatformIdentity(Base):
    """links platform-specific ids to our users"""
    __tablename__ = 'platform_identities'
    
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid')) # internal chordial uuid
    platform = Column(String)  # 'discord', 'telegram', 'web', etc
    platform_user_id = Column(String)  # their id on that platform
    platform_username = Column(String, nullable=True)  # their username if available

    # is this specific link deliverable? flipped off when a send hard-fails
    # (discord 404/forbidden) so we stop paying to message a dead channel,
    # without deactivating the user who may still be reachable elsewhere
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    
    # relationships
    user = relationship("User", back_populates="platform_identities")

    # one platform_user_id per platform, enforced for real (the linking flow
    # relies on this - a telegram account can only be bound to one user)
    __table_args__ = (
        UniqueConstraint('platform', 'platform_user_id',
                         name='uq_platform_identity_platform_user'),
        {'sqlite_autoincrement': True},
    )


class ConversationEvent(Base):
    """the conversation event log: everything that happened in a user's
    channel, in order - messages, agent tool actions, (future) system notes.

    replaces the old conversation_history table. single writer + sqlite
    autoincrement means id order IS chronological order; created_at is kept
    for humans and for the scheduler's elapsed-time math. author attribution
    (rather than a bare user/assistant role) is what lets multiple agent
    personas share one channel later without another schema change.
    """
    __tablename__ = 'conversation_events'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    # WHICH conversation this event belongs to (rooms overhaul): a room uuid,
    # or - for grandfathered pre-rooms history - the user uuid itself (the
    # legacy one-stream-per-user, preserved as a legacy room). user_uuid
    # stays the WHOSE key: user-level reads (recency, active_platform) span
    # every room by filtering it, room reads filter stream_id.
    stream_id = Column(String, nullable=True, index=True)
    # provenance: which platform this event happened on. NOT a conversation
    # key; this just records where each moment took place (delivery
    # targeting, switch detection).
    platform = Column(String)

    author_type = Column(String, nullable=False)   # 'user' | 'agent' | 'system'
    author = Column(String, nullable=False)        # 'user' | a helper id ('vel', 'pip', ...) | 'curator'
    kind = Column(String, nullable=False)          # 'message' | 'action' | 'note'

    # message text, or (for actions) the frozen one-line rendering that gets
    # replayed into prompts verbatim - written once, never re-serialized, so
    # history bytes stay cache-stable
    content = Column(String, nullable=False)
    message_type = Column(String, nullable=True)   # 'conversation' | 'scheduled'; only on kind='message'
    event_metadata = Column(JSON, default={})      # actions: {tool, input, result, is_error, iteration}

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('ix_conversation_events_user_id', 'user_uuid', 'id'),
        {'sqlite_autoincrement': True},
    )


class LinkCode(Base):
    """one-time codes for linking a new platform account to an existing user.

    chordial hands the user a short code (and a telegram deep link carrying
    it); redeeming it on the new platform binds that platform identity to the
    same user - same memories, same conversation. short-lived and single-use.
    """
    __tablename__ = 'link_codes'

    id = Column(Integer, primary_key=True)
    code = Column(String, nullable=False, unique=True)   # 8 chars, unambiguous alphabet
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    # what redeeming this code does: 'platform_link' binds a platform account,
    # 'web_login' mints a browser session, 'device_link' enrolls a desktop-app
    # device. the paths never accept each other's codes.
    purpose = Column(String, nullable=False, default='platform_link',
                     server_default='platform_link')

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)        # created + ttl (15 min default)
    used_at = Column(DateTime, nullable=True)            # stamped on redemption

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class CompressedMessage(Base):
    """stores compressed versions of messages for efficient context"""
    __tablename__ = 'compressed_messages'

    id = Column(Integer, primary_key=True)
    # legacy pointer into the retired conversation_history table; new rows
    # store the conversation_events id here instead. plain integer (no FK) so
    # pre-migration ids can dangle harmlessly.
    conversation_history_id = Column(Integer)
    user_uuid = Column(String, ForeignKey('users.uuid'))
    platform = Column(String)
    
    # original message info
    role = Column(String)  # 'user' or 'assistant'
    original_length = Column(Integer)  # character count of original
    
    # compressed version
    compressed_content = Column(String)
    compressed_length = Column(Integer)  # character count after compression
    compression_ratio = Column(Float)  # how much we compressed (0.3 = 70% reduction)
    
    # metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    model_used = Column(String, default='gpt-3.5-turbo')

    # relationships
    user = relationship("User")

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )

class ConversationSummary(Base):
    """stores summaries of conversation chunks for compressed long-term context"""
    __tablename__ = 'conversation_summaries'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'))
    platform = Column(String)

    # range of conversation_history ids this summary covers
    first_message_id = Column(Integer)
    last_message_id = Column(Integer)
    message_count = Column(Integer)

    summary = Column(String)
    key_topics = Column(JSON, default=[])
    model_used = Column(String)

    created_at = Column(DateTime, default=datetime.utcnow)

    # relationships
    user = relationship("User")

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Memory(Base):
    """stores memories about users for persistent context"""
    __tablename__ = 'memories'
    
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    
    # core memory data
    ai_instruction = Column(String, nullable=False)  # what the ai should remember/do
    weighting = Column(Float, default=1.0)  # importance weight (higher = more important)
    keywords = Column(String)  # comma-separated keywords for searching
    
    # memory properties
    core = Column(Boolean, default=False)  # if true, always included (max weight)
    is_active = Column(Boolean, default=True)  # soft delete flag
    memory_type = Column(String(20), nullable=False)  # PREFERENCE, FACT, EPISODIC
    
    # embedding for semantic search (stored as json for sqlite compatibility)
    # for postgres, you'd use: embedding = Column(Vector(1536))
    embedding = Column(JSON)  # store as json array for now
    
    # metadata
    source = Column(String, nullable=False)  # USER_EXPLICIT, AI_INFERRED, SYSTEM_GENERATED
    access_count = Column(Integer, default=0)
    last_accessed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    ttl = Column(Integer, nullable=True)  # time to live in seconds (null = permanent)
    memory_metadata = Column(JSON, default={})  # flexible metadata storage

    # reinforcement: each time a duplicate memory is saved we bump the existing
    # row instead of inserting a new one, so repeated facts grow in importance.
    reinforced_count = Column(Integer, default=0)   # times this memory was re-saved
    last_reinforced_at = Column(DateTime, nullable=True)

    # curation: the curator agent reviews rows where curated_at IS NULL (merge /
    # update / expire / promote), then stamps them. merged_into points at the
    # canonical row when this one was absorbed by a merge (soft-deleted too).
    curated_at = Column(DateTime, nullable=True)
    merged_into = Column(Integer, nullable=True)

    # v3 multi-helper memory model: one shared pool plus per-helper privates.
    # created_by is always set (attribution, even for shared rows - lets a
    # sibling's memory render as "(from aria) ..."). visibility='shared' means
    # every helper's search + core-memory rendering can see the row; 'private'
    # restricts it to created_by only.
    created_by = Column(String, default='chordial')
    visibility = Column(String, default='shared')  # 'shared' | 'private'

    # relationships
    user = relationship("User", back_populates="memories")

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class HelperState(Base):
    """per-(user, helper) relationship state: has this helper been met, is it
    enabled, and what identity did it take. one row per (user, helper) pair;
    the director's cast is every row with status='active'."""
    __tablename__ = 'helper_states'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    helper_id = Column(String, nullable=False)      # persona card id, e.g. 'tempo'

    # not_met | introducing | active | declined | disabled
    status = Column(String, default='not_met')

    # chosen identity (layer 2, per-user) - denormalized from the shared-visibility
    # identity core memory so the director's cast list is a plain column read.
    persona_name = Column(String, nullable=True)    # e.g. 'Ember'
    persona_form = Column(String, nullable=True)    # e.g. 'red panda' | 'no character'

    introduced_at = Column(DateTime, nullable=True)
    disabled_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint('user_uuid', 'helper_id', name='uq_helper_state_user_helper'),
        {'sqlite_autoincrement': True},
    )


class UsageLog(Base):
    """per-call token accounting - one row per ai api call.

    the foundation for per-user cost visibility and (later) daily budgets.
    cheap to write now, impossible to backfill later.
    """
    __tablename__ = 'usage_log'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=True)
    platform = Column(String, nullable=True)
    # which helper this call was made on behalf of (v3 per-helper cost
    # visibility); nullable/unset until later-phase writers start populating it.
    helper_id = Column(String, nullable=True)

    provider = Column(String)   # 'anthropic' | 'openai'
    model = Column(String)
    role = Column(String)       # 'conversation' | 'scheduled' | 'utility'

    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    # the meter (phase 7c) made this table a per-turn read path: the
    # composite serves budget_verdict's (user, trailing-24h) sum, the
    # single column serves the ops report's window scans
    __table_args__ = (
        Index('ix_usage_log_user_created', 'user_uuid', 'created_at'),
        Index('ix_usage_log_created', 'created_at'),
        {'sqlite_autoincrement': True},
    )


# --- native workspace (docs/NATIVE_WORKSPACE_DESIGN.md) ---------------------
# the system of record for the user's workspace, replacing notion. controlled
# vocabularies live in src/services/workspace/vocab.py; every mutation goes
# through WorkspaceStore (src/services/workspace/store.py) so the invariants -
# closed_at stamping (design section 2.0), plans.last_activity_at side effects,
# goal/plan consistency, reschedule bumps - live in exactly one place.
#
# lifecycle convention (section 2.0): closable entities never hard-delete;
# status vocab splits into an open set and a closed set (closed always
# distinguishes completed from released), and closed_at is stamped by the
# store when status enters the closed set, cleared on reopen.


class Plan(Base):
    """a helper-stewarded body of work, possibly lofty/multi-month (evolves
    the dainframe's Projects). the steward (`helper`) nudges it along its
    cadence; `why` and `success_criteria` are the user's own words, raised in
    conversation rather than demanded at creation."""
    __tablename__ = 'plans'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    title = Column(String, nullable=False)
    helper = Column(String, nullable=False)   # archetype id: chordial/tempo/aria/pep/mochi/poet
    status = Column(String, default='proposed')  # proposed/active/paused | complete/released

    why = Column(String, nullable=True)                # user's motivation, their words
    success_criteria = Column(String, nullable=True)   # "success looks like"
    horizon_start = Column(Date, nullable=True)        # soft range, not a deadline
    horizon_end = Column(Date, nullable=True)
    cadence = Column(String, nullable=True)            # daily/weekly/loose

    legacy_area = Column(String, nullable=True)        # preserved dainframe Area
    notion_page_id = Column(String, nullable=True)     # reserved: the row<->page mapping for the future one-way notion mirror

    # stamped by WorkspaceStore as a side effect of any related write (task
    # under the plan, win logged, note attached, check-in touching it) -
    # powers "it's been three weeks since this came up" without streaks
    last_activity_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Goal(Base):
    """a concrete milestone under a plan. `done_means` is the anti-vagueness
    field: what specifically will be true when this is done."""
    __tablename__ = 'goals'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=False)

    title = Column(String, nullable=False)
    status = Column(String, default='not_started')  # not_started/in_progress | done/renegotiated
    target = Column(Date, nullable=True)
    done_means = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Cycle(Base):
    """the bi-weekly balancing lever across plans. `focus` is pep's negotiated
    balance statement for the cycle. `theme` + `capacity_blocks` are the
    ROOMS_DESIGN section-6 planning fields: a cycle carries a theme and an
    honest capacity estimate in focus blocks (25 min each), against which
    commitments allocate."""
    __tablename__ = 'cycles'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    title = Column(String, nullable=False)
    status = Column(String, default='upcoming')  # upcoming/active | complete
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    goal = Column(String, nullable=True)    # the cycle goal, as today
    focus = Column(String, nullable=True)   # v3: negotiated balance statement
    theme = Column(String, nullable=True)          # "build rhythm without overload"
    capacity_blocks = Column(Integer, nullable=True)  # honest estimate, focus blocks

    notion_page_id = Column(String, nullable=True)  # reserved: the row<->page mapping for the future one-way notion mirror

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Commitment(Base):
    """a cycle's explicit promise (ROOMS_DESIGN section 6): "room runtime v1
    · chordial · 5 blocks · high". first-class rows with STABLE uuids -
    focus attribution and baseline snapshots reference the uuid, never a
    title - so progress survives renames. optionally anchored to a plan
    and/or a task; `next_action` is the activation-energy field (exactly one
    clear next step per active commitment).

    lifecycle per section 2.0: never hard-deleted, closed_at stamped.
    completing is progress and stays an ordinary update; RELEASING after the
    cycle's baseline froze moves planned capacity, so it must arrive as a
    scope change (CycleStore enforces this).
    """
    __tablename__ = 'commitments'

    id = Column(Integer, primary_key=True)
    uuid = Column(String, nullable=False, unique=True,
                  default=lambda: str(uuid.uuid4()))
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    cycle_id = Column(Integer, ForeignKey('cycles.id'), nullable=False)

    title = Column(String, nullable=False)
    status = Column(String, default='active')   # active | completed/released
    priority = Column(String, nullable=True)    # high/medium/low
    blocks_planned = Column(Integer, nullable=True)  # capacity ask, in blocks
    next_action = Column(String, nullable=True)

    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    task_id = Column(Integer, ForeignKey('tasks.id'), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_commitments_user_cycle', 'user_uuid', 'cycle_id'),
        {'sqlite_autoincrement': True},
    )


class CycleBaseline(Base):
    """the frozen plan (ROOMS_DESIGN section 6): an append-only snapshot of
    the accepted commitments and allocations at freeze time. exactly one per
    cycle (unique constraint); the live commitment rows may evolve, the
    snapshot never does. edwin's planning-accuracy signal is the diff between
    this and the projection - reliable because both sides are append-only.
    immutable history: no updated_at, no lifecycle."""
    __tablename__ = 'cycle_baselines'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    cycle_id = Column(Integer, ForeignKey('cycles.id'), nullable=False)
    snapshot = Column(JSON, nullable=False)   # commitments + allocations at freeze
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint('cycle_id', name='uq_cycle_baselines_cycle'),
        {'sqlite_autoincrement': True},
    )


class ScopeChange(Base):
    """the only sanctioned way planned capacity moves after freeze
    (ROOMS_DESIGN section 6): an append-only (reason, deltas) record.
    honest data for edwin, guilt-free changes of mind for the user -
    changing the plan is welcome, silently rewriting history is not.
    immutable: no updated_at, no lifecycle."""
    __tablename__ = 'scope_changes'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    cycle_id = Column(Integer, ForeignKey('cycles.id'), nullable=False)
    # which commitment moved; null for cycle-level changes (capacity itself)
    commitment_uuid = Column(String, nullable=True)
    reason = Column(String, nullable=False)   # the user's words, required
    deltas = Column(JSON, nullable=False)     # {"action": ..., "from": ..., "to": ...}
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('ix_scope_changes_user_cycle', 'user_uuid', 'cycle_id'),
        {'sqlite_autoincrement': True},
    )


class Task(Base):
    """pomodoro-sized work (evolves the dainframe Tasks db in place).
    `scheduled` is a user-local calendar date, exactly as notion stored it -
    agenda comparisons use the user's `today`. singular plan/cycle FKs replace
    notion's multi-relations (every consumer already took only the first)."""
    __tablename__ = 'tasks'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)

    title = Column(String, nullable=False)
    status = Column(String, default='todo')     # todo/in_progress | done/deprioritized
    priority = Column(String, nullable=True)    # high/medium/low
    scheduled = Column(Date, nullable=True)     # user-local calendar date
    window = Column(String, nullable=True)      # morning/afternoon/evening/anytime
    pom_estimate = Column(Float, nullable=True)

    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    # when set, the store enforces goal.plan_id == plan_id
    goal_id = Column(Integer, ForeignKey('goals.id'), nullable=True)
    cycle_id = Column(Integer, ForeignKey('cycles.id'), nullable=True)
    helper = Column(String, nullable=True)      # who assigned/nudges

    # bumped by the store each time `scheduled` slips to a later date;
    # renegotiate (not nag) at 2-3
    reschedules = Column(Integer, default=0)
    description = Column(String, nullable=True)

    # the focus dogfood round (docs/FOCUS_DOGFOOD_DESIGN.md sections 2-3, 10):
    # `next_action` is the scope - the one-line first piece the clock runs
    # on (cap 140, same discipline as Commitment.next_action); `set_aside_on`
    # parks the task for ONE user-local day without touching its status
    # (the next day it simply returns); `set_aside_count` is the "parked on
    # N distinct days" signal behind the breakdown offer - `set_aside_counted_on`
    # is the last day that counted, kept apart from `set_aside_on` because
    # "bring back" clears the stamp and a same-day repark must not count
    # twice; `breakdown_offer_dismissed_at` remembers that the offer was
    # declined so every device agrees never to repeat it.
    next_action = Column(String, nullable=True)
    set_aside_on = Column(Date, nullable=True)
    set_aside_count = Column(Integer, default=0)
    set_aside_counted_on = Column(Date, nullable=True)
    breakdown_offer_dismissed_at = Column(DateTime, nullable=True)

    notion_page_id = Column(String, nullable=True)  # reserved: the row<->page mapping for the future one-way notion mirror

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # stamped for both endings; wins/analytics read it where status='done'
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # the agenda's two query shapes
        Index('ix_tasks_user_status', 'user_uuid', 'status'),
        Index('ix_tasks_user_scheduled', 'user_uuid', 'scheduled'),
        {'sqlite_autoincrement': True},
    )


class TaskSetAside(Base):
    """the set-aside ledger: one row per (task, user-local day) the task
    was parked on. `Task.set_aside_on` is the MUTABLE current stamp -
    "tomorrow" and "bring back" clear it - so any read of a past day
    (yesterday's digest under the morning brief, "was this on the list
    yesterday") needs the durable history, not the stamp (sol, #88).
    written by WorkspaceStore.update_task alongside the counter."""
    __tablename__ = 'task_set_asides'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    task_id = Column(Integer, ForeignKey('tasks.id'), nullable=False)
    day = Column(Date, nullable=False)     # the user-local day parked
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint('task_id', 'day', name='uq_task_set_asides_task_day'),
        Index('ix_task_set_asides_user_day', 'user_uuid', 'day'),
        {'sqlite_autoincrement': True},
    )


class TaskFocus(Base):
    """the per-task pomodoro clock behind the web focus view (src/web/).
    one row per (user, task): `accumulated_seconds` is banked time,
    `running_since` non-null means the clock is live for that task right now.
    FocusStore (src/services/workspace/focus.py) enforces the one invariant -
    at most one running row per user - by banking every running row before
    starting another. rows are bookkeeping, not workspace entities: no
    lifecycle, no public id, deleted with their task never (tasks never
    hard-delete)."""
    __tablename__ = 'task_focus'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey('tasks.id'), nullable=False)

    accumulated_seconds = Column(Integer, nullable=False, default=0)
    running_since = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('user_uuid', 'task_id', name='uq_task_focus_user_task'),
        {'sqlite_autoincrement': True},
    )


class Win(Base):
    """the anti-diminishment ledger: past-tense, concrete, witnessed.
    `evidence` is the user's words verbatim at the time. immutable history -
    no updated_at, no lifecycle."""
    __tablename__ = 'wins'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    title = Column(String, nullable=False)      # past-tense, concrete
    date = Column(Date, nullable=False)
    helper = Column(String, nullable=False)     # who witnessed/logged it

    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    # a win born from a task completion keeps the link
    task_id = Column(Integer, ForeignKey('tasks.id'), nullable=True)

    evidence = Column(String, nullable=True)    # the user's words, verbatim
    weight = Column(String, default='solid')    # spark/solid/milestone

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Checkin(Base):
    """the shared daily journal. morning/evening are unique per (user, date) -
    enforced by a PARTIAL unique index so adhoc check-ins stay unlimited.
    energy is asked, never demanded."""
    __tablename__ = 'checkins'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    date = Column(Date, nullable=False)
    kind = Column(String, nullable=False)       # morning/evening/adhoc
    energy = Column(String, nullable=True)      # low/ok/good/great
    notes = Column(String, nullable=True)
    # "plans touched" - JSON list of plan ids, not an association table
    # (promptable, sqlite-friendly; the Memory.embedding precedent). no FK
    # protects this, so the store resolves entries against the owner's plans.
    plan_ids = Column(JSON, default=[])
    helper = Column(String, nullable=False)     # who ran it

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('uq_checkins_user_date_kind', 'user_uuid', 'date', 'kind',
              unique=True,
              sqlite_where=text("kind IN ('morning', 'evening')"),
              postgresql_where=text("kind IN ('morning', 'evening')")),
        {'sqlite_autoincrement': True},
    )


class Note(Base):
    """the one deliberately NON-committal container (design section 2.7): a
    loose creative idea (no plan_id) or plan-attached detail. never in the
    agenda, never overdue; surfaced when work starts on its plan. no task_id
    by design - tasks are pomodoro-sized, detail belongs on the plan."""
    __tablename__ = 'notes'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    body = Column(String, nullable=False)       # the jot, user's words - only required field
    title = Column(String, nullable=True)       # auto-derived from first line when absent
    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    tags = Column(JSON, default=[])             # medium: writing/music/video/...
    helper = Column(String, nullable=True)      # domain steward who captured it

    status = Column(String, default='active')   # active | promoted/archived - no "done"
    # provenance when an idea grows up; set alongside status -> promoted
    promoted_plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    promoted_task_id = Column(Integer, ForeignKey('tasks.id'), nullable=True)

    # reserved: the row<->page mapping for the future one-way notion mirror
    notion_page_id = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class Occasion(Base):
    """a dated thing that isn't work (design section 2.8): birthdays,
    appointments, flights. informs, never nags - no status, no closed_at;
    occasions pass, they aren't done. on recurrence the store rolls `date`
    forward past occurrence, so `date` always holds the next one."""
    __tablename__ = 'occasions'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False, index=True)

    title = Column(String, nullable=False)
    date = Column(Date, nullable=False)         # user-local, same semantics as tasks.scheduled
    time = Column(String, nullable=True)        # freeform ("14:30", "afternoon") - display, not scheduling
    recurrence = Column(String, nullable=True)  # yearly/monthly/weekly; null = one-off

    plan_id = Column(Integer, ForeignKey('plans.id'), nullable=True)
    notes = Column(String, nullable=True)
    helper = Column(String, nullable=True)      # who captured it

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class AgentTrace(Base):
    """one row per agent turn - records the tool loop for debugging/tuning.

    kept separate from the conversation event log so raw intra-turn tool
    exchanges stay out of the replayed (and cached) conversation prefix.
    """
    __tablename__ = 'agent_traces'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=True)
    platform = Column(String, nullable=True)
    # which helper this trace belongs to (v3 per-helper cost visibility);
    # nullable/unset until later-phase writers start populating it.
    helper_id = Column(String, nullable=True)

    turn_kind = Column(String)          # 'conversation' | 'scheduled'
    iterations = Column(Integer, default=0)
    hit_iteration_cap = Column(Boolean, default=False)
    # list of {iteration, calls: [{name, input, is_error}]}
    tool_trace = Column(JSON, default=[])
    final_text_length = Column(Integer, default=0)
    stop_reason = Column(String, nullable=True)

    total_input_tokens = Column(Integer, default=0)
    total_output_tokens = Column(Integer, default=0)
    total_cache_read_tokens = Column(Integer, default=0)
    total_cache_write_tokens = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )

# --- app devices & sync (docs/ROOMS_DESIGN.md section 10) --------------------
# the desktop app's identity and the offline sync contract. devices are
# individually revocable credentials; device events are append-only facts
# applied exactly once no matter how many times the outbox retries them.


class Device(Base):
    """a linked desktop-app installation - the app-facing api's identity.

    linking redeems a one-time LinkCode (purpose='device_link') and mints a
    bearer secret returned exactly once; only its sha256 is stored. every
    /api/v1 request resolves `Authorization: Bearer <device_uuid>.<secret>`
    to a live (non-revoked) device row and acts as that device's user - the
    tenant scoping the design doc requires from the first endpoint.
    """
    __tablename__ = 'devices'

    id = Column(Integer, primary_key=True)
    device_uuid = Column(String, nullable=False, unique=True,
                         default=lambda: str(uuid.uuid4()))
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    name = Column(String, nullable=False, default='device')
    token_hash = Column(String, nullable=False)   # sha256 hex of the secret

    # sync contract: highest contiguously-applied per-device sequence - the
    # durable ACK cursor the sidecar's outbox trims at.
    acked_seq = Column(Integer, nullable=False, default=0, server_default='0')

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    __table_args__ = (
        {'sqlite_autoincrement': True}
    )


class DeviceEvent(Base):
    """device-origin domain events (the sync contract's server side).

    append-only facts from the sidecar (focus_block.completed, drift.detected,
    ...). the client stamps event_uuid and (device, seq) at write time; both
    carry unique constraints, so a retry after a dropped ACK is a no-op that
    still ACKs - at-least-once delivery, exactly-once apply.
    """
    __tablename__ = 'device_events'

    id = Column(Integer, primary_key=True)
    event_uuid = Column(String, nullable=False, unique=True)
    device_id = Column(Integer, ForeignKey('devices.id'), nullable=False)
    # denormalized from the device so tenant-scoped queries never join
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    seq = Column(Integer, nullable=False)

    event_type = Column(String, nullable=False)   # 'focus_block.completed', ...
    payload = Column(JSON, default={})
    occurred_at = Column(DateTime, nullable=True)  # device wall clock (naive utc)
    applied_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # rejection tombstones: a semantically invalid event (unknown type, bad
    # payload shape) still CONSUMES its (device, seq) so the ACK cursor can
    # pass it - a rejected event must never pin the outbox forever. the error
    # is preserved for debugging; processors skip rejected rows.
    # false() compiles per-dialect (sqlite '0', postgres 'false') - a literal
    # text('0') is invalid DDL for a postgres boolean, which broke every
    # fresh-postgres install path until the drill caught it (phase 7c)
    rejected = Column(Boolean, nullable=False, default=False,
                      server_default=false())
    error = Column(String, nullable=True)

    # stamped by the focus-flow processor (src/services/focus_flow.py) once
    # a row's consequences (pip's observations, ...) are committed - in the
    # SAME transaction, so apply-then-process is crash-safe: an unstamped row
    # is simply picked up by the next pass. null = not yet processed.
    processed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint('device_id', 'seq', name='uq_device_events_device_seq'),
        Index('ix_device_events_unprocessed', 'user_uuid', 'processed_at'),
        {'sqlite_autoincrement': True},
    )


class AppMessageReceipt(Base):
    """idempotency receipts for app-submitted chat messages.

    a lost http response must not cost a second model turn (and duplicate
    tool mutations) on retry: the client stamps every send with a
    client_message_id, unique per device. the receipt row is claimed BEFORE
    the turn runs and the delivered lines are stored into it after - a retry
    replays the stored response instead of re-invoking the model. a claim
    whose response never arrived (crash mid-turn) is reclaimable after a
    short window.
    """
    __tablename__ = 'app_message_receipts'

    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey('devices.id'), nullable=False)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    client_message_uuid = Column(String, nullable=False)
    response = Column(JSON, nullable=True)   # delivered lines; null = in flight
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint('device_id', 'client_message_uuid',
                         name='uq_app_receipts_device_msg'),
        {'sqlite_autoincrement': True},
    )


# --- rooms (docs/ROOMS_DESIGN.md sections 4 + 12, phase 2b) ------------------
# a room is a bounded conversation: purpose, participants, scoped context,
# lifecycle. each room is its own engine stream (room_uuid IS the stream id).


class Room(Base):
    """one bounded conversational space.

    lifecycle is the section-4 idempotent state machine: open -> closing ->
    closed, every transition safe to retrigger. daily rooms are lazy: created
    on the first interaction of a user's local day, closed by the arrival of
    the NEXT day's first interaction (never a midnight cron). the pre-rooms
    per-user stream is grandfathered as a single room_type='legacy' room
    whose room_uuid equals the user uuid - which is exactly what old
    conversation_events rows backfill their stream_id to.

    cycle rooms (phase 6b: 'cycle_retro' | 'cycle_planning') are undated;
    their identity is the subject anchor instead - subject_type='cycle',
    subject_id=the cycle's public id ('cy12'), same vocabulary as
    assessments. the anchor names what the room is ABOUT: a retro looks
    back at its cycle; a planning room follows a sealed cycle (its
    evidence base - the retro summary and the scorecard - belongs to the
    cycle just lived, even though the conversation plans the next one).
    """
    __tablename__ = 'rooms'

    id = Column(Integer, primary_key=True)
    room_uuid = Column(String, nullable=False, unique=True,
                       default=lambda: str(uuid.uuid4()))
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    room_type = Column(String, nullable=False)     # 'daily' | 'legacy' | ...
    status = Column(String, nullable=False, default='open')  # open|closing|closed
    # the user-local date a daily room belongs to; null for non-dated types
    date = Column(Date, nullable=True)
    # the subject anchor (null for daily/legacy rooms)
    subject_type = Column(String, nullable=True)   # 'cycle' | ...
    subject_id = Column(String, nullable=True)     # public id, e.g. 'cy12'

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # at most one room per (user, type, date): the lazy-create race ends
        # in one winner and one integrity error, not two mondays
        UniqueConstraint('user_uuid', 'room_type', 'date',
                         name='uq_rooms_user_type_date'),
        # the undated twin: cycle rooms carry no date, and null dates make
        # the constraint above vacuous (nulls are distinct) - the subject
        # anchor is their uniqueness. at most one retro and one planning
        # room per (user, cycle); daily rooms have null subject_id and are
        # untouched for the same null-distinctness reason.
        Index('uq_rooms_user_type_subject',
              'user_uuid', 'room_type', 'subject_id', unique=True),
        {'sqlite_autoincrement': True},
    )


class RoomSummary(Base):
    """the close pass's durable output: the compressed consequences the NEXT
    room reads instead of this room's transcript. exactly one per room
    (unique constraint = the section-4 idempotence guarantee). v0 summaries
    are deterministic digests; an LLM close pass can upgrade content later
    without touching the shape.
    """
    __tablename__ = 'room_summaries'

    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey('rooms.id'), nullable=False,
                     unique=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    content = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Observation(Base):
    """a helper's structured noticing (ROOMS_DESIGN.md §2/§3): 'helpers
    reason more than they speak' - an observation is what the reasoning
    leaves behind. distinct from a memory on purpose: memories are facts
    about the person that render into prompts; observations are a helper's
    own evidence-tied read of how things are going (a pattern, progress, a
    concern), consumed by reviews and edwin's scorecards - never replayed
    as conversation.
    """
    __tablename__ = 'observations'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    helper_id = Column(String, nullable=False)     # who noticed ('pip', ...)
    # the room the observation was made in (stream uuid); nullable because
    # future emitters (device-event pipelines, cycle jobs) observe outside
    # any conversation
    room_uuid = Column(String, nullable=True)
    kind = Column(String, nullable=False)          # pattern|progress|concern|context
    content = Column(String, nullable=False)
    # what the noticing is tied to: free-form structured refs (quotes, event
    # ids, focus-block ids as later phases start passing them)
    evidence = Column(JSON, nullable=True)
    # when a pipeline (focus_flow) derives the observation from a device
    # event, the event's uuid lands here under a UNIQUE index - the hard
    # guarantee that one fact never becomes two noticings, whatever races.
    # null for conversational observations (multiple nulls are fine).
    source_event_uuid = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('ix_observations_user_helper', 'user_uuid', 'helper_id'),
        Index('ix_observations_user_created', 'user_uuid', 'created_at'),
        Index('uq_observations_source_event', 'source_event_uuid',
              unique=True),
    )


class Assessment(Base):
    """a scored, structured judgment over a bounded subject (a cycle, a
    week) - edwin's artifact. the table landed with the council layer so
    the shape settled early; the phase-6 scorer (cycle_scorer.py) writes
    the rows. the unique (user, subject_type, subject_id) index is the
    exactly-once floor: one subject is scored one time, whatever races -
    scoring is a moment, and late evidence never silently rewrites it.
    subject_id may be null for future free-floating assessments; null rows
    are exempt from the uniqueness (standard sql null semantics).
    """
    __tablename__ = 'assessments'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    helper_id = Column(String, nullable=False)     # the assessor ('edwin')
    subject_type = Column(String, nullable=False)  # 'cycle' | 'week' | ...
    subject_id = Column(String, nullable=True)     # id of the subject row
    summary = Column(String, nullable=False)       # the honest sentence(s)
    detail = Column(JSON, nullable=True)           # score components etc.
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('uq_assessments_subject', 'user_uuid', 'subject_type',
              'subject_id', unique=True),
        {'sqlite_autoincrement': True},
    )


class RewindDecision(Base):
    """the tether's view of one rewind offer (REWIND_DESIGN.md section 8).

    folded from the device's rewind.* events by focus_flow, one row per
    offer_uuid. the row carries only what the ping needs - contested LENGTH
    and the task label - never candidate timestamps (data minimization).
    lifecycle: offered opens (or re-arms) the row; applied/kept close it;
    undone re-opens it fresh. `pinged_at` is the one-ping claim stamp;
    `choice` is the phone's answer, which the sidecar polls for and remains
    free to refuse - the device is authoritative, and the offer's eventual
    terminal event is what closes the row, never the decision itself.
    """
    __tablename__ = 'rewind_decisions'

    id = Column(Integer, primary_key=True)
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    offer_uuid = Column(String, nullable=False, unique=True)
    # the device the offer lives on. offer_uuids are device-local, so
    # presence must be too: a return on the laptop says nothing about the
    # desktop whose card is still waiting in an empty room.
    device_id = Column(Integer, ForeignKey('devices.id'), nullable=True)
    label = Column(String, nullable=True)          # task label, for the copy
    contested_seconds = Column(Integer, nullable=False, default=0)
    offered_at = Column(DateTime, nullable=False)  # latest rewind.offered
    returned_at = Column(DateTime, nullable=True)  # a return.detected since
    pinged_at = Column(DateTime, nullable=True)    # claim stamp: one ping
    ping_platform = Column(String, nullable=True)
    choice = Column(String, nullable=True)         # 'remove' | 'keep'
    decided_at = Column(DateTime, nullable=True)
    source = Column(String, nullable=True)         # 'telegram' | 'discord'
    closed_at = Column(DateTime, nullable=True)    # terminal event landed

    __table_args__ = (
        Index('ix_rewind_decisions_open', 'user_uuid', 'closed_at'),
    )


# --- stuck mode (docs/STUCK_MODE_DESIGN.md sections 5 + 7) -------------------
# one press of "i'm stuck" opens an episode; the turn (or the fallback ladder)
# fills it with three proposals of three kinds; the person's reactions are an
# append-only ledger beside it. generating and browsing proposals changes
# nothing outside these two tables - effects happen only on acceptance.


class StuckEpisode(Base):
    """one press of the button (STUCK_MODE_DESIGN.md section 5.1 + 7).

    lifecycle, server-owned: thinking -> ready | fallback | failed;
    ready -> thinking only when a new generation is owed (the third
    "something different"); ready | fallback -> accepted | rested | closed.
    terminal states reject later model writes, so a slow turn that lands
    after the person already chose cannot rewrite the card under them.

    `proposals` holds every generation's validated set in order, each item
    carrying its server-assigned `proposal_id` and `generation` - the model
    never invents identifiers. `request_uuid` (unique per device) makes the
    create idempotent: a retried press replays the same episode instead of
    opening a second one. `execution_id` is minted once at acceptance and
    the sidecar deduplicates on it before starting or pausing a run.
    `run_ref` and `outcome` are folded later from the device event stream;
    device-local integer run ids are never treated as globally unique.
    """
    __tablename__ = 'stuck_episodes'

    id = Column(Integer, primary_key=True)
    episode_uuid = Column(String, nullable=False, unique=True,
                          default=lambda: str(uuid.uuid4()))
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False,
                       index=True)
    # the device that pressed; null for surfaces without one (rooms, telegram)
    device_id = Column(Integer, ForeignKey('devices.id'), nullable=True)
    request_uuid = Column(String, nullable=False)   # client idempotency key
    surface = Column(String, nullable=False)        # companion|home|tray|room|telegram
    task_id = Column(Integer, ForeignKey('tasks.id'), nullable=True)
    status = Column(String, nullable=False, default='thinking')
    # thinking|ready|fallback|failed|accepted|rested|closed
    generation = Column(Integer, nullable=False, default=1)
    proposals = Column(JSON, nullable=True)         # list of proposal dicts
    error = Column(String, nullable=True)           # why `failed`, if it did
    # the ladder ran out of fresh kinds (or a gen-2 card was fully turned
    # down): the client's cue to show the rest branch; the card stays
    exhausted = Column(Boolean, nullable=False, default=False,
                       server_default=false())
    accepted_proposal_id = Column(String, nullable=True)
    accepted_kind = Column(String, nullable=True)
    execution_id = Column(String, nullable=True)    # minted once, at accept
    run_ref = Column(JSON, nullable=True)           # {device_uuid, event_uuid}
    outcome = Column(JSON, nullable=True)           # folded from device events
    opened_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ready_at = Column(DateTime, nullable=True)      # first card shown
    closed_at = Column(DateTime, nullable=True)     # any terminal state

    __table_args__ = (
        UniqueConstraint('device_id', 'request_uuid',
                         name='uq_stuck_episodes_device_request'),
        Index('ix_stuck_episodes_open', 'user_uuid', 'closed_at'),
        {'sqlite_autoincrement': True},
    )


class StuckReaction(Base):
    """the append-only companion ledger: one row per reaction to a proposal
    (accepted | different | too_much | closed). separate rows make retries
    and concurrent surfaces safe without rewriting a json map on the
    episode; `request_uuid` is unique so a retried react is a replay, never
    a second reaction. `proposal_id` is null for episode-level reactions
    (too_much on the rest branch, closed)."""
    __tablename__ = 'stuck_reactions'

    id = Column(Integer, primary_key=True)
    episode_id = Column(Integer, ForeignKey('stuck_episodes.id'),
                        nullable=False, index=True)
    # denormalized from the episode so tenant-scoped reads never join
    user_uuid = Column(String, ForeignKey('users.uuid'), nullable=False)
    proposal_id = Column(String, nullable=True)
    generation = Column(Integer, nullable=False)
    reaction = Column(String, nullable=False)   # accepted|different|too_much|closed
    request_uuid = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        {'sqlite_autoincrement': True},
    )
