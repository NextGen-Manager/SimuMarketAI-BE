from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from .contracts import (
    AgentArtifact,
    AgentRole,
    FinanceInput,
    RunLimits,
    RunManifest,
    StageMetric,
    StructuredBallot,
)
from .finance import calculate_finance
from .prompts import build_prompt

ROLE_ORDER = (
    AgentRole.MARKET_ANALYST,
    AgentRole.CUSTOMER_PERSONA,
    AgentRole.FINANCE,
    AgentRole.REPORT,
)


def dependency_probe() -> dict[str, str]:
    import camel
    import oasis

    return {
        "status": "ok",
        "camel_ai": camel.__version__,
        "camel_oasis": "0.2.5",
        "oasis_module": str(Path(oasis.__file__).resolve()),
    }


def _extract_json(content: str) -> dict[str, Any]:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("Respons agent tidak memuat object JSON.")
    parsed = json.loads(content[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Respons agent bukan object JSON.")
    return parsed


def _usage(info: dict[str, Any]) -> tuple[int, int, int]:
    usage = info.get("usage", {})
    if not isinstance(usage, dict):
        return 0, 0, 0
    return (
        int(usage.get("prompt_tokens", 0)),
        int(usage.get("completion_tokens", 0)),
        int(usage.get("total_tokens", 0)),
    )


async def run_live(
    *, output_root: Path, model_id: str, seed: int, limits: RunLimits
) -> RunManifest:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY wajib diisi untuk menjalankan spike live.")

    import oasis
    from camel.messages import BaseMessage
    from camel.models import ModelFactory
    from camel.types import ModelPlatformType
    from oasis import ActionType, generate_reddit_agent_graph

    run_id = f"oasis-{uuid4()}"
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    trace_path = output_dir / "trace.db"
    random.seed(seed)

    model = ModelFactory.create(
        model_platform=ModelPlatformType.GEMINI,
        model_type=model_id,
        model_config_dict={
            "temperature": 0,
            "max_tokens": limits.max_output_tokens_per_stage,
        },
        api_key=api_key,
        max_retries=1,
        timeout=limits.stage_timeout_seconds,
    )
    profiles_path = Path(__file__).resolve().parent.parent / "profiles.json"
    graph = await generate_reddit_agent_graph(
        profile_path=str(profiles_path),
        model=model,
        available_actions=[ActionType.DO_NOTHING],
    )
    environment = oasis.make(
        agent_graph=graph,
        platform=oasis.DefaultPlatformType.REDDIT,
        database_path=str(trace_path),
    )

    finance = calculate_finance(
        FinanceInput(
            fixed_cost_idr=24_000_000,
            selling_price_idr=18_000,
            variable_cost_idr=9_000,
        )
    )
    artifacts: list[AgentArtifact] = []
    ballots: list[StructuredBallot] = []
    metrics: list[StageMetric] = []
    failures = 0
    consumed_tokens = 0

    try:
        await environment.reset()
        for index, role in enumerate(ROLE_ORDER):
            agent = graph.get_agent(index)
            prompt = build_prompt(role, finance)
            started = time.perf_counter_ns()
            try:
                response = await asyncio.wait_for(
                    agent.astep(BaseMessage.make_user_message(role_name="User", content=prompt)),
                    timeout=limits.stage_timeout_seconds,
                )
            except TimeoutError:
                elapsed_ms = (time.perf_counter_ns() - started) // 1_000_000
                metrics.append(
                    StageMetric(
                        stage=role,
                        wall_clock_ms=elapsed_ms,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        schema_validation_failures=1,
                    )
                )
                failures += 1
                break
            elapsed_ms = (time.perf_counter_ns() - started) // 1_000_000
            content = response.msgs[0].content if response.msgs else "{}"
            await agent.env.action.perform_action(
                {"prompt": prompt, "response": content},
                ActionType.INTERVIEW.value,
            )
            stage_failures = 0
            try:
                payload = _extract_json(content)
                if role is AgentRole.CUSTOMER_PERSONA:
                    ballots.append(StructuredBallot.model_validate(payload))
                else:
                    artifacts.append(AgentArtifact.model_validate(payload))
            except (ValidationError, ValueError, json.JSONDecodeError):
                stage_failures = 1
                failures += 1

            prompt_tokens, completion_tokens, total_tokens = _usage(response.info)
            consumed_tokens += total_tokens
            metrics.append(
                StageMetric(
                    stage=role,
                    wall_clock_ms=elapsed_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    schema_validation_failures=stage_failures,
                )
            )
            if consumed_tokens >= limits.max_total_tokens:
                failures += 1
                break
    finally:
        await environment.close()

    status = "completed" if failures == 0 else "partial"
    manifest = RunManifest(
        run_id=run_id,
        status=status,
        model_id=model_id,
        provider="gemini",
        oasis_version="0.2.5",
        camel_ai_version="0.2.78",
        prompt_version="oasis-spike-v1",
        cohort_version="four-role-cohort-v1",
        seed=seed,
        limits=limits,
        trace_path=str(trace_path),
        finance=finance,
        ballots=ballots,
        artifacts=artifacts,
        metrics=metrics,
    )
    (output_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest
