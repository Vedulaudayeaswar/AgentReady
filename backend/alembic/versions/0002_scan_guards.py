"""one active scan per website

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fail any duplicate active scans left over from before this constraint existed.
    op.execute("""
        UPDATE scan_jobs SET status = 'failed'
        WHERE status IN ('queued', 'running') AND id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY website_id ORDER BY created_at DESC) AS rn
                FROM scan_jobs WHERE status IN ('queued', 'running')
            ) ranked WHERE rn = 1
        )
    """)
    op.create_index(
        "uq_scan_jobs_one_active_per_website", "scan_jobs", ["website_id"], unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_products_business_updated", "products", ["business_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_products_business_updated", table_name="products")
    op.drop_index("uq_scan_jobs_one_active_per_website", table_name="scan_jobs")
