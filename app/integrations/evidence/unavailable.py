"""Runtime provider used while no licensed market source is configured.

Docs/16 records that the production competitor source is still an open product
decision. Until it is made, the honest behaviour is to report every requested
metric as missing rather than to substitute an average, a national figure, or
anything an LLM could be asked to guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from app.domain.evidence import (
    EvidenceRequest,
    EvidenceSnapshot,
    MissingEvidence,
)

SNAPSHOT_VERSION = "evidence-snapshot-unavailable-v1"
REASON_CODE = "source_not_configured"
REASON = "Sumber data pasar belum tersedia untuk metrik ini."


class UnavailableEvidenceProvider:
    provider_id = "unavailable"
    snapshot_version = SNAPSHOT_VERSION
    is_fixture = False

    async def collect(self, requests: Sequence[EvidenceRequest]) -> EvidenceSnapshot:
        return EvidenceSnapshot(
            snapshot_version=self.snapshot_version,
            provider_id=self.provider_id,
            retrieved_at=datetime.now(UTC),
            items=[],
            missing=[
                MissingEvidence(
                    metric=request.metric,
                    reason_code=REASON_CODE,
                    reason=REASON,
                )
                for request in requests
            ],
        )
