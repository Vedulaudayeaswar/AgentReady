"""production readiness: alert evaluation time for the daily notification frequency

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08

Additive only (one nullable column); safe to apply while the application is running.
Existing merchants start with NULL, i.e. their next intelligence pass evaluates alerts for the
last 24 hours, exactly as a new merchant would.
"""

from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("intelligence_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("intelligence_settings", schema=None) as batch_op:
        batch_op.drop_column("last_notified_at")
