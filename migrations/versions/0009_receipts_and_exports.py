"""add receipt OCR and export artifacts

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "receipt_imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("original_file_name", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=80), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("upload_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("image_retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_transaction_id", sa.Uuid(), nullable=True),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('created', 'uploading', 'queued', 'preprocessing', 'extracting', "
            "'ready_for_review', 'confirmed', 'committed', 'failed', 'cancelled')",
            name="ck_receipt_import_status",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["business_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index(
        "ix_receipt_imports_user_status_created",
        "receipt_imports",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_receipt_imports_image_retention_until", "receipt_imports", ["image_retention_until"]
    )
    op.create_index("ix_receipt_imports_business_id", "receipt_imports", ["business_id"])
    op.create_index("ix_receipt_imports_user_id", "receipt_imports", ["user_id"])

    op.add_column("transactions", sa.Column("receipt_import_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint(
        "uq_transactions_receipt_import_id", "transactions", ["receipt_import_id"]
    )
    op.create_foreign_key(
        "fk_transactions_receipt_import_id",
        "transactions",
        "receipt_imports",
        ["receipt_import_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "ocr_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_import_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("engine_version", sa.String(length=80), nullable=False),
        sa.Column("preprocessing_version", sa.String(length=80), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("raw_text_object_key", sa.String(length=500), nullable=True),
        sa.Column("structured_extraction", JSON_TYPE, nullable=True),
        sa.Column("confidence_bps", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["receipt_import_id"], ["receipt_imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_import_id", "attempt_number", name="uq_ocr_attempt_number"),
    )
    op.create_index("ix_ocr_attempts_receipt_import_id", "ocr_attempts", ["receipt_import_id"])

    op.create_table(
        "receipt_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_import_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_name", sa.String(length=180), nullable=True),
        sa.Column("merchant_confidence_bps", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurred_at_confidence_bps", sa.Integer(), nullable=True),
        sa.Column("subtotal_idr", sa.BigInteger(), nullable=True),
        sa.Column("tax_idr", sa.BigInteger(), nullable=True),
        sa.Column("service_idr", sa.BigInteger(), nullable=True),
        sa.Column("discount_idr", sa.BigInteger(), nullable=True),
        sa.Column("total_idr", sa.BigInteger(), nullable=False),
        sa.Column("total_confidence_bps", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["receipt_import_id"], ["receipt_imports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_import_id"),
    )
    op.create_table(
        "receipt_draft_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_draft_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("raw_name", sa.String(length=180), nullable=False),
        sa.Column("normalized_name", sa.String(length=180), nullable=False),
        sa.Column("matched_product_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_idr", sa.BigInteger(), nullable=False),
        sa.Column("line_total_idr", sa.BigInteger(), nullable=False),
        sa.Column("confidence_bps", sa.Integer(), nullable=True),
        sa.Column("corrected", sa.Boolean(), nullable=False),
        sa.CheckConstraint("line_total_idr >= 0", name="ck_receipt_draft_item_total_non_negative"),
        sa.CheckConstraint("quantity > 0", name="ck_receipt_draft_item_quantity_positive"),
        sa.CheckConstraint("unit_price_idr >= 0", name="ck_receipt_draft_item_price_non_negative"),
        sa.ForeignKeyConstraint(["matched_product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["receipt_draft_id"], ["receipt_drafts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_draft_id", "position", name="uq_receipt_draft_item_position"),
    )
    op.create_index(
        "ix_receipt_draft_items_receipt_draft_id", "receipt_draft_items", ["receipt_draft_id"]
    )

    op.create_table(
        "export_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=True),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("request_snapshot", JSON_TYPE, nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('analysis_report', 'transaction_summary')", name="ck_export_kind"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'ready', 'failed', 'expired')",
            name="ck_export_status",
        ),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["business_id"], ["business_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint("requested_by_user_id", "idempotency_key", name="uq_export_user_key"),
    )
    op.create_index("ix_export_artifacts_analysis_run_id", "export_artifacts", ["analysis_run_id"])
    op.create_index("ix_export_artifacts_business_id", "export_artifacts", ["business_id"])
    op.create_index(
        "ix_export_artifacts_requested_by_user_id", "export_artifacts", ["requested_by_user_id"]
    )
    op.create_index("ix_export_artifacts_retention_until", "export_artifacts", ["retention_until"])
    op.create_index(
        "ix_export_artifacts_status_retention",
        "export_artifacts",
        ["status", "retention_until"],
    )


def downgrade() -> None:
    op.drop_table("export_artifacts")
    op.drop_table("receipt_draft_items")
    op.drop_table("receipt_drafts")
    op.drop_table("ocr_attempts")
    op.drop_constraint("fk_transactions_receipt_import_id", "transactions", type_="foreignkey")
    op.drop_constraint("uq_transactions_receipt_import_id", "transactions", type_="unique")
    op.drop_column("transactions", "receipt_import_id")
    op.drop_table("receipt_imports")
