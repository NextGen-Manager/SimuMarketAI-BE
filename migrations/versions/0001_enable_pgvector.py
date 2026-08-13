"""enable pgvector

Revision ID: 0001
Revises:
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Enabled now so the extension is not a surprise dependency later. Nothing in
    # the MVP queries it; docs/09 keeps retrieval out of scope.
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))


def downgrade() -> None:
    op.execute(sa.text("DROP EXTENSION IF EXISTS vector"))
