"""stuck_episodes + stuck_reactions: the "i'm stuck" button's ledger

docs/STUCK_MODE_DESIGN.md sections 5 + 7: one press opens an episode; the
turn (or the fallback ladder) fills it with three proposals of three kinds;
reactions are an append-only ledger beside it. request_uuid per device makes
the press idempotent; a unique request_uuid per reaction makes retries
replays. nothing outside these tables changes until a proposal is accepted.

Revision ID: d8f2c4a7e1b9
Revises: c3a9f1e7d2b5
Create Date: 2026-09-09
"""
import sqlalchemy as sa
from alembic import op

revision = 'd8f2c4a7e1b9'
down_revision = 'c3a9f1e7d2b5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'stuck_episodes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('episode_uuid', sa.String(), nullable=False, unique=True),
        sa.Column('user_uuid', sa.String(), sa.ForeignKey('users.uuid'),
                  nullable=False),
        sa.Column('device_id', sa.Integer(), sa.ForeignKey('devices.id'),
                  nullable=True),
        sa.Column('request_uuid', sa.String(), nullable=False),
        sa.Column('surface', sa.String(), nullable=False),
        sa.Column('task_id', sa.Integer(), sa.ForeignKey('tasks.id'),
                  nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('proposals', sa.JSON(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('exhausted', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('accepted_proposal_id', sa.String(), nullable=True),
        sa.Column('accepted_kind', sa.String(), nullable=True),
        sa.Column('execution_id', sa.String(), nullable=True),
        sa.Column('run_ref', sa.JSON(), nullable=True),
        sa.Column('outcome', sa.JSON(), nullable=True),
        sa.Column('opened_at', sa.DateTime(), nullable=False),
        sa.Column('ready_at', sa.DateTime(), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('device_id', 'request_uuid',
                            name='uq_stuck_episodes_device_request'),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_stuck_episodes_user_uuid', 'stuck_episodes',
                    ['user_uuid'])
    op.create_index('ix_stuck_episodes_open', 'stuck_episodes',
                    ['user_uuid', 'closed_at'])
    op.create_table(
        'stuck_reactions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('episode_id', sa.Integer(),
                  sa.ForeignKey('stuck_episodes.id'), nullable=False),
        sa.Column('user_uuid', sa.String(), sa.ForeignKey('users.uuid'),
                  nullable=False),
        sa.Column('proposal_id', sa.String(), nullable=True),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('reaction', sa.String(), nullable=False),
        sa.Column('request_uuid', sa.String(), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_stuck_reactions_episode_id', 'stuck_reactions',
                    ['episode_id'])


def downgrade() -> None:
    op.drop_index('ix_stuck_reactions_episode_id', table_name='stuck_reactions')
    op.drop_table('stuck_reactions')
    op.drop_index('ix_stuck_episodes_open', table_name='stuck_episodes')
    op.drop_index('ix_stuck_episodes_user_uuid', table_name='stuck_episodes')
    op.drop_table('stuck_episodes')
