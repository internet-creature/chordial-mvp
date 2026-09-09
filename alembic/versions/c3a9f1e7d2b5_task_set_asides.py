"""task_set_asides: the set-aside ledger

docs/FOCUS_DOGFOOD_DESIGN.md section 2 + 5: one row per (task, user-local
day) a task was parked on. `tasks.set_aside_on` is the mutable current
stamp - "tomorrow" and "bring back" clear it - so reads of a PAST day
(yesterday's digest under the morning brief, "was it on the list
yesterday") need durable history. backfilled from the current stamps so
today's parked tasks are in the ledger from the first run.

Revision ID: c3a9f1e7d2b5
Revises: b7e4d2f9a1c3
Create Date: 2026-09-09
"""
import sqlalchemy as sa
from alembic import op

revision = 'c3a9f1e7d2b5'
down_revision = 'b7e4d2f9a1c3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'task_set_asides',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_uuid', sa.String(), sa.ForeignKey('users.uuid'),
                  nullable=False),
        sa.Column('task_id', sa.Integer(), sa.ForeignKey('tasks.id'),
                  nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('task_id', 'day',
                            name='uq_task_set_asides_task_day'),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_task_set_asides_user_day', 'task_set_asides',
                    ['user_uuid', 'day'])
    op.execute(
        "INSERT INTO task_set_asides (user_uuid, task_id, day, created_at) "
        "SELECT user_uuid, id, set_aside_on, CURRENT_TIMESTAMP FROM tasks "
        "WHERE set_aside_on IS NOT NULL")


def downgrade() -> None:
    op.drop_index('ix_task_set_asides_user_day', table_name='task_set_asides')
    op.drop_table('task_set_asides')
