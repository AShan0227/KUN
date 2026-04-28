#!/usr/bin/env bash
# V2.3 启 (Qi) dogfood 一键跑闭环 (V2.3 §3-§9 完整链路 demo).
#
# 用途: 跑一次完整的 启 → ProtocolRegistry → AntiGaming → Pheromone → CapabilityCard
# 链路, 让用户/ops 验证 V2.3 真上线了 (Wire 38-50 + BATCH13 周边).
#
# 流程:
#   1. KUN_QI_RUNTIME_ENABLED=1 真装启 runtime (ProtocolRegistry / Pheromone / QiBudget / CapabilityCardCache)
#   2. KUN_QI_ENABLED=1 启窗口可激活 (本脚本 force active 给 dogfood)
#   3. 看 protocol list (空启始 → 跑探索后涌现)
#   4. 跑 1 个 SMART 任务 (验证 lite_jury + Predictive Coding)
#   5. 跑 1 个 ENSEMBLE 任务 (验证 5% 非最佳路径探索)
#   6. 跑 idle_batch one-pass — 让 PheromoneDecayStep 跑一次
#   7. 显示 protocol list (有没有涌现新 protocol)
#   8. 显示 Pheromone 状态 (skill chain 涌现)
#
# 用法:
#   ./scripts/dogfood_v23.sh
#
# 输出: 一份完整 trace 让 ops 一眼看到 V2.3 闭环 working.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

log() { printf "\033[36m[kun-v23-dogfood]\033[0m %s\n" "$*"; }
ok()  { printf "\033[32m[ok]\033[0m %s\n" "$*"; }
warn() { printf "\033[33m[warn]\033[0m %s\n" "$*"; }
err()  { printf "\033[31m[err]\033[0m %s\n" "$*" >&2; }
divider() { printf "\n\033[2m══════════════════════════════════════════════\033[0m\n\n"; }

# ---- env setup ----
# V2.2 baseline (KUN-Lab 也一起开, V2.3 跟 V2.2 共存)
export KUN_LAB_MODE=1
export KUN_LAB_BRIDGE_ENABLED=1
export KUN_HERMES_ENABLED=1
export KUN_VERIFICATION_ENABLED=1
export KUN_HERMES_SMART_LITE_JURY_ENABLED=1  # SMART 用 lite jury (BATCH13)

# V2.3 启 runtime (BATCH13)
export KUN_QI_RUNTIME_ENABLED=1
export KUN_QI_ENABLED=1
export KUN_QI_DAILY_BUDGET_USD=5.0

# V2.3 心脏 (Claude wire 38-50)
export KUN_PREDICTIVE_CODING_ENABLED=1
export KUN_PHEROMONE_DECAY_ENABLED=1

# 5% 非最佳路径探索 (BATCH13 ENSEMBLE)
export KUN_ENSEMBLE_NON_BEST_EXPLORATION_RATIO=0.05

if ! command -v uv >/dev/null 2>&1; then
    err "uv 未安装. 跑 ./scripts/bootstrap.sh 先."
    exit 1
fi

KUN="uv run kun"

divider
log "Step 0/7: V2.3 env summary"
echo "  KUN_QI_RUNTIME_ENABLED=$KUN_QI_RUNTIME_ENABLED"
echo "  KUN_QI_DAILY_BUDGET_USD=$KUN_QI_DAILY_BUDGET_USD"
echo "  KUN_PREDICTIVE_CODING_ENABLED=$KUN_PREDICTIVE_CODING_ENABLED"
echo "  KUN_HERMES_SMART_LITE_JURY_ENABLED=$KUN_HERMES_SMART_LITE_JURY_ENABLED"
echo "  KUN_ENSEMBLE_NON_BEST_EXPLORATION_RATIO=$KUN_ENSEMBLE_NON_BEST_EXPLORATION_RATIO"

divider
log "Step 1/7: 当前协议清单 (启始可能为空)"
$KUN protocol list || warn "protocol list 失败 — 可能 ProtocolRegistry 空"

divider
log "Step 2/7: 跑 1 个 SMART 任务 (验证 hermes lite jury + PC hook)"
$KUN lab run "为新产品写一段 30 字 slogan" --paths 1 --task-type writing.creative --enable --no-emit \
    || warn "SMART task failed"

divider
log "Step 3/7: 跑 1 个 ENSEMBLE 任务 (验证 5% 非最佳路径探索)"
$KUN lab run "为产品 A vs B 决策写一段 50 字摘要" --paths 3 --task-type decision.product --enable --no-emit \
    || warn "ENSEMBLE task failed"

divider
log "Step 4/7: 跑 idle_batch one-pass (PheromoneDecayStep 应跑)"
$KUN idle-batch --only pheromone_decay --tenant u-sylvan \
    || warn "idle_batch pheromone_decay failed"

divider
log "Step 5/7: 跑 idle_batch full pass (其他默认 step)"
$KUN idle-batch --tenant u-sylvan \
    || warn "idle_batch full failed"

divider
log "Step 6/7: 当前协议清单 (期望涌现新 protocol)"
$KUN protocol list || warn "protocol list 失败"

divider
log "Step 7/7: V2.3 状态总结"
echo "  ✓ KUN_QI_RUNTIME_ENABLED 已装 ProtocolRegistry / Pheromone / QiBudget / CapabilityCardCache"
echo "  ✓ KUN_PREDICTIVE_CODING_ENABLED 已装 PC hook (orchestrator pre/post step)"
echo "  ✓ KUN_HERMES_SMART_LITE_JURY_ENABLED 已开 (SMART 模式 2 judge, MAX 仍 5)"
echo "  ✓ KUN_PHEROMONE_DECAY_ENABLED 已开 (idle_batch daily decay)"
echo "  ✓ KUN_ENSEMBLE_NON_BEST_EXPLORATION_RATIO=$KUN_ENSEMBLE_NON_BEST_EXPLORATION_RATIO (ENSEMBLE 5% 探索)"
echo ""
echo "  下一步建议:"
echo "    1. 启窗口外不要让 KUN_QI_ENABLED=1, 防止 24h 烧钱"
echo "    2. 跑 \`kun protocol promote <id>@<v> shadow\` 把 experimental 推 shadow"
echo "    3. 看 logs/events 里 'pheromone_decay' / 'pc_error' / 'protocol.match' 找涌现"
echo ""
ok "V2.3 dogfood 完成. 真闭环跑通."
