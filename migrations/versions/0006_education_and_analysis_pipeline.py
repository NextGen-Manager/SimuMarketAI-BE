"""add education content, analysis pipeline state, evidence, and reports

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB()


def upgrade() -> None:
    op.create_table(
        "input_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "education_modules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("topic", sa.String(length=80), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("content_version", sa.String(length=40), nullable=False),
        sa.Column("business_types", JSONB, nullable=False),
        sa.Column("estimated_minutes", sa.Integer(), nullable=False),
        sa.Column("passing_score_percent", sa.Integer(), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "passing_score_percent BETWEEN 0 AND 100", name="ck_education_passing_score"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_education_modules_published", "education_modules", ["published_at"])

    op.create_table(
        "education_questions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("module_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("options", JSONB, nullable=False),
        sa.Column("correct_index", sa.Integer(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["module_id"], ["education_modules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("module_id", "position", name="uq_education_question_position"),
    )
    op.create_index("ix_education_questions_module_id", "education_questions", ["module_id"])

    op.create_table(
        "education_progress",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("module_id", sa.Uuid(), nullable=False),
        sa.Column("content_version", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correct_answers", sa.Integer(), nullable=False),
        sa.Column("total_questions", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["module_id"], ["education_modules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "module_id",
            "content_version",
            name="uq_education_progress_user_module_version",
        ),
    )
    op.create_index("ix_education_progress_module_id", "education_progress", ["module_id"])
    op.create_index("ix_education_progress_user_id", "education_progress", ["user_id"])

    op.add_column(
        "analysis_runs",
        sa.Column(
            "business_type",
            sa.String(length=32),
            nullable=False,
            server_default="food_stall",
        ),
    )
    op.add_column(
        "analysis_runs",
        sa.Column("current_stage", sa.String(length=32), nullable=False, server_default="queued"),
    )
    op.add_column(
        "analysis_runs",
        sa.Column("completed_stages", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "analysis_runs",
        sa.Column("skipped_stages", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "analysis_runs",
        sa.Column("warnings", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column("analysis_runs", sa.Column("input_snapshot_id", sa.Uuid(), nullable=True))
    op.add_column(
        "analysis_runs", sa.Column("evidence_snapshot_version", sa.String(length=80), nullable=True)
    )
    op.add_column("analysis_runs", sa.Column("correlation_id", sa.Uuid(), nullable=True))
    op.add_column(
        "analysis_runs", sa.Column("idempotency_key", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "analysis_runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "analysis_runs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("analysis_runs", sa.Column("failure_code", sa.String(length=80), nullable=True))

    # Rows written before this revision have no correlation ID; give them one so
    # the column can carry the not-null guarantee the model declares.
    op.execute(
        "UPDATE analysis_runs SET correlation_id = gen_random_uuid() WHERE correlation_id IS NULL"
    )
    op.alter_column("analysis_runs", "correlation_id", nullable=False)

    for column in (
        "business_type",
        "current_stage",
        "completed_stages",
        "skipped_stages",
        "warnings",
    ):
        op.alter_column("analysis_runs", column, server_default=None)

    op.create_foreign_key(
        "fk_analysis_runs_input_snapshot",
        "analysis_runs",
        "input_snapshots",
        ["input_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_analysis_user_idempotency", "analysis_runs", ["user_id", "idempotency_key"]
    )
    op.create_index("ix_analysis_runs_correlation_id", "analysis_runs", ["correlation_id"])

    op.create_table(
        "evidence_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("metric", sa.String(length=80), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.String(length=40), nullable=False),
        sa.Column("geography", JSONB, nullable=False),
        sa.Column("category_mapping_version", sa.String(length=80), nullable=True),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality", JSONB, nullable=False),
        sa.Column("limitations", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_items_analysis_run_id", "evidence_items", ["analysis_run_id"])
    op.create_index(
        "ix_evidence_items_metric_observed", "evidence_items", ["metric", "observed_at"]
    )
    op.create_index("ix_evidence_items_run_metric", "evidence_items", ["analysis_run_id", "metric"])

    op.create_table(
        "analysis_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("report_version", sa.String(length=40), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_run_id"),
    )


def downgrade() -> None:
    op.drop_table("analysis_reports")
    op.drop_index("ix_evidence_items_run_metric", table_name="evidence_items")
    op.drop_index("ix_evidence_items_metric_observed", table_name="evidence_items")
    op.drop_index("ix_evidence_items_analysis_run_id", table_name="evidence_items")
    op.drop_table("evidence_items")

    op.drop_index("ix_analysis_runs_correlation_id", table_name="analysis_runs")
    op.drop_constraint("uq_analysis_user_idempotency", "analysis_runs", type_="unique")
    op.drop_constraint("fk_analysis_runs_input_snapshot", "analysis_runs", type_="foreignkey")
    for column in (
        "failure_code",
        "completed_at",
        "started_at",
        "idempotency_key",
        "correlation_id",
        "evidence_snapshot_version",
        "input_snapshot_id",
        "warnings",
        "skipped_stages",
        "completed_stages",
        "current_stage",
        "business_type",
    ):
        op.drop_column("analysis_runs", column)

    op.drop_index("ix_education_progress_user_id", table_name="education_progress")
    op.drop_index("ix_education_progress_module_id", table_name="education_progress")
    op.drop_table("education_progress")
    op.drop_index("ix_education_questions_module_id", table_name="education_questions")
    op.drop_table("education_questions")
    op.drop_index("ix_education_modules_published", table_name="education_modules")
    op.drop_table("education_modules")
    op.drop_table("input_snapshots")
