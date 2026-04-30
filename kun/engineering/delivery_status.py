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
                "StateLedger story 可把某个任务的事件历史压成可读时间线、成本和决策摘要",
                "StateLedger 当前快照已新增 state_ledger_entries 持久表；全局热账本会异步 upsert，黑板读路径会先读 DB 再叠加内存热态",
                "Mission story 可把一个长期目标下的多个 task 账本聚合成总故事线、成本、决策数和下一步",
                "StateLedger replay 会从 EventRow 重建推断状态、外部动作、模型/skill/context 路径、风险和账本缺口",
                "StateLedger audit 可对比当前快照和 EventRow 回放故事，标出状态漂移、成本漂移和历史缺口",
                "傩深度体检会抽检 StateLedger 当前快照和 EventRow 回放，发现状态/成本漂移会生成 finding",
                "默认 StateLedger 通用读路径会读取 state_ledger_entries，黑板不再是唯一能跨重启读当前快照的消费者",
                "idle-batch 有部分复盘和学习能力",
                "Mission 级预算已滚动汇总并可超预算暂停",
                "Mission reaper 可处理 queued/running 卡死任务",
                "NUO 深度体检会识别：有 Mission 可推进但自动续跑 worker 未开启",
                "Mission 可记录下一步和复盘摘要",
                "Mission 续跑 prompt 会带上最近复盘、预算提醒、风险提醒和下一步动作",
                "Mission 复盘中的预算/风险/不确定性会影响下一次 Watchtower 策略权重和执行档位",
                "Mission continuation 完成后会自动写入下一步建议和 last_continuation 摘要，下一轮续跑能消费",
                "自动续跑 worker 默认已接入 cron，但只处理已排队、未超过尝试次数、未被预算/权限挡住的 Mission task；可用 KUN_MISSION_RESUME_WORKER_ENABLED=0 关闭",
                "ops dogfood --include-db-mission 可跑真实数据库 Mission 续跑 smoke",
                "普通任务的 pending action 审批通过后会排入 continuation，API 后台和 cron worker 都能恢复执行",
            ],
            evidence_refs=[
                "kun/engineering/mission_worker.py",
                "kun/engineering/mission_control.py",
                "kun/api/missions.py",
                "kun/api/blackboard.py",
                "kun/api/blackboard_data_sources.py",
                "tests/unit/test_mission_worker.py",
                "tests/unit/test_mission_control.py",
                "tests/unit/test_mission_reaper.py",
                "tests/unit/test_wave7.py",
            ],
            missing=[
                "续跑还不是原 TaskRow 原地恢复，而是 continuation task 挂回 Mission",
                "自动续跑已经默认打开，但还缺跨周真实产品运营 dogfood 来验证长期稳定性和成本边界",
                "StateLedger 持久化是第一版当前快照 cache；已有漂移审计，但尚未做完整确定性快照重建",
                "Mission 复盘和 continuation 摘要只做轻量权重/档位 nudging，还没训练长期策略模型",
                "还没有跑跨周真实产品运营 dogfood",
                "普通任务 continuation 采用子任务续跑并回写原任务视图，还不是原 TaskRow 原地续跑",
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
                "主工作区会显示能力边界数量：可测 / 半闭环 / 仅审计 / 未就绪",
                "主工作区会显示用户可见的 top 能力缺口，并跳转到傩查看详情",
                "主工作区会显示傩发现的系统风险，避免风险只躲在 NUO 高级页",
                "长期目标卡片可按需展开 Mission 故事线，查看跨 task 的事件数、决策数、成本、原因和下一步",
                "长期目标卡片展开后可轻量调整下一步，走 Mission API 持久化，不需要进高级节点图",
                "任务详情会显示账本审计，能看出当前快照和长期事件回放是否漂移",
                "WebSocket 未连接时，对话主入口会降级到 /api/chat/run HTTP 执行并提示用户",
            ],
            evidence_refs=[
                "frontend/src/app/page.tsx",
                "kun/api/missions.py",
                "kun/api/blackboard.py",
                "kun/api/blackboard_data_sources.py",
            ],
            missing=[
                "更完整的节点图 / 高级调试视图",
                "更广泛的用户轻量编辑入口（预算、风险、约束、确认策略）",
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
                "Pending action executor 会消费 WorldGateway handler 体检，缺配置/缺补偿/blocked 时拦截真实外发",
                "WorldGateway 拦截/失败会进入启的问题队列，作为后续优化输入",
                "NUO 可把 WorldGateway handler 持久化 quarantined/disabled/enabled，执行器会消费这个状态",
                "NUO 可根据 handler 失败率、补偿缺口、配置缺口生成自动 quarantine 决策；默认 dry-run，确认后才写入",
                "idle-batch 会定期跑 WorldGateway handler 自动 quarantine 建议，默认只报告不静默改控制",
                "傩系统体检会扫描审批、暂停任务和 handler 控制之间的协同冲突",
                "傩系统体检的 warn/error/critical findings 会进入 blackboard 全局状态，并显示在主工作区",
            ],
            evidence_refs=[
                "kun/api/nuo/health_panel.py",
                "kun/api/nuo/action_panel.py",
                "kun/api/blackboard.py",
                "kun/api/blackboard_data_sources.py",
                "kun/world/handler_auto_control.py",
                "kun/world/handler_control.py",
                "kun/engineering/nuo_system_health.py",
                "kun/engineering/action_executor.py",
                "kun/qi/problem_queue.py",
                "tests/unit/test_delivery_status.py",
                "tests/unit/test_action_executor.py",
                "tests/unit/test_system_coordination.py",
            ],
            missing=[
                "定期 NUO 体检已进入主工作区和当前状态视图，但还没做浏览器/移动端主动推送",
                "handler 自动 quarantine 已接入定时体检 dry-run，但真实自动执行仍需用户/运维确认",
                "协同体检目前先发现冲突和给建议，还没有自动暂停/恢复所有冲突任务",
            ],
            next_steps=[
                "把 auto-quarantine 高风险建议推送到主看板，并保持真实外发默认人工确认",
                "让守望消费协同体检结果，对低风险卡住任务做安全恢复，对高风险任务升级人工",
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
                "ProtocolRegistry 协议消费已进入统一 DecisionTicket 和 StateLedger",
                "TaskRouter 角色/模型用途选择已进入统一 DecisionTicket 和 StateLedger",
                "真实 LLM provider/model/tier 选择已进入统一 DecisionTicket 和 StateLedger",
                "ContextPacker 上下文选择已进入统一 DecisionTicket 和 StateLedger",
                "SkillSelector 候选技能选择已进入统一 DecisionTicket 和 StateLedger",
                "ValidationPipeline 验证强度选择已进入统一 DecisionTicket 和 StateLedger",
                "BudgetTracker 运行时预算档位已进入统一 DecisionTicket、StateLedger 和事件流",
                "PreDeliverGate 交付审核结果已进入统一 DecisionTicket 和 StateLedger",
                "信用分配会把 execution_mode / llm_route / decision_ticket 作为可学习资源记录",
                "NUO context maintenance 可压缩过长摘要、软遗忘/硬删除长期未用资产",
                "idle-batch 会把情节规则蒸馏为 methodology 资产，供 ContextPacker 后续复用",
                "NUO / CLI 可查看 resource_credit_stats 里的 top 贡献资源，避免信用只停留在流水账",
                "相似任务的结果记忆 / 元决策记忆会召回成小证据包，接入 Watchtower Decision Plane",
                "相似任务的执行过程记忆会召回成过程经验摘要，并进入 ContextPacker prompt 摘要",
                "启的问题信号可持久化到 qi_problem_signals，重启后不会全部丢失",
                "任务结果记忆会携带 strategy_pack / execution_mode / skill / context / decision_path，供相似召回和信用分配复用",
                "SkillSelector 会消费 MoE 贡献信用热缓存，让同类候选里历史贡献高的 skill 前排",
                "LLMRouter 会消费模型档位/模型/路线贡献信用，在真实调用前对模型档位做谨慎覆盖",
                "Orchestrator 选 skill 前会预热持久 skill 贡献信用，并走 graph/capability 选择器",
                "required_skills / Watchtower skill_hints 是强信号，但不再绕过贡献信用和能力卡排序",
                "ValueGate 会把 task_type / execution_mode / strategy_pack / step skill 写入贡献信用",
                "ProductionValueEstimator 会读取 ValueGate 历史信用，让同类任务的过往效果影响是否继续投入",
                "Context/Memory 资产启动时可按 KUN_CONTEXT_STORE_BACKEND=auto/redis 装 Redis 持久 store，Redis 不可用时诚实降级为 memory",
                "傩深度体检会把 StateLedger 漂移/缺历史暴露成 finding，避免账本漂移只藏在 API 里",
            ],
            evidence_refs=[
                "kun/memory/writeback.py",
                "kun/memory/similar_task_recall.py",
                "kun/qi/problem_queue.py",
                "kun/context/packer.py",
                "kun/skills/selector.py",
                "kun/engineering/orchestrator.py",
                "kun/engineering/credit_assignment.py",
                "kun/api/nuo/health_panel.py",
                "kun/cli.py",
                "kun/watchtower/decision_plane.py",
                "tests/unit/test_watchtower_decision_plane.py",
                "tests/unit/test_v3_memory_scoring_gateway.py",
                "tests/unit/test_qi_problem_queue.py",
                "tests/unit/test_skill_pheromone_boost.py",
                "tests/unit/test_skill_selector_graph_capability.py",
            ],
            missing=[
                "相似任务召回目前是确定性轻量检索，还不是向量库 / 跨租户匿名经验池",
                "执行过程经验目前只进入上下文提示，还没有直接改写 Watchtower 策略权重或 step 级 action choice",
                "贡献信用对模型路由已进热路径，但还需要真实 dogfood 样本校准阈值",
                "ValueGate 已接轻量历史信用，但还没用真实 dogfood 样本训练成稳定的跨任务 gate estimator",
            ],
            next_steps=[
                "用真实 dogfood 样本校准相似经验权重，避免过拟合单次成功路径",
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
                "生产认证支持 KUN_AUTH_SECRETS 多密钥验签，可做不中断轮换",
                "NUO 外部动作审批支持 world:approve / world:dispatch scope 守门",
                "已补 Postgres backup 和 restore smoke 脚本",
                "已补本地备份/恢复演练脚本，可生成 tar.gz + manifest，并支持 restore dry-run 校验",
                "生产 ready 自检会标出 auth / RLS / 默认密钥问题",
                "ops preflight 会阻断半启用的真实 WorldGateway handler 配置，避免上线后才发现密钥/env 缺失",
                "Mission reaper、context maintenance、resource credit 已补 Prometheus 指标",
                "ops preflight CLI 可在上线前检查配置、迁移、备份脚本和能力边界诚实性",
                "ops backup-drill-create / backup-drill-restore-dry-run 可做本地关键配置包和 no-write restore 演练",
                "ops secret-audit / NUO secret-audit 可检查默认密码、缺失密钥和半启用外部 handler",
                "支持 KUN_SECRET_STORE_FILE 外部 JSON secret store，WorldGateway 租户级密钥不再只能散落在 env",
                "ops secret-store-set 可把 KUN_WORLD_* 租户外部动作配置写入本地 JSON secret store，输出不会回显密钥值",
                "NUO /nuo/health/secret-store/set 可受控写入 KUN_WORLD_* 外部动作配置，响应不回显密钥值",
                "ops onboard-tenant CLI 可生成租户启动 token、权限 scope 和 smoke curl",
                "ops account-bootstrap CLI 可把租户、owner 成员、token 签发记录写入持久账本",
                "production API 会检查账号账本里的 revoked token，已撤销 token 会被拒绝",
                "账号会话已有最小 refresh token 闭环：refresh/access token 入账、refresh 续短期 access、refresh token 不能访问普通 API",
                "ops account-session CLI 可签发持久入账的 access+refresh 会话包，避免 refresh 功能只停在测试里",
                "已补默认关闭的邀请码注册 API：可创建租户账本、owner 和 access+refresh 会话，但必须显式配置邀请码",
                "已补默认关闭的邀请接受 API：可用全局邀请码或一次性邀请 token 激活 invited 成员并签发 access+refresh 会话",
                "已补最小用户会话自助面板 API：查看当前 session、列出自己的 token、撤销自己的 token",
                "生产请求会记录 token 最后使用时间、使用次数、UA 摘要和 IP 哈希，NUO 账号面板可查看最小会话使用账本",
                "NUO 账号面板可查看当前租户账号、成员和 token 签发账本，且不会暴露 raw bearer token",
                "NUO 账号面板可写入成员邀请账本；不会伪装成已发送邮件或已完成成员登录",
                "NUO 账号面板可撤销当前租户已签发 token，生产请求中间件会消费撤销结果",
                "前端主入口 / NUO / billing 已通过统一 apiClient 从 localStorage 或 NEXT_PUBLIC_KUN_TENANT_ID/NEXT_PUBLIC_KUN_USER_ID 注入租户与用户 header，避免页面散落 u-sylvan/sylvan",
                "WebSocket 支持 auth_token 查询参数；生产环境必须使用签名 token，不再接受裸 tenant_id/user_id 打开会话",
                "ops dogfood CLI 可跑 V4 低风险 smoke，验证 preflight / token / WorldGateway / 诚实边界",
                "ops dogfood --include-db-mission 可额外验证 Mission/RuntimeState/Orchestrator runner 的真实 DB 续跑闭环",
                "ops dogfood --include-db-account 可额外验证账号账本、token 使用账本、refresh session、成员邀请和接受邀请的真实 DB smoke",
                "ops delivery-status CLI 可直接查看 ready / partial / not_ready，防止伪功能被误认为已完成",
                "CI 会检查 scripts/alembic 的 ruff/format，并在 honesty gate 里检查 Alembic 单 head",
                "ops release-check CLI 会检查 V4 release checklist、preflight、legal guard、git dirty/tag、rollback/hotfix 文档",
            ],
            evidence_refs=[
                ".github/workflows/ci.yml",
                "docker-compose.dev.yml",
                "alembic/versions",
                "docs/DEPLOY.md",
                "docs/ops/release-checklist-v4.md",
                "kun/ops/preflight.py",
                "kun/ops/release_gate.py",
                "kun/ops/secret_audit.py",
                "kun/ops/secret_store.py",
                "kun/api/nuo/health_panel.py",
                "kun/ops/tenant_onboarding.py",
                "kun/ops/account_registry.py",
                "kun/ops/account_sessions.py",
                "kun/api/nuo/account_panel.py",
                "kun/api/session.py",
                "frontend/src/kunApiClient.ts",
                "frontend/src/app/page.tsx",
                "frontend/src/app/nuo/page.tsx",
                "frontend/src/app/billing/page.tsx",
                "kun/api/main.py",
                "kun/ops/backup_restore.py",
                "kun/ops/dogfood.py",
                "scripts/backup_restore_drill.py",
                "kun/cli.py",
                "tests/unit/test_ops_preflight.py",
                "tests/unit/test_account_sessions.py",
                "tests/unit/test_nuo_account_panel.py",
                "tests/unit/test_ops_backup_restore.py",
            ],
            missing=[
                "完整密码登录 / OAuth 账号体系",
                "前端仍只有 localStorage/env 注入入口，没有登录 UI、租户切换器、CSRF/设备态风控；WebSocket token 通过 query 传递，生产还应升级到更正式的会话/短期票据方案",
                "完整设备登录态和异常登录风控",
                "云 KMS / 托管 Secret Manager、自动轮换和租户自助密钥配置",
                "真实数据库/S3 环境的备份恢复演练",
                "跨周真实产品运营 dogfood 验收场景",
                "成员邀请邮件发送和账单闭环",
            ],
            next_steps=[
                "把账号账本升级成自助注册、成员管理和完整会话体系",
                "做真实环境备份/恢复 smoke test",
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
        "WorldGateway 审批执行会写入 world_action_executions 持久账本，记录 attempt/status/handler/外发/补偿/错误",
        "WorldGateway handler 健康卡会优先消费持久执行账本，避免只读 pending_actions.payload 的脆弱 JSON",
        "真实外部动作缺少 external_dispatch_confirmed=true 时会被策略层拦截",
        "NUO 判断 handler blocked/unregistered 时，审批后执行器会拦截并保持任务暂停",
        "WorldGateway 执行结果会回写 world_action / world_handler / world_policy 决策票据信用",
        "真实 handler 执行时可按租户读取 SMTP / 企业 API / 浏览器白名单覆盖配置",
        "傩体检、secret audit、preflight 已识别 KUN_TENANT_<TENANT>_* 租户级外部动作配置",
        "傩可从 world_action_executions 账本识别重试/补偿/缺幂等风险，不会默认重复真实外发",
        "任务 preflight 会尽量生成 WorldGateway 已注册的低风险动作类型：email.draft / local_file.write / webhook.post_dry_run / browser.plan",
        "执行中 LLM 可通过 world-request 内置 skill 生成待审批动作并暂停任务，但真实外发仍必须走 NUO/WorldGateway 审批链",
        "真实外发和高风险动作读取 handler health/control 失败时默认 fail-closed，避免绕过傩控制",
        "真实外发和高风险动作必须带显式 idempotency_key，重复幂等键会被代码和 DB partial unique index 双层拦截",
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
            "集中 Secret Manager、密钥轮换和租户自助密钥配置",
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
            "alembic/versions/0023_world_action_executions.py",
            "tests/unit/test_action_executor.py",
            "tests/unit/test_delivery_status.py",
            "tests/unit/test_world_handler_health.py",
            "tests/unit/test_concurrency_safety.py",
        ],
        next_steps=[
            "按租户配置真实 email / browser / enterprise API handler",
            "把 env 级租户覆盖升级成集中 Secret Manager、轮换、重试、补偿、回滚演练",
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
        if missing_refs:
            problems.append(
                f"{item.capability_id}: capability references missing evidence {missing_refs}"
            )
        if item.status in {"partial", "audit_only"} and item.done and not item.evidence_refs:
            problems.append(
                f"{item.capability_id}: incomplete capability with done claims needs evidence refs"
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
