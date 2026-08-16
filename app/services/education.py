from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from app.core.errors import (
    ConflictError,
    EducationContentInvalidError,
    NotFoundError,
    ValidationFailedError,
)
from app.domain.auth import IdentityContext
from app.domain.taxonomy import BusinessType
from app.persistence.models import EducationModule, EducationProgress
from app.repositories.business import BusinessRepository
from app.repositories.education import EducationRepository
from app.schemas.education import (
    EducationCompleteRequest,
    EducationCompleteResponse,
    EducationModuleDetail,
    EducationModuleSummary,
    EducationPrerequisiteModule,
    EducationPrerequisites,
    EducationProgressRead,
    EducationQuestionRead,
)
from app.services.access import require_owner_workspace

NO_PUBLISHED_CONTENT_NOTE = (
    "Belum ada modul edukasi prasyarat yang terbit untuk jenis usaha ini. "
    "Analisis belum dapat dijalankan sampai materi tersedia."
)


class EducationService:
    def __init__(
        self,
        repository: EducationRepository,
        businesses: BusinessRepository,
        identity: IdentityContext,
    ) -> None:
        self._repository = repository
        self._businesses = businesses
        self._identity = identity

    async def list_modules(
        self, business_type: BusinessType | None
    ) -> list[EducationModuleSummary]:
        await self._require_owner()
        modules = await self._relevant_modules(business_type)
        progress = await self._repository.progress_for([module.id for module in modules])
        return [
            self._summary(module, progress.get((module.id, module.content_version)))
            for module in modules
        ]

    async def get_module(self, module_id: UUID) -> EducationModuleDetail:
        await self._require_owner()
        module = await self._repository.get_published(module_id)
        if module is None:
            raise NotFoundError()
        questions = await self._repository.questions_for(module.id)
        progress = await self._repository.get_progress(module.id, module.content_version)
        summary = self._summary(module, progress)
        return EducationModuleDetail(
            **summary.model_dump(),
            body=module.body,
            passing_score_percent=module.passing_score_percent,
            questions=[
                EducationQuestionRead(
                    id=question.id,
                    position=question.position,
                    prompt=question.prompt,
                    # The answer key never leaves the server.
                    options=list(question.options),
                )
                for question in questions
            ],
        )

    async def complete_module(
        self, module_id: UUID, payload: EducationCompleteRequest
    ) -> EducationCompleteResponse:
        await self._require_owner()
        module = await self._repository.get_published(module_id)
        if module is None:
            raise NotFoundError()
        if payload.content_version != module.content_version:
            raise ConflictError(
                "Versi konten modul sudah berubah. Muat ulang modul lalu kerjakan lagi."
            )

        questions = await self._repository.questions_for(module.id)
        if not questions:
            raise EducationContentInvalidError()
        if len(payload.answers) != len(questions):
            raise ValidationFailedError("Jumlah jawaban tidak sesuai dengan jumlah pertanyaan.")

        correct = sum(
            1
            for question, answer in zip(questions, payload.answers, strict=True)
            if answer == question.correct_index
        )
        total = len(questions)
        percent = (correct * 100) // total
        passed = percent >= module.passing_score_percent

        now = datetime.now(UTC)
        progress = await self._repository.get_progress(module.id, module.content_version)
        if progress is None:
            progress = EducationProgress(
                user_id=self._identity.user_id,
                module_id=module.id,
                content_version=module.content_version,
                started_at=now,
                correct_answers=correct,
                total_questions=total,
                passed=passed,
                completed_at=now if passed else None,
            )
            self._repository.add_progress(progress)
        else:
            progress.correct_answers = correct
            progress.total_questions = total
            # A completion already earned on this content version is not revoked
            # by a later weaker attempt.
            if passed and not progress.passed:
                progress.passed = True
                progress.completed_at = now
        await self._repository.commit()

        return EducationCompleteResponse(
            module_id=module.id,
            content_version=module.content_version,
            passed=progress.passed,
            correct_answers=correct,
            total_questions=total,
            passing_score_percent=module.passing_score_percent,
            completed_at=progress.completed_at,
        )

    async def prerequisites(self, business_type: BusinessType) -> EducationPrerequisites:
        await self._require_owner()
        modules = await self._relevant_modules(business_type)
        required = [module for module in modules if module.is_required]
        progress = await self._repository.progress_for([module.id for module in required])

        entries: list[EducationPrerequisiteModule] = []
        for module in required:
            record = progress.get((module.id, module.content_version))
            entries.append(
                EducationPrerequisiteModule(
                    id=module.id,
                    slug=module.slug,
                    title=module.title,
                    content_version=module.content_version,
                    estimated_minutes=module.estimated_minutes,
                    completed=bool(record is not None and record.passed),
                )
            )

        outstanding = [entry for entry in entries if not entry.completed]
        return EducationPrerequisites(
            business_type=business_type,
            satisfied=bool(required) and not outstanding,
            content_available=bool(required),
            required=entries,
            outstanding=outstanding,
            note=None if required else NO_PUBLISHED_CONTENT_NOTE,
        )

    async def _require_owner(self) -> None:
        await require_owner_workspace(self._businesses, self._identity)

    async def _relevant_modules(self, business_type: BusinessType | None) -> list[EducationModule]:
        modules = await self._repository.list_published()
        if business_type is None:
            return modules
        # Module counts are small and the mapping lives in a JSON column, so the
        # filter stays in Python rather than in dialect-specific SQL.
        return [
            module
            for module in modules
            if not module.business_types or business_type in module.business_types
        ]

    @staticmethod
    def _summary(
        module: EducationModule, progress: EducationProgress | None
    ) -> EducationModuleSummary:
        return EducationModuleSummary(
            id=module.id,
            slug=module.slug,
            title=module.title,
            summary=module.summary,
            topic=module.topic,
            content_version=module.content_version,
            estimated_minutes=module.estimated_minutes,
            business_types=cast(list[BusinessType], list(module.business_types)),
            is_required=module.is_required,
            reviewed_at=module.reviewed_at,
            progress=(
                None
                if progress is None
                else EducationProgressRead(
                    module_id=progress.module_id,
                    content_version=progress.content_version,
                    started_at=progress.started_at,
                    completed_at=progress.completed_at,
                    correct_answers=progress.correct_answers,
                    total_questions=progress.total_questions,
                    passed=progress.passed,
                )
            ),
        )
