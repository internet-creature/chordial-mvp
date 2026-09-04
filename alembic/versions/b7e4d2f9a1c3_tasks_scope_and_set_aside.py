"""tasks: scope + set aside (the focus dogfood round's server spine)

docs/FOCUS_DOGFOOD_DESIGN.md sections 2-4 and 10: `next_action` is the
one-line scope a focus block runs on; `set_aside_on` parks a task for one
user-local day without changing its status; `set_aside_count` counts the
distinct days it was parked (the breakdown offer's signal); and
`breakdown_offer_dismissed_at` records a declined offer so no device
repeats it. all nullable - existing rows read as unscoped, not parked.

Revision ID: b7e4d2f9a1c3
Revises: d4f8b2a6c917
Create Date: 2026-09-04
"""
import sqlalchemy as sa
from alembic import op

revision = 'b7e4d2f9a1c3'
down_revision = 'd4f8b2a6c917'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tasks') as batch:
        batch.add_column(sa.Column('next_action', sa.String(), nullable=True))
        batch.add_column(sa.Column('set_aside_on', sa.Date(), nullable=True))
        batch.add_column(sa.Column('set_aside_count', sa.Integer(),
                                   nullable=True, server_default='0'))
        batch.add_column(sa.Column('breakdown_offer_dismissed_at',
                                   sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks') as batch:
        batch.drop_column('breakdown_offer_dismissed_at')
        batch.drop_column('set_aside_count')
        batch.drop_column('set_aside_on')
        batch.drop_column('next_action')
