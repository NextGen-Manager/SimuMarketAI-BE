"""Deterministic council payloads used by the fake OASIS runtime."""

from __future__ import annotations

import re
from typing import Any

from app.domain.agents import SimulationRequest
from app.integrations.oasis.prompts import CouncilMember

OBJECTION_LABELS: dict[str, str] = {
    "price_above_comfort": "Harga di atas batas nyaman",
    "portion_unclear": "Ukuran porsi belum jelas",
    "queue_time": "Khawatir waktu tunggu",
    "menu_variety": "Pilihan menu terbatas",
}
OBJECTION_CODES = tuple(OBJECTION_LABELS)
CHOICES = ("purchase", "consider", "reject")
QUOTE_BY_CHOICE = {
    "purchase": "cukup masuk akal untuk saya coba.",
    "consider": "masih perlu saya bandingkan dengan pilihan lain.",
    "reject": "belum sesuai kebutuhan saya saat ini.",
}
TOOL_CALL_ID = re.compile(r'"tool_call_id":\s*"([^"]+)"')


def market_payload(request: SimulationRequest) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    for index, item in enumerate(request.evidence):
        observations.append(
            {
                "id": f"MA-{index + 1:03d}",
                "stance": "risk" if item.metric == "competitor_count" else "opportunity",
                "claim": (
                    f"Nilai {item.metric} pada radius analisis berasal dari {item.source} "
                    "dan perlu diperiksa ulang di lapangan."
                ),
                "evidence_metrics": [item.metric],
                "confidence": "medium" if item.confidence_percent >= 50 else "low",
            }
        )
    for index, metric in enumerate(request.missing_evidence_metrics):
        observations.append(
            {
                "id": f"MA-GAP-{index + 1:03d}",
                "stance": "uncertainty",
                "claim": f"Metrik {metric} belum tersedia sehingga kesimpulan pasar terbatas.",
                "evidence_metrics": [],
                "confidence": "low",
            }
        )
    if not observations:
        observations.append(
            {
                "id": "MA-GAP-001",
                "stance": "uncertainty",
                "claim": "Tidak ada bukti pasar yang dapat dinilai pada run ini.",
                "evidence_metrics": [],
                "confidence": "low",
            }
        )
    return {
        "headline": "Penilaian pasar dibatasi oleh cakupan bukti yang tersedia.",
        "observations": observations[:12],
        "evidence_gaps": list(request.missing_evidence_metrics),
        "disagreements": [
            "Opportunity Scout dan Competition Skeptic berbeda pandangan tentang "
            "kesiapan lokasi karena bukti belum lengkap."
        ],
    }


def persona_ballot(
    request: SimulationRequest,
    member: CouncilMember,
    index: int,
    *,
    baseline: bool,
) -> dict[str, Any]:
    price = request.concept.price_idr
    rotation = (request.seed + index + (0 if baseline else 1)) % len(CHOICES)
    choice = CHOICES[rotation]
    objection = OBJECTION_CODES[(request.seed + index) % len(OBJECTION_CODES)]
    spread = (index % 3 + 1) * 1_000
    return {
        "agent_id": member.agent_id,
        "archetype": member.archetype or "budget_driven",
        "choice": choice,
        "objection_code": objection,
        "objection_label": OBJECTION_LABELS[objection],
        "acceptable_price_min_idr": max(0, price - spread),
        "acceptable_price_max_idr": price + spread,
        "quote": "Respons sintetis: tawaran ini " + QUOTE_BY_CHOICE[choice],
    }


def finance_payload(request: SimulationRequest, prompt: str) -> dict[str, Any]:
    ids = TOOL_CALL_ID.findall(prompt)
    return {
        "finance_rule_version": request.finance_rule_version,
        "critiques": [
            {
                "id": "FIN-001",
                "assumption": "Volume harian dasar dapat dicapai sejak bulan pertama.",
                "concern": (
                    "Volume awal usaha baru umumnya di bawah rencana sehingga skenario "
                    "dasar perlu diperlakukan sebagai target, bukan perkiraan."
                ),
                "severity": "high",
                "tool_call_ids": ids[:1],
            },
            {
                "id": "FIN-002",
                "assumption": "Biaya variabel per unit tetap sepanjang bulan.",
                "concern": (
                    "Harga bahan segar berfluktuasi sehingga marjin kontribusi dapat "
                    "berubah tanpa perubahan harga jual."
                ),
                "severity": "medium",
                "tool_call_ids": ids,
            },
        ],
        "fragile_assumptions": [
            "Susut bahan tidak dimasukkan ke perhitungan.",
            "Biaya platform pesan-antar belum diperhitungkan.",
        ],
    }


def report_payload(prompt: str, *, extra_number: int | None) -> dict[str, Any]:
    body = (
        "Penilaian ini menggabungkan bukti pasar yang tersedia, hasil kalkulator "
        "deterministik, dan respons sintetis panel persona. Seluruh angka pada "
        "laporan berasal dari engine, bukan dari narasi ini."
    )
    if extra_number is not None:
        body += f" Perkiraan tambahan tanpa sumber: {extra_number}."
    received = [
        artifact_type
        for artifact_type in ("MarketAssessment", "CustomerSimulationResult", "FinanceReview")
        if f'"{artifact_type}"' in prompt or f"{artifact_type}:" in prompt
    ]
    sections = [
        {
            "id": "NAR-001",
            "title": "Ringkasan penilaian",
            "body": body,
            "source_artifact_types": received or ["MarketAssessment"],
        }
    ]
    if "CustomerSimulationResult" in received:
        sections.append(
            {
                "id": "NAR-002",
                "title": "Batas penggunaan",
                "body": (
                    "Respons persona adalah sinyal sintetis eksploratif dan tidak boleh "
                    "dibaca sebagai perilaku pelanggan nyata. Bukti yang belum tersedia "
                    "menurunkan keyakinan, bukan skor."
                ),
                "source_artifact_types": ["CustomerSimulationResult"],
            }
        )
    return {
        "sections": sections,
        "red_team_findings": [
            "Draft awal menyebut peluang tanpa menunjuk metrik; klaim tersebut dihapus."
        ],
        "removed_unsupported_claims": [
            "Klaim tentang pertumbuhan permintaan tahunan dihapus karena tidak ada bukti."
        ],
    }
