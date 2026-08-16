from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.correlation import get_correlation_id
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.artifacts import ObjectStorage
from app.domain.auth import BusinessRole, IdentityContext
from app.persistence.models import ExportArtifact, Product, Transaction, TransactionItem
from app.repositories.business import BusinessRepository
from app.repositories.exports import ExportRepository, ExportWorkerRepository
from app.schemas.exports import ExportRead, TransactionExportCreate
from app.schemas.receipts import SignedTransfer
from app.services.artifact_queue import CeleryExportDispatcher, ExportDispatcher

logger = logging.getLogger(__name__)
DISCLAIMER = "Hasil adalah alat bantu keputusan, bukan jaminan keberhasilan usaha."


class ExportService:
    def __init__(
        self,
        session: AsyncSession,
        identity: IdentityContext,
        settings: Settings,
        storage: ObjectStorage,
        dispatcher: ExportDispatcher | None = None,
    ) -> None:
        self._session = session
        self._identity = identity
        self._settings = settings
        self._storage = storage
        self._dispatcher = dispatcher or CeleryExportDispatcher()
        self._repository = ExportRepository(session, identity)
        self._businesses = BusinessRepository(session)

    async def create_analysis(self, analysis_id: UUID, idempotency_key: str) -> ExportRead:
        expected: dict[str, object] = {"analysis_id": str(analysis_id)}
        existing = await self._repository.find_by_idempotency(idempotency_key)
        if existing:
            self._require_same_export(existing, "analysis_report", expected)
            return await self._read(existing)
        analysis = await self._repository.analysis(analysis_id)
        if analysis is None:
            raise NotFoundError()
        try:
            artifact = await self._repository.create(
                kind="analysis_report",
                analysis_run_id=analysis.id,
                idempotency_key=idempotency_key,
                request_snapshot={"analysis_id": str(analysis.id), "status": analysis.status},
                retention_until=datetime.now(UTC)
                + timedelta(days=self._settings.export_retention_days),
            )
            await self._repository.commit()
        except IntegrityError:
            await self._session.rollback()
            existing_after_race = await self._repository.find_by_idempotency(idempotency_key)
            if existing_after_race is None:
                raise
            artifact = existing_after_race
            self._require_same_export(artifact, "analysis_report", expected)
            return await self._read(artifact)
        self._dispatcher.dispatch(artifact.id, UUID(get_correlation_id()))
        return await self._read(artifact)

    async def create_transaction(
        self, payload: TransactionExportCreate, idempotency_key: str
    ) -> ExportRead:
        expected: dict[str, object] = {
            "business_id": str(payload.business_id),
            "start": payload.start.isoformat(),
            "end": payload.end.isoformat(),
        }
        existing = await self._repository.find_by_idempotency(idempotency_key)
        if existing:
            self._require_same_export(existing, "transaction_summary", expected)
            return await self._read(existing)
        actor = await self._businesses.get_actor(self._identity, payload.business_id)
        if actor is None or actor.role is not BusinessRole.OWNER:
            raise NotFoundError()
        if (
            payload.start.tzinfo is None
            or payload.end.tzinfo is None
            or payload.start > payload.end
        ):
            raise ValidationFailedError("Rentang waktu export tidak valid.")
        try:
            artifact = await self._repository.create(
                kind="transaction_summary",
                business_id=actor.business_id,
                idempotency_key=idempotency_key,
                request_snapshot=expected,
                retention_until=datetime.now(UTC)
                + timedelta(days=self._settings.export_retention_days),
            )
            await self._repository.commit()
        except IntegrityError:
            await self._session.rollback()
            existing_after_race = await self._repository.find_by_idempotency(idempotency_key)
            if existing_after_race is None:
                raise
            artifact = existing_after_race
            self._require_same_export(artifact, "transaction_summary", expected)
            return await self._read(artifact)
        self._dispatcher.dispatch(artifact.id, UUID(get_correlation_id()))
        return await self._read(artifact)

    async def get(self, export_id: UUID) -> ExportRead:
        artifact = await self._repository.get(export_id)
        if artifact is None:
            raise NotFoundError()
        return await self._read(artifact)

    async def _read(self, artifact: ExportArtifact) -> ExportRead:
        download = None
        if artifact.status == "ready" and artifact.object_key:
            expires_at = datetime.now(UTC) + timedelta(
                seconds=self._settings.object_storage_signed_url_seconds
            )
            download = SignedTransfer(
                method="GET",
                url=await self._storage.create_download_url(
                    artifact.object_key,
                    expires_seconds=self._settings.object_storage_signed_url_seconds,
                ),
                expires_at=expires_at,
            )
        return ExportRead(
            export_id=artifact.id,
            kind=artifact.kind,
            status=artifact.status,
            created_at=artifact.created_at,
            download=download,
            failure_code=artifact.failure_code,
        )

    @staticmethod
    def _require_same_export(
        artifact: ExportArtifact, kind: str, expected: dict[str, object]
    ) -> None:
        recorded = {key: artifact.request_snapshot.get(key) for key in expected}
        if artifact.kind != kind or recorded != expected:
            raise ConflictError(
                "Idempotency-Key sudah digunakan untuk permintaan export yang berbeda."
            )


async def execute_export(
    export_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: ObjectStorage,
) -> None:
    async with session_factory() as session:
        repository = ExportWorkerRepository(session)
        artifact = await repository.claim(export_id)
        if artifact is None:
            return
        try:
            if artifact.kind == "analysis_report" and artifact.analysis_run_id:
                report = await repository.analysis_report(artifact.analysis_run_id)
                if report is None:
                    raise ValueError("analysis report unavailable")
                pdf = render_analysis_pdf(report.payload)
            elif artifact.kind == "transaction_summary" and artifact.business_id:
                start = datetime.fromisoformat(str(artifact.request_snapshot["start"]))
                end = datetime.fromisoformat(str(artifact.request_snapshot["end"]))
                transactions, items, products = await repository.transactions(
                    artifact.business_id, start=start, end=end
                )
                pdf = render_transaction_pdf(
                    business_id=artifact.business_id,
                    start=start,
                    end=end,
                    transactions=transactions,
                    items=items,
                    products=products,
                )
            else:
                raise ValueError("unsupported export kind")
            object_key = f"exports/{artifact.requested_by_user_id}/{artifact.id}.pdf"
            await storage.write(object_key, pdf, content_type="application/pdf")
            await repository.ready(
                artifact,
                object_key=object_key,
                sha256=hashlib.sha256(pdf).hexdigest(),
                size_bytes=len(pdf),
            )
        except Exception:
            logger.exception("export_render_failed", extra={"export_id": str(export_id)})
            await repository.fail(artifact, "EXPORT_RENDER_FAILED")


async def purge_expired_artifacts(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: ObjectStorage,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        repository = ExportWorkerRepository(session)
        for artifact in await repository.expired_exports(now):
            if artifact.object_key:
                await storage.delete(artifact.object_key)
            artifact.object_key = None
            artifact.status = "expired"
        receipts = await repository.expired_receipts(now)
        for attempt in await repository.raw_ocr_attempts([receipt.id for receipt in receipts]):
            if attempt.raw_text_object_key:
                await storage.delete(attempt.raw_text_object_key)
                attempt.raw_text_object_key = None
        for receipt in receipts:
            await storage.delete(receipt.object_key)
            receipt.object_key = ""
        await repository.commit()
    _purge_oasis_traces(Path(settings.oasis_trace_root), now, settings.oasis_trace_retention_days)


def _purge_oasis_traces(root: Path, now: datetime, retention_days: int) -> None:
    if not root.exists() or not root.is_dir():
        return
    cutoff = now.timestamp() - timedelta(days=retention_days).total_seconds()
    resolved_root = root.resolve()
    if resolved_root.parent == resolved_root or len(resolved_root.parts) < 2:
        logger.error("unsafe_trace_retention_root", extra={"root": str(resolved_root)})
        return
    for path in root.iterdir():
        resolved = path.resolve()
        if resolved.parent != resolved_root or path.stat().st_mtime > cutoff:
            continue
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            path.rmdir()


def render_analysis_pdf(payload: Mapping[str, object]) -> bytes:
    story = _document_header("Laporan Market Analysis")
    status = {"completed": "Selesai", "partial": "Selesai sebagian"}.get(
        str(payload.get("status")), "Tidak tersedia"
    )
    input_snapshot = _mapping(payload.get("input_snapshot"))
    location = _mapping(input_snapshot.get("location"))
    pricing = _mapping(input_snapshot.get("pricing"))
    operations = _mapping(input_snapshot.get("operations"))
    readiness = _mapping(payload.get("readiness"))
    confidence = _mapping(payload.get("evidence_confidence"))
    market = _mapping(payload.get("market"))
    finance = _mapping(payload.get("finance"))
    simulation = _mapping(payload.get("synthetic_simulation"))
    story.extend(
        [
            _paragraph(f"Status laporan: {status}", _body_style()),
            _paragraph(
                f"Konsep: {input_snapshot.get('concept_name', 'tidak tersedia')}", _body_style()
            ),
            _paragraph(
                f"Lokasi: {location.get('area_name', location.get('area_id', 'tidak tersedia'))}",
                _body_style(),
            ),
            _paragraph(
                f"Launch Readiness Score: {readiness.get('score', 'tidak terdefinisi')}",
                _body_style(),
            ),
            _paragraph(
                f"Rule version: {readiness.get('rule_version', 'tidak tersedia')}", _body_style()
            ),
            _paragraph(
                f"Evidence Confidence: {confidence.get('label', 'tidak tersedia')}", _body_style()
            ),
            Spacer(1, 5 * mm),
            Paragraph("Breakdown skor", _heading_style()),
        ]
    )
    dimensions = readiness.get("dimensions", [])
    if isinstance(dimensions, list) and dimensions:
        rows = [["Dimensi", "Bobot", "Nilai", "Status"]]
        for raw_dimension in dimensions:
            dimension = _mapping(raw_dimension)
            rows.append(
                [
                    str(dimension.get("label", dimension.get("key", "Dimensi"))),
                    f"{dimension.get('weight_percent', 'tidak tersedia')}%",
                    str(dimension.get("score", "tidak dapat dinilai")),
                    str(dimension.get("status", "tidak tersedia")),
                ]
            )
        story.extend([_data_table(rows), Spacer(1, 5 * mm)])

    story.extend(
        [
            Paragraph("Parameter dan kondisi pasar", _heading_style()),
            _paragraph(
                f"Harga jual rencana: {_format_idr(pricing.get('average_selling_price_idr'))}",
                _body_style(),
            ),
            _paragraph(
                f"Modal awal: {_format_idr(operations.get('initial_investment_idr'))}",
                _body_style(),
            ),
            _paragraph(
                f"Kompetitor sejenis: {_available_number(market.get('competitor_count'))}; "
                f"sampel harga pembanding: "
                f"{_available_number(market.get('comparable_price_sample_size'))}; "
                f"taxonomy {market.get('category_mapping_version', 'tidak tersedia')}",
                _body_style(),
            ),
            Spacer(1, 5 * mm),
            Paragraph("Proyeksi finansial deterministik", _heading_style()),
            _paragraph(
                f"Marjin kontribusi per unit: "
                f"{_format_idr(finance.get('contribution_margin_per_unit_idr'))}",
                _body_style(),
            ),
            _paragraph(
                f"BEP unit per bulan: {_available_number(finance.get('bep_units_month'))}; "
                f"BEP pendapatan per bulan: "
                f"{_format_idr(finance.get('bep_revenue_month_idr'))}; "
                f"rule {finance.get('rule_version', 'tidak tersedia')}",
                _body_style(),
            ),
        ]
    )
    scenarios = finance.get("scenarios", [])
    if isinstance(scenarios, list) and scenarios:
        rows = [["Skenario", "Unit/bulan", "Pendapatan", "Profit", "Payback"]]
        for raw_scenario in scenarios:
            scenario = _mapping(raw_scenario)
            payback = scenario.get("payback_months")
            rows.append(
                [
                    str(scenario.get("label", scenario.get("name", "Skenario"))),
                    str(scenario.get("monthly_units", "tidak tersedia")),
                    _format_idr(scenario.get("monthly_revenue_idr")),
                    _format_idr(scenario.get("monthly_operating_profit_idr")),
                    f"{payback} bulan" if isinstance(payback, int) else "tidak terdefinisi",
                ]
            )
        story.extend([_data_table(rows), Spacer(1, 4 * mm)])
    story.extend(_string_list("Asumsi yang termasuk", finance.get("assumptions_included")))
    story.extend(_string_list("Asumsi yang belum termasuk", finance.get("assumptions_excluded")))

    story.extend([Spacer(1, 5 * mm), Paragraph("Simulasi persona", _heading_style())])
    if simulation.get("status") == "experimental":
        story.append(
            _paragraph(
                f"Respons sintetis. Cohort {simulation.get('cohort_size', 'tidak tersedia')} "
                f"persona, versi {simulation.get('cohort_version', 'tidak tersedia')}. "
                "Hasil ini eksploratif dan bukan survei pelanggan nyata.",
                _body_style(),
            )
        )
        metrics = _mapping(simulation.get("metrics"))
        if metrics:
            story.append(
                _paragraph(
                    "Hitungan persona: "
                    + ", ".join(f"{key}={value}" for key, value in metrics.items()),
                    _body_style(),
                )
            )
        quotes = simulation.get("quotes", [])
        if isinstance(quotes, list):
            for raw_quote in quotes:
                quote = _mapping(raw_quote)
                story.append(
                    _paragraph(
                        f"{quote.get('text', '')} ({quote.get('label', 'respons sintetis')})",
                        _body_style(),
                    )
                )
    else:
        story.append(
            _paragraph(
                f"Tidak tersedia: {simulation.get('reason', 'simulasi tidak dijalankan')}",
                _body_style(),
            )
        )
    story.extend(_record_list("Risiko utama", payload.get("risks"), "title", "detail"))
    story.extend(_record_list("Rekomendasi", payload.get("recommendations"), "title", "rationale"))
    story.extend([Spacer(1, 5 * mm), Paragraph("Bukti dan provenance", _heading_style())])
    evidence = payload.get("evidence", [])
    if isinstance(evidence, list) and evidence:
        for item in evidence:
            entry = _mapping(item)
            story.append(
                _paragraph(
                    f"{entry.get('metric', 'Metrik')}: {entry.get('value', 'tidak tersedia')} "
                    f"{entry.get('unit', '')}; sumber {entry.get('source', 'tidak tersedia')}; "
                    f"observasi {entry.get('observed_at', 'tidak tersedia')}; "
                    f"diambil {entry.get('retrieved_at', 'tidak tersedia')}",
                    _body_style(),
                )
            )
    else:
        story.append(Paragraph("Tidak ada bukti pasar yang tersedia.", _body_style()))
    missing = confidence.get("missing", [])
    if isinstance(missing, list) and missing:
        story.append(
            _paragraph(f"Bukti yang belum tersedia: {', '.join(map(str, missing))}", _body_style())
        )
    story.extend(_string_list("Keterbatasan", payload.get("limitations")))
    story.extend(
        [
            Spacer(1, 5 * mm),
            Paragraph("Disclaimer", _heading_style()),
            _paragraph(str(payload.get("disclaimer", DISCLAIMER)), _body_style()),
        ]
    )
    return _build_pdf(story)


def render_transaction_pdf(
    *,
    business_id: UUID,
    start: datetime,
    end: datetime,
    transactions: list[Transaction],
    items: list[TransactionItem],
    products: dict[UUID, Product],
) -> bytes:
    revenue = sum(transaction.gross_total_idr for transaction in transactions)
    quantities: dict[UUID, int] = defaultdict(int)
    product_revenue: dict[UUID, int] = defaultdict(int)
    for item in items:
        quantities[item.product_id] += item.quantity
        product_revenue[item.product_id] += item.line_total_idr
    story = _document_header("Ringkasan Transaksi")
    story.extend(
        [
            _paragraph(f"Usaha: {business_id}", _body_style()),
            _paragraph(f"Periode: {start.isoformat()} sampai {end.isoformat()}", _body_style()),
            _paragraph(f"Jumlah transaksi: {len(transactions)}", _body_style()),
            _paragraph(f"Pendapatan tercatat: {_format_idr(revenue)}", _body_style()),
            _paragraph(
                "Sumber: transaksi terkonfirmasi pada PostgreSQL. "
                f"Laporan dibuat {datetime.now(UTC).isoformat()}.",
                _body_style(),
            ),
            Spacer(1, 5 * mm),
            Paragraph("Ringkasan produk", _heading_style()),
        ]
    )
    rows = [["Produk", "Jumlah", "Pendapatan"]]
    for product_id in sorted(quantities, key=lambda key: quantities[key], reverse=True):
        product = products.get(product_id)
        rows.append(
            [
                product.name if product else "Produk tidak tersedia",
                str(quantities[product_id]),
                f"Rp {product_revenue[product_id]:,}".replace(",", "."),
            ]
        )
    table = _data_table(rows, widths=[90 * mm, 25 * mm, 45 * mm])
    story.extend([table, Spacer(1, 6 * mm), Paragraph(DISCLAIMER, _body_style())])
    return _build_pdf(story)


def _document_header(title: str) -> list[object]:
    return [
        Paragraph("SimuMarket AI", _heading_style()),
        Paragraph(title, _title_style()),
        Spacer(1, 5 * mm),
    ]


def _build_pdf(story: list[object]) -> bytes:
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="SimuMarket AI",
    )
    document.build(story)
    return output.getvalue()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _paragraph(value: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(value), style)


def _format_idr(value: object) -> str:
    if not isinstance(value, int):
        return "tidak terdefinisi"
    return f"Rp {value:,}".replace(",", ".")


def _available_number(value: object) -> str:
    return str(value) if isinstance(value, int) else "tidak tersedia"


def _string_list(title: str, value: object) -> list[object]:
    if not isinstance(value, list) or not value:
        return []
    story: list[object] = [Paragraph(title, _heading_style())]
    story.extend(_paragraph(f"- {item}", _body_style()) for item in value if isinstance(item, str))
    return story


def _record_list(title: str, value: object, heading_key: str, detail_key: str) -> list[object]:
    if not isinstance(value, list) or not value:
        return []
    story: list[object] = [Spacer(1, 4 * mm), Paragraph(title, _heading_style())]
    for raw_item in value:
        item = _mapping(raw_item)
        story.append(
            _paragraph(
                f"{item.get(heading_key, 'Tanpa judul')}: "
                f"{item.get(detail_key, 'tidak tersedia')}; "
                f"sumber {item.get('source', 'tidak tersedia')}",
                _body_style(),
            )
        )
    return story


def _data_table(rows: list[list[str]], *, widths: list[float] | None = None) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F4F1")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5D1")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _title_style() -> ParagraphStyle:
    return ParagraphStyle(
        "TitleCompact", parent=getSampleStyleSheet()["Title"], fontName="Helvetica-Bold"
    )


def _heading_style() -> ParagraphStyle:
    return ParagraphStyle(
        "HeadingCompact", parent=getSampleStyleSheet()["Heading2"], fontName="Helvetica-Bold"
    )


def _body_style() -> ParagraphStyle:
    return ParagraphStyle("BodyCompact", parent=getSampleStyleSheet()["BodyText"], leading=15)
