from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeEngine

from app.persistence.database import Base

# JSONB on PostgreSQL, plain JSON elsewhere so the same models run under SQLite in tests.
JsonType: TypeEngine[object] = JSON().with_variant(postgresql.JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    replaced_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="SET NULL")
    )


class BusinessProfile(Base):
    __tablename__ = "business_profiles"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120))
    location_name: Mapped[str] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "business_id", name="uq_membership_user_business"),
        CheckConstraint("role IN ('owner', 'cashier')", name="ck_membership_role"),
        Index("ix_memberships_business_role", "business_id", "role"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_profiles.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BusinessInvite(Base):
    __tablename__ = "business_invites"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_profiles.id", ondelete="CASCADE"), index=True
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redeemed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("business_id", "name", name="uq_product_business_name"),
        CheckConstraint("selling_price_idr >= 0", name="ck_product_selling_price_non_negative"),
        CheckConstraint("hpp_idr >= 0", name="ck_product_hpp_non_negative"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_profiles.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    selling_price_idr: Mapped[int] = mapped_column(BigInteger)
    hpp_idr: Mapped[int] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "client_reference", name="uq_transaction_business_client_reference"
        ),
        CheckConstraint("gross_total_idr >= 0", name="ck_transaction_total_non_negative"),
        CheckConstraint(
            "channel IN ('dine_in', 'takeaway', 'delivery')", name="ck_transaction_channel"
        ),
        Index("ix_transactions_business_occurred", "business_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_profiles.id", ondelete="CASCADE"), index=True
    )
    recorded_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    gross_total_idr: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16), default="manual")
    client_reference: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TransactionItem(Base):
    __tablename__ = "transaction_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_transaction_item_quantity_positive"),
        CheckConstraint("unit_price_idr >= 0", name="ck_transaction_item_price_non_negative"),
        CheckConstraint("line_total_idr >= 0", name="ck_transaction_item_total_non_negative"),
        Index("ix_transaction_items_product_transaction", "product_id", "transaction_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_idr: Mapped[int] = mapped_column(BigInteger)
    line_total_idr: Mapped[int] = mapped_column(BigInteger)


class InputSnapshot(Base):
    """Frozen analysis input.

    Written once when a run is created and never updated, so a report can always
    be traced back to exactly what the user submitted.
    """

    __tablename__ = "input_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    payload: Mapped[dict[str, object]] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="ck_analysis_score"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_analysis_user_idempotency"),
        Index("ix_analysis_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24))
    concept_name: Mapped[str] = mapped_column(String(120))
    area_name: Mapped[str] = mapped_column(String(180))
    score: Mapped[int | None] = mapped_column(Integer)
    interpretation: Mapped[str | None] = mapped_column(String(120))
    rule_version: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    business_type: Mapped[str] = mapped_column(String(32), default="food_stall")
    current_stage: Mapped[str] = mapped_column(String(32), default="queued")
    completed_stages: Mapped[list[str]] = mapped_column(JsonType, default=list)
    skipped_stages: Mapped[list[str]] = mapped_column(JsonType, default=list)
    warnings: Mapped[list[dict[str, object]]] = mapped_column(JsonType, default=list)
    input_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("input_snapshots.id", ondelete="RESTRICT")
    )
    evidence_snapshot_version: Mapped[str | None] = mapped_column(String(80))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True, default=uuid4)
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    # Model, prompt, and package versions of the simulation attempt, per docs/10.
    # A provider alias can move without notice, so a run that cannot name the
    # exact model it used cannot be compared against any other run.
    oasis_version: Mapped[str | None] = mapped_column(String(40))
    camel_version: Mapped[str | None] = mapped_column(String(40))
    model_manifest: Mapped[dict[str, object] | None] = mapped_column(JsonType)
    prompt_manifest: Mapped[dict[str, object] | None] = mapped_column(JsonType)


class AnalysisEventRecord(Base):
    """One persisted stage transition.

    PostgreSQL is the system of record for progress, not Redis. A stream that
    reconnects after a broker outage replays from here, and `sequence` doubles
    as the SSE `Last-Event-ID`.
    """

    __tablename__ = "analysis_events"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "sequence", name="uq_analysis_event_sequence"),
        Index("ix_analysis_events_run_sequence", "analysis_run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    current_stage: Mapped[str] = mapped_column(String(32))
    completed_stages: Mapped[list[str]] = mapped_column(JsonType, default=list)
    skipped_stages: Mapped[list[str]] = mapped_column(JsonType, default=list)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(300))
    warnings: Mapped[list[dict[str, object]]] = mapped_column(JsonType, default=list)
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentRun(Base):
    """One council execution inside an analysis run."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "agent_type", name="uq_agent_run_type"),
        CheckConstraint("status IN ('completed', 'failed', 'skipped')", name="ck_agent_run_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    agent_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    model_id: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(80))
    cohort_version: Mapped[str | None] = mapped_column(String(80))
    seed: Mapped[int] = mapped_column(Integer)
    persona_count: Mapped[int] = mapped_column(Integer, default=0)
    round_limit: Mapped[int] = mapped_column(Integer, default=0)
    token_budget: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    schema_failures: Mapped[int] = mapped_column(Integer, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentInstance(Base):
    """A single personality instance within a council run."""

    __tablename__ = "agent_instances"
    __table_args__ = (
        Index("ix_agent_instances_run_order", "agent_run_id", "activation_order"),
        CheckConstraint(
            "outcome IN ('completed', 'failed', 'skipped')", name="ck_agent_instance_outcome"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    agent_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    archetype: Mapped[str | None] = mapped_column(String(64))
    profile_version: Mapped[str] = mapped_column(String(80))
    model_id: Mapped[str] = mapped_column(String(120))
    allowed_actions: Mapped[list[str]] = mapped_column(JsonType, default=list)
    activation_order: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str] = mapped_column(String(16))


class AgentArtifact(Base):
    """A schema-validated artifact emitted by a council.

    Raw conversation is not copied here. docs/10 keeps the transcript in the
    trace object and stores the typed result plus a checksum, so a report can be
    reconciled without duplicating a whole deliberation into the database.
    """

    __tablename__ = "agent_artifacts"
    __table_args__ = (
        Index("ix_agent_artifacts_run_type", "analysis_run_id", "artifact_type"),
        CheckConstraint(
            "validation_status IN ('valid', 'rejected')", name="ck_agent_artifact_validation"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    agent_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JsonType)
    source_artifact_ids: Mapped[list[str]] = mapped_column(JsonType, default=list)
    validation_status: Mapped[str] = mapped_column(String(16))
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentTraceArtifact(Base):
    """Pointer to one run's OASIS trace, never the trace content itself."""

    __tablename__ = "agent_trace_artifacts"
    __table_args__ = (UniqueConstraint("object_key", name="uq_agent_trace_object_key"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    environment_id: Mapped[str] = mapped_column(String(120))
    object_key: Mapped[str] = mapped_column(String(500))
    checksum: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    access_scope: Mapped[str] = mapped_column(String(32), default="owner_only")
    manifest: Mapped[dict[str, object]] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        Index("ix_evidence_items_run_metric", "analysis_run_id", "metric"),
        Index("ix_evidence_items_metric_observed", "metric", "observed_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    metric: Mapped[str] = mapped_column(String(80))
    value: Mapped[int] = mapped_column(BigInteger)
    unit: Mapped[str] = mapped_column(String(40))
    geography: Mapped[dict[str, object]] = mapped_column(JsonType)
    category_mapping_version: Mapped[str | None] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str | None] = mapped_column(String(500))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quality: Mapped[dict[str, object]] = mapped_column(JsonType)
    limitations: Mapped[list[str]] = mapped_column(JsonType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AnalysisReportRecord(Base):
    __tablename__ = "analysis_reports"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), unique=True
    )
    report_version: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, object]] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EducationModule(Base):
    __tablename__ = "education_modules"
    __table_args__ = (
        CheckConstraint(
            "passing_score_percent BETWEEN 0 AND 100", name="ck_education_passing_score"
        ),
        Index("ix_education_modules_published", "published_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    title: Mapped[str] = mapped_column(String(180))
    summary: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(String(80))
    body: Mapped[str | None] = mapped_column(Text)
    content_version: Mapped[str] = mapped_column(String(40))
    business_types: Mapped[list[str]] = mapped_column(JsonType, default=list)
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=10)
    passing_score_percent: Mapped[int] = mapped_column(Integer, default=70)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class EducationQuestion(Base):
    __tablename__ = "education_questions"
    __table_args__ = (
        UniqueConstraint("module_id", "position", name="uq_education_question_position"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    module_id: Mapped[UUID] = mapped_column(
        ForeignKey("education_modules.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JsonType)
    correct_index: Mapped[int] = mapped_column(Integer)
    explanation: Mapped[str | None] = mapped_column(Text)


class EducationProgress(Base):
    __tablename__ = "education_progress"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "module_id",
            "content_version",
            name="uq_education_progress_user_module_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    module_id: Mapped[UUID] = mapped_column(
        ForeignKey("education_modules.id", ondelete="CASCADE"), index=True
    )
    # Progress points at the content version it was earned against, so an audit
    # can tell whether a completion still refers to the material now published.
    content_version: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correct_answers: Mapped[int] = mapped_column(Integer, default=0)
    total_questions: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_actor_created", "actor_user_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[UUID | None] = mapped_column(Uuid)
    outcome: Mapped[str] = mapped_column(String(20))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
