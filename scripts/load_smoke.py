"""Small repeatable readiness load probe for CI and release checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import asdict, dataclass

import httpx


@dataclass(frozen=True, slots=True)
class LoadResult:
    requests: int
    failures: int
    concurrency: int
    p95_ms: int
    maximum_ms: int


async def run_load(url: str, *, requests: int, concurrency: int) -> LoadResult:
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=10) as client:

        async def send() -> tuple[int, bool]:
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get(url)
                    payload = response.json()
                    healthy = (
                        response.status_code == 200
                        and isinstance(payload, dict)
                        and payload.get("status") == "ok"
                    )
                except (httpx.HTTPError, ValueError):
                    healthy = False
                elapsed_ms = math.ceil((time.perf_counter() - started) * 1000)
                return elapsed_ms, healthy

        samples = await asyncio.gather(*(send() for _ in range(requests)))

    durations = sorted(duration for duration, _ in samples)
    percentile_index = max(0, math.ceil(len(durations) * 0.95) - 1)
    return LoadResult(
        requests=requests,
        failures=sum(not healthy for _, healthy in samples),
        concurrency=concurrency,
        p95_ms=durations[percentile_index],
        maximum_ms=durations[-1],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--maximum-p95-ms", type=int, default=1_000)
    args = parser.parse_args()
    if args.requests < 1 or not 1 <= args.concurrency <= args.requests:
        parser.error(
            "requests dan concurrency harus positif, dan concurrency tidak boleh lebih besar"
        )

    result = asyncio.run(run_load(args.url, requests=args.requests, concurrency=args.concurrency))
    print(json.dumps(asdict(result), sort_keys=True))
    if result.failures or result.p95_ms > args.maximum_p95_ms:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
