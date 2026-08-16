from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .contracts import RunLimits
from .runner import dependency_probe, run_live


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SimuMarket AI OASIS feasibility spike")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("probe", help="Verify pinned OASIS dependencies")

    live = subparsers.add_parser("live", help="Run the four-role Gemini spike")
    live.add_argument("--runs", type=int, default=1)
    live.add_argument("--seed", type=int, default=42)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "probe":
        print(json.dumps(dependency_probe(), indent=2))
        return 0

    if args.runs < 1:
        raise SystemExit("--runs minimal bernilai 1.")

    model_id = os.getenv("OASIS_MODEL_ID", "gemini-3.1-flash-lite")
    output_root = Path(__file__).resolve().parent.parent / "artifacts"
    limits = RunLimits(
        max_total_tokens=int(os.getenv("OASIS_MAX_TOTAL_TOKENS", "12000")),
        max_output_tokens_per_stage=int(os.getenv("OASIS_MAX_OUTPUT_TOKENS", "1000")),
        stage_timeout_seconds=int(os.getenv("OASIS_STAGE_TIMEOUT_SECONDS", "45")),
    )
    manifests = [
        asyncio.run(
            run_live(
                output_root=output_root,
                model_id=model_id,
                seed=args.seed + run,
                limits=limits,
            )
        )
        for run in range(args.runs)
    ]
    print(json.dumps([manifest.model_dump(mode="json") for manifest in manifests], indent=2))
    return 0
