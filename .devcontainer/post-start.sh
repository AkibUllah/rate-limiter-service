#!/usr/bin/env bash
# Codespaces start hook: bring up the Redis-backed limiter (two replicas) and seed a demo policy.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up -d --build

echo "waiting for the API on :8000 ..."
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# PUT is an upsert, so re-running on every start is harmless.
curl -fsS -X PUT http://127.0.0.1:8000/v1/policies/otp -H 'content-type: application/json' \
  -d '{"name":"otp","limit":5,"window_seconds":300,"algorithm":"sliding_log"}' >/dev/null

cat <<'MSG'

Rate Limiter Service is up (2 replicas sharing limits through Redis).
  API docs:  forwarded port 8000, path /docs
  Try:       POST /v1/check {"key":"phone:+8801711000000","policy":"otp"}  (6th call within 5 min is denied)
MSG
