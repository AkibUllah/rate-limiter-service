#!/usr/bin/env python3
"""Throughput / latency benchmark for every algorithm on the in-memory backend (optionally Redis).

    python bench/bench.py                       # in-memory, 200k checks per algorithm
    python bench/bench.py --iterations 50000 --keys 1000
    python bench/bench.py --redis redis://localhost:6379/0

The clock is a ManualClock advanced by a fixed step per call, so every algorithm sees the same
timeline and the numbers measure the limiter itself, not time.time().
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ratelimiter import Algorithm, ManualClock, MemoryBackend, Policy, RateLimiter  # noqa: E402


def run_case(
    limiter: RateLimiter,
    clock: ManualClock,
    policy: Policy,
    iterations: int,
    keys: int,
    step: float,
):
    key_names = [f"client-{i}" for i in range(keys)]
    latencies: list[float] = []
    allowed = 0
    sample_every = max(iterations // 2000, 1)  # ~2000 latency samples per case
    started = time.perf_counter()
    for i in range(iterations):
        key = key_names[i % keys]
        if i % sample_every == 0:
            t0 = time.perf_counter()
            decision = limiter.check(key, policy)
            latencies.append((time.perf_counter() - t0) * 1e6)
        else:
            decision = limiter.check(key, policy)
        allowed += decision.allowed
        clock.advance(step)
    elapsed = time.perf_counter() - started
    latencies.sort()
    return {
        "algorithm": policy.algorithm.value,
        "ops_per_sec": iterations / elapsed,
        "p50_us": statistics.median(latencies),
        "p99_us": latencies[int(len(latencies) * 0.99) - 1],
        "allowed_pct": 100.0 * allowed / iterations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--iterations", type=int, default=200_000)
    parser.add_argument("--keys", type=int, default=1000, help="distinct keys cycled round-robin")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--window", type=float, default=60.0)
    parser.add_argument(
        "--step", type=float, default=0.0005, help="simulated seconds between checks"
    )
    parser.add_argument(
        "--redis", default=None, help="Redis URL to benchmark the Lua backend as well"
    )
    args = parser.parse_args(argv)

    backends = [("memory", MemoryBackend())]
    if args.redis:
        from ratelimiter.backends.redis import redis_backend_from_url

        backends.append(("redis", redis_backend_from_url(args.redis)))

    print(f"python {platform.python_version()} · {platform.machine()} · {platform.system()}")
    print(
        f"iterations={args.iterations:,} keys={args.keys} "
        f"policy={args.limit}/{args.window:g}s step={args.step}s\n"
    )
    print("| backend | algorithm | ops/sec | p50 latency | p99 latency | allowed |")
    print("| --- | --- | ---: | ---: | ---: | ---: |")
    for backend_name, backend in backends:
        for algorithm in Algorithm:
            clock = ManualClock()
            limiter = RateLimiter(backend, clock=clock, key_prefix=f"bench:{backend_name}:")
            policy = Policy(
                limit=args.limit,
                window=args.window,
                algorithm=algorithm,
                name=f"bench-{algorithm.value}",
            )
            row = run_case(limiter, clock, policy, args.iterations, args.keys, args.step)
            print(
                f"| {backend_name} | {row['algorithm']} | {row['ops_per_sec']:,.0f} | "
                f"{row['p50_us']:.1f} µs | {row['p99_us']:.1f} µs | {row['allowed_pct']:.1f}% |"
            )
            if hasattr(backend, "clear"):
                backend.clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
