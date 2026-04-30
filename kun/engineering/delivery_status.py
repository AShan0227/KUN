"""Honest product delivery status for KUN V3.

This is intentionally not a marketing checklist.  It is the machine-readable
boundary between "already wired into the real flow" and "still a safe stub /
partial slice".  NUO surfaces this so users and operators do not mistake
audited placeholders for production capability.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from kun.world.gateway import WorldGateway, WorldHandlerDescriptor, get_world_gateway

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
    evidence_refs: list[str] = Field(default_factory=list)

    @property
    def can_claim_complete(self) -> bool:
        return self.status == "ready" and not self.missing


def get_v3_delivery_status(
    *,
    world_gateway: WorldGateway | None = None,
) -> list[DeliveryCapability]:
    """Current V3 capability status.

    Keep this list brutally honest.  If a feature only emits events or writes a
    row but no real consumer acts on it, it must be `partial` or `audit_only`.
    """
    world_gateway_item = _world_gateway_delivery_status(world_gateway=world_gateway)
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
            evidence_refs=[
                "kun/interface/llm/router.py",
                "kun/interface/llm/codex_mcp_provider.py",
                "tests/unit/test_llm_router.py",
            ],
            next_steps=[
                "按任务类型自动学习 gpt-5.5 / fallback 的最佳切换策略",
                "把模型结果继续写回 capability_card",
                "让守望按真实成功率动态调整路由",
            ],
        ),
        world_gateway_item,
        DeliveryCapability(
            capability_id="long_horizon_tasks",
            label="长周期任务能力",
            status="partial",
            summary="已有任务状态、事件、恢复信号的基础，但还不能独立长期运营一个产品。",
            done=[
                "RuntimeState / StateLedger / task events 已接入",
                "Mission / MissionTask / MissionMilestone 数据模型已落库",
                "Mission HTTP API 可创建 Mission、挂 Task、记录里程碑",
                "resume request 扫描能找出 queued mission task 并发事件",
                "MissionResumeWorker 已安装并接入共享 Orchestrator continuation runner",
                "续跑结果会写回 Mission checkpoint，并记录 source_task_id / executed_task_id",
                "首页长期目标卡可手动点“推进一次”",
                "pending approval 通过后可解除任务暂停",
                "StateLedger history 可从 EventRow 回放长期事件",
                "idle-batch 有部分复盘和学习能力",
                "Mission 级预算已滚动汇总并可超预算暂停",
                "Mission reaper 可处理 queued/running 卡死任务",
                "Mission 可记录下一步和复盘摘要",
                "Mission 续跑 prompt 会带上最近复盘、预算提醒、风险提醒和下一步动作",
                "Mission 复盘中的预算/风险/不确定性会影响下一次 Watchtower 策略权重和执行档位",
            ],
            evidence_refs=[
                "kun/engineering/mission_worker.py",
                "kun/engineering/mission_control.py",
                "kun/api/blackboard.py",
                "kun/api/blackboard_data_sources.py",
                "tests/unit/test_mission_worker.py",
                "tests/unit/test_mission_control.py",
                "tests/unit/test_mission_reaper.py",
                "tests/unit/test_wave7.py",
            ],
            missing=[
                "续跑还不是原 TaskRow 原地恢复，而是 continuation task 挂回 Mission",
                "StateLedger 还不是完整事件溯源重建器，暂时只做历史事件回放",
                "Mission 复盘只做轻量权重/档位 nudging，还没训练长期策略模型",
            ],
            next_steps=[
                "让 Mission review 结果进一步反向影响 Watchtower Decision Plane 权重",
                "补跨周运营策略模板和真实 dogfood 运营任务",
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
            evidence_refs=[
                "frontend/src/app/page.tsx",
                "kun/api/blackboard.py",
            ],
            missing=[
                "更完整的节点图 / 高级调试视图",
                "用户轻量编辑和确认入口",
            ],
            next_steps=[
                "把任务详情里的预算 / 风险 / 下一步动作再打磨成可编辑入口",
                "节点图只放高级模式，不抢主入口",
            ],
        ),
        DeliveryCapability(
            capability_id="nuo_manager",
            label="NUO 管家",
            status="partial",
            summary="健康、成本、权限、风险是默认入口；高级诊断和能力画像已折叠。",
            done=[
                "健康面板",
                "成本面板",
                "待审批动作",
                "诊断和能力画像",
                "WorldGateway handler 健康体检",
                "未启用的真实外部 handler 会暴露 env/密钥配置缺口",
                "Context / memory 瘦身维护 dry-run 和真实执行入口",
                "默认只露健康 / 成本 / 权限 / 风险，高级诊断折叠",
                "idle-batch 会定期生成 NUO 深度体检摘要并写入事件账本",
                "NUO 深度体检发现会进入 StateLedger 当前状态视图，任务看板能看到系统风险",
            ],
            evidence_refs=[
                "kun/api/nuo/health_panel.py",
                "kun/api/nuo/action_panel.py",
                "kun/engineering/nuo_system_health.py",
                "tests/unit/test_delivery_status.py",
            ],
            missing=[
                "定期 NUO 体检已进入当前状态账本，但还没做浏览器/移动端主动推送",
                "把 handler 健康结果接入自动限权 / 降级",
            ],
            next_steps=[
                "把高风险 NUO finding 推送到用户看板 / StateLedger 当前快照",
                "失败率高的外部 handler 自动降级为人工确认",
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
                "CreditAssignment 已接入 Orchestrator，能给 memory/skill/model/role_template 记贡献",
                "资源贡献已持久化到 resource_credit_stats，重启后不会全部丢失",
                "ContextPacker 已按持久贡献度、验证结果和质量分排序",
                "Watchtower Decision Plane 会用 strategy_pack 历史信用做 MoE 式轻量加权",
                "TaskRouter 角色/模型用途选择已进入统一 DecisionTicket 和 StateLedger",
                "PreDeliverGate 交付审核结果已进入统一 DecisionTicket 和 StateLedger",
                "NUO context maintenance 可压缩过长摘要、软遗忘/硬删除长期未用资产",
            ],
            evidence_refs=[
                "kun/memory/writeback.py",
                "kun/context/packer.py",
                "kun/engineering/orchestrator.py",
                "kun/engineering/credit_assignment.py",
                "kun/watchtower/decision_plane.py",
                "tests/unit/test_v3_memory_scoring_gateway.py",
            ],
            missing=[
                "定期蒸馏",
                "贡献信用对模型路由 / skill 路由的更强影响还需要真实样本和阈值校准",
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
                "生产模式不再信任裸 X-Tenant-Id；支持 HMAC Bearer token 解析租户/权限",
                "NUO 外部动作审批支持 world:approve / world:dispatch scope 守门",
                "已补 Postgres backup 和 restore smoke 脚本",
                "生产 ready 自检会标出 auth / RLS / 默认密钥问题",
                "Mission reaper、context maintenance、resource credit 已补 Prometheus 指标",
            ],
            evidence_refs=[
                "docker-compose.dev.yml",
                "alembic/versions",
                "docs/DEPLOY.md",
            ],
            missing=[
                "完整正式用户账号体系",
                "租户 onboarding",
                "集中密钥管理和轮换",
                "线上 CI/release/tag",
                "真实环境备份恢复演练",
                "真实 dogfood 验收场景",
            ],
            next_steps=[
                "定义 dogfood 场景",
                "补 release checklist 自动检查",
                "做备份/恢复 smoke test",
            ],
        ),
    ]


def _world_gateway_delivery_status(
    *,
    world_gateway: WorldGateway | None,
) -> DeliveryCapability:
    try:
        gateway = world_gateway or get_world_gateway()
        descriptors = gateway.handler_descriptors()
    except Exception as exc:
        return DeliveryCapability(
            capability_id="world_gateway",
            label="外部世界动作",
            status="partial",
            summary="WorldGateway 已存在，但当前无法读取 handler 注册表。",
            done=[
                "高风险动作进入 pending approval",
                "审批链不会把未知动作伪装成已执行",
            ],
            evidence_refs=[
                "kun/world/gateway.py",
                "kun/engineering/action_executor.py",
                "tests/unit/test_action_executor.py",
            ],
            missing=[
                f"handler 注册表读取失败: {type(exc).__name__}",
                "真实邮件发送",
                "真实浏览器操作",
                "企业 API handler",
            ],
            next_steps=["修复 WorldGateway 注册表读取，再按 handler 自动生成能力边界"],
        )

    by_type = {item.action_type: item for item in descriptors}
    done = [
        "高风险动作进入 pending approval",
        "审批后经 WorldGateway 生成 audit packet",
        "NUO 可查看当前 WorldGateway handler 支持状态",
        "待审批动作会带 gateway_preview；local_file.write 可在批准前看到 diff",
        "审批接口会返回 gateway 执行结果和产物路径",
        "NUO 可查看最近外部动作执行记录和产物摘要",
        "handler 执行失败会明确返回失败并保持任务暂停",
        "不支持的 action_type 会明确 requires_handler=true",
        "WorldGateway 会返回 user_summary / next_step / permissions_required，避免用户误解是否真实外发",
        "WorldGateway handler 注册表会暴露重试策略、补偿策略和风险范围",
        "真实外部动作缺少 external_dispatch_confirmed=true 时会被策略层拦截",
        "NUO 判断 handler blocked/unregistered 时，审批后执行器会拦截并保持任务暂停",
    ]
    done.extend(_handler_done_line(item) for item in descriptors)

    missing = []
    if "email.send" not in by_type:
        missing.append("真实邮件发送（email.send 未注册；需显式开启 SMTP env）")
    if "browser.execute" not in by_type:
        missing.append("真实浏览器操作（browser.execute 未注册；需显式开启 Playwright env）")
    if "enterprise_api.post" not in by_type:
        missing.append("企业 API handler（enterprise_api.post 未注册；需 HTTPS host 白名单）")
    missing.extend(
        [
            "外部系统密钥轮换和租户级密钥隔离",
            "支付 / 发布等更高风险动作",
        ]
    )

    real_handlers = {"email.send", "browser.execute", "enterprise_api.post"} & set(by_type)
    summary = (
        f"已从 WorldGateway 注册表自动识别 {len(descriptors)} 个 handler；"
        f"真实外部 handler 已启用 {len(real_handlers)} 个。"
    )

    return DeliveryCapability(
        capability_id="world_gateway",
        label="外部世界动作",
        status="partial",
        summary=summary,
        done=done,
        missing=missing,
        evidence_refs=[
            "kun/world/gateway.py",
            "kun/world/handler_health.py",
            "tests/unit/test_action_executor.py",
            "tests/unit/test_delivery_status.py",
            "tests/unit/test_world_handler_health.py",
        ],
        next_steps=[
            "按租户配置真实 email / browser / enterprise API handler",
            "给每个真实 handler 补租户级密钥、重试、补偿、回滚演练",
            "把 handler 健康状态进一步接入自动限权 / 租户配置引导",
        ],
    )


def _handler_done_line(handler: WorldHandlerDescriptor) -> str:
    if handler.external_dispatched:
        return f"{handler.action_type} 已注册真实执行 handler：{handler.user_label}"
    return f"{handler.action_type} 已注册低风险 handler：{handler.user_label}"


def delivery_status_summary() -> dict[str, int]:
    """Compact counts for health summary."""
    counts = {"ready": 0, "partial": 0, "audit_only": 0, "not_ready": 0}
    for item in get_v3_delivery_status():
        counts[item.status] += 1
    return counts


def validate_delivery_status(items: list[DeliveryCapability] | None = None) -> list[str]:
    """Return honest-status problems that should block review.

    This is deliberately simple and deterministic. It catches the most common
    product mistake: marking a capability `ready` while still listing missing
    core pieces.
    """
    problems: list[str] = []
    for item in items or get_v3_delivery_status():
        if item.status == "ready" and item.missing:
            problems.append(f"{item.capability_id}: ready capability still has missing items")
        if item.status == "ready" and not item.done:
            problems.append(f"{item.capability_id}: ready capability has no done evidence")
        if item.status == "ready" and not item.evidence_refs:
            problems.append(f"{item.capability_id}: ready capability has no machine evidence refs")
        missing_refs = [ref for ref in item.evidence_refs if not _evidence_ref_exists(ref)]
        if item.status == "ready" and missing_refs:
            problems.append(
                f"{item.capability_id}: ready capability references missing evidence {missing_refs}"
            )
        if item.status in {"partial", "audit_only", "not_ready"} and not item.missing:
            problems.append(
                f"{item.capability_id}: incomplete capability must explain missing items"
            )
    return problems


def _evidence_ref_exists(ref: str) -> bool:
    path = Path(ref)
    if path.exists():
        return True
    root = Path(__file__).resolve().parents[2]
    return (root / ref).exists()


__all__ = [
    "DeliveryCapability",
    "DeliveryStatus",
    "delivery_status_summary",
    "get_v3_delivery_status",
    "validate_delivery_status",
]
