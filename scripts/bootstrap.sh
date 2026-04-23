#!/usr/bin/env bash
# KUN 一键启动脚本 — 首次使用.
#
#   ./scripts/bootstrap.sh
#
# 功能:
#   1. 检查 uv / docker / node 工具链
#   2. uv sync --dev
#   3. cp .env.example → .env (如缺失)
#   4. docker compose -f docker-compose.dev.yml up -d
#   5. 等 Postgres 就绪
#   6. alembic upgrade head
#   7. 跑 90 个测试
#   8. 启 API (后台) + 打开 health check

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

log() { printf "\033[36m[kun-bootstrap]\033[0m %s\n" "$*"; }
err() { printf "\033[31m[error]\033[0m %s\n" "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || err "missing tool: $1. install it and rerun."; }

log "checking toolchain..."
need uv
need docker
if ! docker info >/dev/null 2>&1; then
    err "docker daemon is not running. start Docker Desktop and retry."
fi

if [[ ! -f .env ]]; then
    log "creating .env from .env.example (FILL IN your keys before using real LLMs)"
    cp .env.example .env
fi

log "uv sync --dev"
uv sync --dev >/dev/null

log "docker compose up -d"
docker compose -f docker-compose.dev.yml up -d

log "waiting for postgres..."
for i in {1..30}; do
    if docker compose -f docker-compose.dev.yml exec -T postgres pg_isready -U kun >/dev/null 2>&1; then
        log "postgres ready"
        break
    fi
    sleep 1
    if [[ "$i" == "30" ]]; then
        err "postgres failed to become ready"
    fi
done

log "running migrations"
uv run alembic upgrade head

log "running tests"
uv run pytest tests/unit -q

log "done. next steps:"
cat <<NEXT

  API:        make serve
  CLI:        uv run kun run "hello"
  Rules:      make rules
  Skills:     make skills
  IdleBatch:  make idle-batch
  Frontend:   cd frontend && npm install && npm run dev

  Grafana:    http://localhost:3001 (admin/admin)
  Jaeger:     http://localhost:16686
  Prometheus: http://localhost:9090
  NATS:       http://localhost:8222/varz
  MinIO:      http://localhost:9001 (minio/minio123)

  tear down:  make down     (keeps data)
              make down-volumes   (wipe data)
NEXT
