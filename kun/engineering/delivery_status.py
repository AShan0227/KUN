"""Honest product delivery status for KUN V3.

This is intentionally not a marketing checklist.  It is the machine-readable
boundary between "already wired into the real flow" and "still a safe stub /
partial slice".  NUO surfaces this so users and operators do not mistake
audited placeholders for production capability.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DeliveryStatus = Literal["ready", "partial", "audit_only", "not_ready"]


class DeliveryCapability(BaseModel):
    """One honest capability row shown in NUO."""

    capability_id: str
    label: str
    status: DeliveryStatus
    user_visible: bool = True
    summary: str
    done: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)

    @property
    def can_claim_complete(self) -> bool:
        return self.status == "ready" and not self.missing


def get_v3_delivery_status() -> list[DeliveryCapability]:
    """Current V3 capability status.

    Keep this list brutally honest.  If a feature only emits events or writes a
    row but no real consumer acts on it, it must be `partial` or `audit_only`.
    """
    return [
        DeliveryCapability(
            capability_id="llm_provider",
            label="LLM 主链路",
            status="ready",
            summary="普通执行、规划、判官和代码任务可统一走 Codex MCP。",
            done=[
                "KUN_LLM_PRIMARY=codex 时 top/strong/cheap/coding 都走 Codex MCP",
                "本机已切到 gpt-5.5",
                "保留 MiniMax fallback",
            ],
            missing=[
                "还没有按任务类型自动学习 gpt-5.5 / fallback 的最佳切换策略",
            ],
            next_steps=[
                "把模型结果继续写回 capability_card",
                "让守望按真实成功率动态调整路由",
            ],
        ),
        DeliveryCapability(
            capability_id="world_gateway",
            label="外部世界动作",
            status="audit_only",
            summary="现在只做审批、渲染和审计；不会假装已真实外发。",
            done=[
                "高风险动作进入 pending approval",
                "审批后经 WorldGateway 生成 audit packet",
                "payload 明确 external_dispatched=false",
            ],
            missing=[
                "邮件 / 浏览器 / 企业 API / 文件 / 支付等真实 handler",
                "handler 级权限、重试、补偿、回滚策略",
                "外部系统密钥和审计隔离",
            ],
            next_steps=[
                "先接最小安全 handler: local_file.write / webhook.post dry-run",
                "再接 email/browser/API 等真实外部动作",
            ],
        ),
        DeliveryCapability(
            capability_id="long_horizon_tasks",
            label="长周期任务能力",
            status="partial",
            summary="已有任务状态、事件、恢复信号的基础，但还不能独立长期运营一个产品。",
            done=[
                "RuntimeState / StateLedger / task events 已接入",
                "pending approval 通过后可解除任务暂停",
                "idle-batch 有部分复盘和学习能力",
            ],
            missing=[
                "长期任务恢复和失败续跑策略",
                "定时调度与阶段性目标复盘",
                "跨天/跨周任务的预算、风险、里程碑管理",
            ],
            next_steps=[
                "做 durable task resume worker",
                "给 TASK.md 工程对象补 milestone / checkpoint / owner",
            ],
        ),
        DeliveryCapability(
            capability_id="main_frontend",
            label="前端主体验",
            status="partial",
            summary="已有对话框 + 任务看板雏形，但还不是完整任务控制台。",
            done=[
                "首页主入口是对话 + 简单任务状态",
                "黑板 state ledger 可被前端读取",
                "NUO 有独立入口",
            ],
            missing=[
                "任务详情页",
                "预算 / 风险 / 下一步动作的统一主视图",
                "用户轻量编辑和确认入口",
            ],
            next_steps=[
                "把 StateLedger 变成任务详情主数据源",
                "主界面只露状态、预算、风险、待确认四件事",
            ],
        ),
        DeliveryCapability(
            capability_id="nuo_manager",
            label="NUO 管家",
            status="partial",
            summary="健康、成本、审批、诊断、能力画像已有，但用户层还需要做减法。",
            done=[
                "健康面板",
                "成本面板",
                "待审批动作",
                "诊断和能力画像",
            ],
            missing=[
                "用户侧只显示健康 / 成本 / 权限 / 风险的极简入口",
                "高级诊断默认折叠",
                "真实外部动作风险解释",
            ],
            next_steps=[
                "把高级诊断收进二级展开",
                "补能力边界面板，明确哪些功能还没真实接通",
            ],
        ),
        DeliveryCapability(
            capability_id="memory_strategy_reuse",
            label="记忆和策略复用",
            status="partial",
            summary="已有结果、过程、元决策写回的第一版，复用闭环还要继续打通。",
            done=[
                "任务结果记忆写入 Context AssetStore",
                "执行过程记忆写入 Context AssetStore",
                "模型/路径选择的元决策记忆已写入",
                "scorecard 进入事件和 capability writeback 来源",
            ],
            missing=[
                "定期蒸馏",
                "遗忘/衰减",
                "策略复用对下次路由的强影响",
            ],
            next_steps=[
                "让 idle-batch 汇总元决策为 methodology",
                "把相似任务检索结果接入 Watchtower Decision Plane",
            ],
        ),
        DeliveryCapability(
            capability_id="production_deployment",
            label="生产级部署",
            status="not_ready",
            summary="本机可测试，生产交付还缺账号、密钥、CI、监控和备份闭环。",
            done=[
                "本地 Docker 依赖可启动",
                "Alembic 单 head",
                "RLS 应用账号路径已设计",
                "Grafana / Prometheus / OTEL 本地栈存在",
            ],
            missing=[
                "正式用户账号体系",
                "租户 onboarding",
                "密钥管理",
                "线上 CI/release/tag",
                "备份恢复演练",
                "真实 dogfood 验收场景",
            ],
            next_steps=[
                "定义 dogfood 场景",
                "补 release checklist 自动检查",
                "做备份/恢复 smoke test",
            ],
        ),
    ]


def delivery_status_summary() -> dict[str, int]:
    """Compact counts for health summary."""
    counts = {"ready": 0, "partial": 0, "audit_only": 0, "not_ready": 0}
    for item in get_v3_delivery_status():
        counts[item.status] += 1
    return counts


__all__ = [
    "DeliveryCapability",
    "DeliveryStatus",
    "delivery_status_summary",
    "get_v3_delivery_status",
]
