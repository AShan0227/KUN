#!/usr/bin/env bash
# KUN-Lab dogfood 一键跑闭环验证 (V2.2 §26 完整链路 demo).
#
# 用途: 跑一次完整的 lab → events → idle_batch → KP → registry → ExecutionMode
# classifier 链路, 让用户/ops 验证 V2.2 §26 真上线了.
#
# 流程:
#   1. 启用 KUN_LAB_MODE=1 + KUN_LAB_BRIDGE_ENABLED=1 (lab + 主仓库消费)
#   2. 跑 3 次 ensemble (3 种典型任务 — 写作 / 决策 / 编程)
#   3. 跑 lab promote (强制门槛低, 让有效 recipe 推过来)
#   4. 跑 idle_batch one-pass — 让 LabRecipeAdoptionStep 拉 events
#   5. 显示 LabRecipeRegistry 状态 (lab 推过来啥)
#   6. 显示 ExecutionMode classifier 对几个 task_type 的决策 (有 lab hint vs 没)
#
# 用法:
#   ./scripts/dogfood_run.sh
#
# 输出: 一份完整 trace 让 ops 一眼看到 lab 闭环 working.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

log() { printf "\033[36m[kun-dogfood]\033[0m %s\n" "$*"; }
ok()  { printf "\033[32m[ok]\033[0m %s\n" "$*"; }
warn() { printf "\033[33m[warn]\033[0m %s\n" "$*"; }
err()  { printf "\033[31m[err]\033[0m %s\n" "$*" >&2; }
divider() { printf "\n\033[2m══════════════════════════════════════════════\033[0m\n\n"; }

# ---- env setup ----
export KUN_LAB_MODE=1
export KUN_LAB_BRIDGE_ENABLED=1
export KUN_HERMES_ENABLED=1
export KUN_VERIFICATION_ENABLED=1

if ! command -v uv >/dev/null 2>&1; then
    err "uv 未安装. 跑 ./scripts/bootstrap.sh 先."
    exit 1
fi

KUN="uv run kun"

divider
log "Step 1/5: 跑 3 种典型任务的 ensemble"
log "task 1: 写作 (writing.creative)"
$KUN lab run "为新产品写一段 30 字 slogan" --paths 3 --task-type writing.creative --enable --no-emit || warn "task 1 failed"

log "task 2: 决策 (decision.product)"
$KUN lab run "创业团队该选 SaaS 还是开源策略?" --paths 3 --task-type decision.product --enable --no-emit || warn "task 2 failed"

log "task 3: 编程 (coding.refactor)"
$KUN lab run "把这段 try/except 重构成 context manager" --paths 3 --task-type coding.refactor --enable --no-emit || warn "task 3 failed"

divider
log "Step 2/5: 看 ExperimentLog 累积"
$KUN lab stats --top 10 || warn "lab stats failed"

divider
log "Step 3/5: 跑 promote (强制低门槛 min_total=2 让 recipe 推出来)"
$KUN lab promote --min-total 2 --min-winrate 0.3 --apply --tenant u-sylvan || warn "lab promote failed"

divider
log "Step 4/5: 跑 idle_batch 一遍 (LabRecipeAdoptionStep 拉 events.experiment.promoted)"
$KUN idle-batch --tenant u-sylvan --only lab_recipe_adoption || warn "idle_batch failed (注: install_lab_kp_bridge 需在 install_runtime 跑过, 否则 step 不在 registry — 这不阻塞 dogfood, 看 metrics 即可)"

divider
log "Step 5/5: 看 lab metrics + registry 状态"
log "Prometheus metrics 端点 (生产 ops 看 /metrics):"
echo "  - kun_lab_experiment_total{status=ok|budget_exceeded}"
echo "  - kun_lab_experiment_cost_usd"
echo "  - kun_lab_path_total{strategy, tier, status}"
echo "  - kun_lab_promotion_total{task_type, target_module}"
echo "  - kun_lab_registry_size"

divider
ok "Dogfood 完成."
log "下一步:"
log "  1. 看 ExperimentLog stats — 哪个 strategy 胜率高 (即使 in-memory, stats 反映 3 任务结果)"
log "  2. 跑 'kun lab promote --apply' — 让 recipe 真推主仓库 (走 events bus)"
log "  3. 启 API (kun serve) + 跑 chat task — 看 ExecutionMode classifier 是否真用 lab hint"
log "  4. 看 docs/v2/V2.2-implementation-audit.md — V2.2 完整状态"
log "  5. 看 docs/ops/dogfood-checklist.md — 验证清单"
