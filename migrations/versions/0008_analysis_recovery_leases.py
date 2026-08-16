"""add analysis recovery leases and terminal event failure codes

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analysis_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "analysis_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_analysis_runs_status_lease",
        "analysis_runs",
        ["status", "lease_expires_at"],
    )
    op.add_column(
        "analysis_events",
        sa.Column("failure_code", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("analysis_events", "failure_code")
    op.drop_index("ix_analysis_runs_status_lease", table_name="analysis_runs")
    op.drop_column("analysis_runs", "attempt_count")
    op.drop_column("analysis_runs", "lease_expires_at")
