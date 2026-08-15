"""add analysis progress events, agent runs, artifacts, and trace metadata

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB()


def upgrade() -> None:
    op.add_column("analysis_runs", sa.Column("oasis_version", sa.String(length=40), nullable=True))
    op.add_column("analysis_runs", sa.Column("camel_version", sa.String(length=40), nullable=True))
    op.add_column("analysis_runs", sa.Column("model_manifest", JSONB, nullable=True))
    op.add_column("analysis_runs", sa.Column("prompt_manifest", JSONB, nullable=True))

    op.create_table(
        "analysis_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("current_stage", sa.String(length=32), nullable=False),
        sa.Column("completed_stages", JSONB, nullable=False),
        sa.Column("skipped_stages", JSONB, nullable=False),
        sa.Column("percent", sa.Integer(), nullable=False),
        sa.Column("message", sa.String(length=300), nullable=False),
        sa.Column("warnings", JSONB, nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_run_id", "sequence", name="uq_analysis_event_sequence"),
    )
    op.create_index("ix_analysis_events_analysis_run_id", "analysis_events", ["analysis_run_id"])
    op.create_index("ix_analysis_events_correlation_id", "analysis_events", ["correlation_id"])
    op.create_index(
        "ix_analysis_events_run_sequence", "analysis_events", ["analysis_run_id", "sequence"]
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("cohort_version", sa.String(length=80), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("persona_count", sa.Integer(), nullable=False),
        sa.Column("round_limit", sa.Integer(), nullable=False),
        sa.Column("token_budget", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("schema_failures", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('completed', 'failed', 'skipped')", name="ck_agent_run_status"
        ),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_run_id", "agent_type", name="uq_agent_run_type"),
    )
    op.create_index("ix_agent_runs_analysis_run_id", "agent_runs", ["analysis_run_id"])

    op.create_table(
        "agent_instances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("archetype", sa.String(length=64), nullable=True),
        sa.Column("profile_version", sa.String(length=80), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("allowed_actions", JSONB, nullable=False),
        sa.Column("activation_order", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('completed', 'failed', 'skipped')", name="ck_agent_instance_outcome"
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_instances_agent_run_id", "agent_instances", ["agent_run_id"])
    op.create_index(
        "ix_agent_instances_run_order", "agent_instances", ["agent_run_id", "activation_order"]
    )

    op.create_table(
        "agent_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("source_artifact_ids", JSONB, nullable=False),
        sa.Column("validation_status", sa.String(length=16), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "validation_status IN ('valid', 'rejected')", name="ck_agent_artifact_validation"
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_artifacts_agent_run_id", "agent_artifacts", ["agent_run_id"])
    op.create_index("ix_agent_artifacts_analysis_run_id", "agent_artifacts", ["analysis_run_id"])
    op.create_index(
        "ix_agent_artifacts_run_type", "agent_artifacts", ["analysis_run_id", "artifact_type"]
    )

    op.create_table(
        "agent_trace_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.String(length=120), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("access_scope", sa.String(length=32), nullable=False),
        sa.Column("manifest", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key", name="uq_agent_trace_object_key"),
    )
    op.create_index(
        "ix_agent_trace_artifacts_analysis_run_id", "agent_trace_artifacts", ["analysis_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_trace_artifacts_analysis_run_id", table_name="agent_trace_artifacts")
    op.drop_table("agent_trace_artifacts")

    op.drop_index("ix_agent_artifacts_run_type", table_name="agent_artifacts")
    op.drop_index("ix_agent_artifacts_analysis_run_id", table_name="agent_artifacts")
    op.drop_index("ix_agent_artifacts_agent_run_id", table_name="agent_artifacts")
    op.drop_table("agent_artifacts")

    op.drop_index("ix_agent_instances_run_order", table_name="agent_instances")
    op.drop_index("ix_agent_instances_agent_run_id", table_name="agent_instances")
    op.drop_table("agent_instances")

    op.drop_index("ix_agent_runs_analysis_run_id", table_name="agent_runs")
    op.drop_table("agent_runs")

    op.drop_index("ix_analysis_events_run_sequence", table_name="analysis_events")
    op.drop_index("ix_analysis_events_correlation_id", table_name="analysis_events")
    op.drop_index("ix_analysis_events_analysis_run_id", table_name="analysis_events")
    op.drop_table("analysis_events")

    for column in ("prompt_manifest", "model_manifest", "camel_version", "oasis_version"):
        op.drop_column("analysis_runs", column)
