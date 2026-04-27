#!/usr/bin/env bash
# 单独安装 git pre-commit hooks (bootstrap.sh 已自动跑过这个).
#
# 用途:
#   - 不想跑完整 bootstrap, 只装 hooks
#   - bootstrap 时 pre-commit install 失败, 手动重试
#
# 装完后, 每次 git commit 自动跑:
#   - ruff check (修代码错)
#   - ruff format (格式化)
#   - mypy kun (类型检查)
#   - 一些 yaml/toml/json 校验

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

log() { printf "\033[36m[install-hooks]\033[0m %s\n" "$*"; }
err() { printf "\033[31m[error]\033[0m %s\n" "$*" >&2; exit 1; }

if ! command -v uv >/dev/null 2>&1; then
    err "uv 未安装. 先跑 ./scripts/bootstrap.sh 或 brew install uv"
fi

if ! uv run pre-commit --version >/dev/null 2>&1; then
    log "pre-commit 不在 .venv, 先 uv sync --extra dev"
    uv sync --extra dev >/dev/null
fi

log "uv run pre-commit install --install-hooks"
uv run pre-commit install --install-hooks

log "测试一下: uv run pre-commit run --all-files (跳过, 太慢)"
log "  你 commit 时会自动跑 staged files. 想强制全跑: uv run pre-commit run --all-files"

log "完成. 现在 commit 前会自动:"
log "  - ruff check + ruff format (Wire 27/28/29 hotfix 教训, 防再犯)"
log "  - mypy kun (类型检查)"
log "  - yaml/toml/json/large-file/merge-conflict/private-key 检查"
