# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CodeQL code scanning workflow and Dependabot configuration (pip, Docker, GitHub Actions).
- API reference and terminal screenshots in the README.
- Roadmap tracked as GitHub issues under the `v0.2.0` milestone.

### Changed

- CI workflow runs with a read-only `GITHUB_TOKEN` by default.

## [0.1.0] - 2026-10-25

First public release.

### Added

- `ratelimiter` package with five algorithms behind one `Policy` type: token bucket, fixed window, sliding log, sliding counter and GCRA.
- `MemoryBackend` (lock-protected) and `RedisBackend` (one atomic Lua script per algorithm, TTL on every key).
- Policy strings such as `100/minute`, `10/5s` and `1000/hour;algorithm=gcra;burst=50`.
- Weighted requests, per-policy key scoping and explicit reset.
- `Decision` objects with `allowed`, `remaining`, `retry_after`, `reset_after` and ready-made HTTP headers.
- Pure ASGI `RateLimitMiddleware` with `X-RateLimit-*` headers, `429` + `Retry-After`, IP or header keys, per-request policy selection and exempt paths.
- FastAPI service: `POST /v1/check` with named or inline policies, policy CRUD (memory or shared Redis hash), `POST /v1/reset`, `/health` and optional API key.
- Injectable `ManualClock` for deterministic tests and benchmarks; only accepted requests consume quota.
- Benchmark script, Docker image, docker compose with two Redis-backed replicas, CI on Python 3.10/3.11, Pages API reference, Codespaces and Render kits.

[Unreleased]: https://github.com/AkibUllah/rate-limiter-service/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AkibUllah/rate-limiter-service/releases/tag/v0.1.0
