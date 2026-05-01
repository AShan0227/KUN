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

REAL_WORLD_HANDLER_SETUP: dict[str, tuple[str, str, str]] = {
    "email.send": (
        "真实邮件发送",
        "KUN_WORLD_EMAIL_SEND_ENABLED=true",
        "SMTP host/from + 收件人域名白名单",
    ),
    "browser.execute": (
        "真实浏览器操作",
        "KUN_WORLD_BROWSER_EXECUTE_ENABLED=true",
        "Playwright 运行环境 + HTTPS host 白名单",
    ),
    "enterprise_api.post": (
        "企业 API handler",
        "KUN_WORLD_API_POST_ENABLED=true",
        "HTTPS host 白名单 + 必要认证配置",
    ),
}


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
                "LLMRouteGovernor 已进入 LLMRouter 热路径，模型调用前会执行守望的成本/信任/隐私治理咨询",
            ],
            evidence_refs=[
                "kun/interface/llm/router.py",
                "kun/interface/llm/codex_mcp_provider.py",
                "kun/watchtower/llm_route_governance.py",
                "tests/unit/test_llm_router.py",
            ],
            next_steps=[
                "按任务类型自动学习 gpt-5.5 / fallback 的最佳切换策略",
                "把模型结果继续写回 capability_card",
                "让守望按真实成功率动态调整路由",
                "继续用真实 dogfood 校准 LLMRouteGovernor 的成本估算和模型信任规则",
            ],
        ),
        world_gateway_item,
        DeliveryCapability(
            capability_id="compiler_layer",
            label="KUN 编译层",
            status="partial",
            summary="已有轻量输入编译器、CLI/AssetStore 写入入口、批量资料 manifest、聊天附件热路径、Hermes 标准材料包，以及 skill/task/protocol 内部资产编译器；可处理文本/Markdown/HTML/JSON/CSV、本地 PDF 摘要、KUN 内部对象和可选白名单 URL 文本抓取，但还没接 Office/OCR/音视频重后端。",
            done=[
                "新增 kun.compiler，定义 CanonicalMaterial / CanonicalAsset / CompilerProfile / Provenance",
                "轻量编译器已支持 text、markdown、html、json、csv、本地 PDF 文本摘要和安全本地 path 输入",
                "本地文件编译必须传 allowed_root，路径逃逸会被拒绝",
                "URL 输入默认只生成 blocked/placeholder；只有显式开启 HTTPS 白名单抓取才会联网",
                "编译结果带 l1/l2/l3_ref、风险、权限、来源、token 估算和编译 profile",
                "PDF 支持只走本地 pypdf 文本抽取；扫描件/OCR/PDF 深理解仍明确标成限制",
                "URL 编译默认不联网；开启 KUN_COMPILER_URL_FETCH_ENABLED 后仍必须 HTTPS + KUN_COMPILER_URL_ALLOWED_HOSTS 白名单 + 大小限制",
                "CompilerIngestor 已能把 compiled material 转成 knowledge LayeredAsset 并写入 AssetStore",
                "kun compiler compile-text / compile-path 可输出 CanonicalMaterial JSON，不写库",
                "kun compiler compile-url 可输出 URL CanonicalMaterial JSON；默认只产出 placeholder，开白名单后才抓取",
                "kun compiler ingest-text / ingest-path / ingest-url 可把编译结果写入 AssetStore，适合运维脚本和离线资料导入",
                "kun compiler ingest-manifest 可一次导入 text/path/url 批量资料；每项仍走 allowed_root、URL 白名单和 placeholder 不入库规则",
                "kun compiler sync-source 可重复运行 manifest_file 同步源；配置和 manifest 必须留在 config_root 内，适合后续 scheduler/企业资料同步接入",
                "idle-batch 已注册 compiler_sync_sources step；只有显式配置 KUN_COMPILER_SYNC_SOURCE_FILES 才会在闲时同步资料，不会默认乱扫文件或联网",
                "POST /api/compiler/ingest-manifest 提供企业/RAG 资料热入口；租户来自 TenantContext，客户端传 item.tenant_id 会被忽略",
                "POST /api/compiler/ingest-manifest 先生成 CompilerReviewPackage：安全且质量足够才入库，URL/低质量/风险资料会以 review-only 问题信号交给傩/启，不污染普通 Context",
                "REST / WebSocket 聊天附件会先走 InputTranslator，再把原始 bytes 编译成 knowledge asset 写入当前租户 AssetStore，PDF/CSV 等不会先被压扁成普通文本资产",
                "聊天附件 prompt 和 descriptor 会带 compiler_asset_id / compiler_status / compiler_kind，后续可追溯资料来源",
                "Hermes 已能把 CanonicalMaterial 编译成 LLM / skill / API / external_agent / human 不同目标的标准材料包",
                "聊天附件 prompt 会包含 Hermes v5.compiler 材料包，LLM 看到的是 CanonicalMaterial 契约而不是随意文本",
                "kun.compiler.internal_assets 已能把 SKILL.md、TaskRef、Qi Protocol 编译成统一 LayeredAsset，skill/task/protocol 不再只能散落在各自私有格式里",
                "Orchestrator 启动任务时会把 TaskRef 编译成 task LayeredAsset 写入 Context store，并发 task.compiled_asset.created 事件，避免内部编译器只停留在函数层",
                "rejected / placeholder / unsupported material 默认不会污染普通 Context 检索",
                "傩 context maintenance 已能识别编译资产的风险、来源和 profile 缺口",
                "傩 context maintenance 已能给编译资产写入 compiler_quality_score，并对低质量/受限资产标记重新编译建议",
                "kun compiler recompile-candidates 可 dry-run 或显式 --apply 执行傩的重编译建议；本地 path 仍需 allowed_root，URL 仍需 HTTPS 白名单，原资产不会被删除或覆盖",
                "kun context merge-duplicates 可 dry-run 或显式 --apply 执行傩的重复资产合并建议；重复项只软遗忘并记录到主资产，不硬删",
                "MarkItDownMaterialCompiler 已作为可选后端存在：默认禁用，缺 markitdown 包时明确 unavailable，不伪装成已支持",
                "pyproject 已提供 compiler extra，可用 uv sync --extra compiler 安装 MarkItDown 依赖；主环境仍默认不启用该后端",
                "MarkItDown 可选后端仍复用 allowed_root 路径约束，path traversal 会在加载 converter 前被拒绝",
                "kun compiler compile-path / ingest-path 可通过 --backend markitdown 显式选择 MarkItDown 后端；未开启时明确返回 unsupported/unavailable，不会假装已解析 Office 文件",
                "CompilerRegistry 可注册后续音视频 / OCR 后端，但当前不伪装成已接入",
                "idle-batch 已注册 compiler_intake_review step；只有显式传入 compiler_intake_requests 才会生成 CompilerReviewPackage，并把风险/低质量/后端缺失资料以 review-only 写入 Qi 问题队列",
            ],
            evidence_refs=[
                "kun/compiler/models.py",
                "kun/compiler/material.py",
                "kun/compiler/markitdown.py",
                "pyproject.toml",
                "kun/compiler/ingestion.py",
                "kun/compiler/registry.py",
                "kun/compiler/recompile.py",
                "kun/compiler/sync.py",
                "kun/compiler/internal_assets.py",
                "kun/compiler/review_queue.py",
                "kun/engineering/orchestrator.py",
                "kun/datamodel/events.py",
                "kun/context/deduplicate.py",
                "kun/api/compiler.py",
                "kun/cli.py",
                "tests/unit/test_compiler.py",
                "tests/unit/test_compiler_cli.py",
                "tests/unit/test_compiler_ingestion.py",
                "tests/unit/test_compiler_sync.py",
                "tests/unit/test_compiler_review_queue.py",
                "tests/unit/test_idle_batch.py",
                "kun/api/input_payload.py",
                "tests/unit/test_input_payload.py",
                "kun/interface/hermes.py",
                "tests/unit/test_hermes_full_chain_adapter.py",
            ],
            missing=[
                "已有 CLI/脚本级批量 manifest、repeatable sync-source、idle-batch 显式同步、HTTP 热入口、聊天附件写入桥、Hermes 材料包和白名单 URL 抓取能力；但还没接真正企业系统 API connector（SharePoint/Drive/Notion/企业知识库）",
                "MarkItDown 只是可选本地 path 后端；生产要用还需安装依赖并显式开启 KUN_COMPILER_MARKITDOWN_ENABLED",
                "还没接 OCR、音频转写、DOCX/PPTX/XLSX 深结构解析等真实重后端",
                "傩已能做轻量格式质量评分、重编译建议、显式重编译执行和重复资产软合并；但还没接更激进的内容级语义合并/规则蒸馏",
            ],
            next_steps=[
                "把外部文件/网页/企业资料先过 compiler，再进入 Context / Memory / Skill / Hermes",
                "给 MarkItDown 可选后端补企业同步路径和真实样本 dogfood，但继续保持 KUN 自己的 CanonicalMaterial 标准",
            ],
        ),
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
                "ops state-ledger-repair 可 dry-run 或确认后用 EventRow 回放结果修复 state_ledger_entries 当前快照",
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
                "ops dogfood --include-db-long-horizon-drill 可跑时间压缩的多轮 Mission 演练，验证连续推进、复盘和故事线回放",
                "普通任务的 pending action 审批通过后会排入 continuation，API 后台和 cron worker 都能恢复执行",
                "MultiTaskScheduler 已有 V5 并发车道：fast / mission / qi / nuo / world / high_risk，每条车道可独立限流",
                "任务会按风险、任务类型、skill、execution_mode 自动进入不同车道，避免简单任务被复杂任务拖住",
                "/api/chat/run 的非 FastPath 任务默认会先进入 MultiTaskScheduler，再由共享 Orchestrator 执行；可用 KUN_CHAT_SCHEDULER_ENABLED=0 回退直跑",
                "Orchestrator 已把外层 OODA 的 orient / decide / reflect / finalize 写入 DecisionTicket、EventRow 和 StateLedger，长周期任务能追溯每步为什么继续或需要调整",
            ],
            evidence_refs=[
                "kun/engineering/mission_worker.py",
                "kun/engineering/mission_control.py",
                "kun/core/multi_task_scheduler.py",
                "kun/api/missions.py",
                "kun/api/blackboard.py",
                "kun/api/blackboard_data_sources.py",
                "tests/unit/test_mission_worker.py",
                "tests/unit/test_mission_control.py",
                "tests/unit/test_mission_reaper.py",
                "tests/unit/test_multi_task_scheduler.py",
                "tests/unit/test_wave7.py",
            ],
            missing=[
                "续跑还不是原 TaskRow 原地恢复，而是 continuation task 挂回 Mission",
                "自动续跑已经默认打开，并有时间压缩多轮 drill；但还缺真实跨周产品运营 dogfood 来验证长期稳定性和成本边界",
                "StateLedger 持久化是第一版当前快照 cache；已有漂移审计和单任务修复命令，但尚未做全业务对象确定性事件溯源",
                "Mission 复盘和 continuation 摘要只做轻量权重/档位 nudging，还没训练长期策略模型",
                "还没有跑真实跨周产品运营 dogfood；目前只有时间压缩多轮 drill",
                "普通任务 continuation 采用子任务续跑并回写原任务视图，还不是原 TaskRow 原地续跑",
                "Mission / Qi / NUO / WorldGateway worker 还没有全部统一切到 MultiTaskScheduler；目前 chat 热入口和 scheduler API 已接入，后台 worker 仍有分散入口",
            ],
            next_steps=[
                "让 Mission review 结果进一步反向影响 Watchtower Decision Plane 权重",
                "补跨周运营策略模板和真实 dogfood 运营任务",
                "把 Mission、Qi、NUO、WorldGateway 和高风险审批统一接入 lane scheduler",
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
                "任务详情可提交 1-5 分用户反馈和原因标签，反馈会进入任务事件流",
                "任务详情可轻量调整风险、预算、约束和确认策略，并写入 TaskRow / EventRow / StateLedger",
                "任务详情已有隐藏的轻量执行路径图，可从 StateLedger 展示目标、策略、模型、skill/context、外部动作和风险状态",
                "WebSocket 未连接时，对话主入口会降级到 /api/chat/run HTTP 执行并提示用户",
                "主工作区和 /account 已有最小会话入口：可保存/清除 tenant_id、user_id、bearer token，并读取服务端 session",
            ],
            evidence_refs=[
                "frontend/src/app/page.tsx",
                "kun/api/missions.py",
                "kun/api/blackboard.py",
                "kun/api/blackboard_data_sources.py",
            ],
            missing=[
                "已有轻量执行路径图；更完整的 React Flow 节点编辑器仍未做",
                "已有风险/预算/约束/确认策略轻量编辑；还缺可拖拽的高级节点编辑视图",
            ],
            next_steps=[
                "把任务详情里的下一步动作和节点图高级调试视图继续打磨",
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
                "WorldGateway handler 健康体检会暴露只读 diagnostics：缺补偿描述、真实外发/高风险、缺租户密钥配置、EventRow 失败/异常事件统计",
                "未启用的真实外部 handler 会暴露 env/密钥配置缺口",
                "Context / memory 瘦身维护 dry-run 和真实执行入口",
                "傩深度健康报告会自动跑 Context / memory 瘦身 dry-run，把可压缩、可软遗忘、硬删候选和重复候选变成系统发现",
                "默认只露健康 / 成本 / 权限 / 风险，高级诊断折叠",
                "idle-batch 会定期生成 NUO 深度体检摘要并写入事件账本",
                "idle-batch 生成 NUO 深度体检时，会同步把系统 findings 写入启的问题队列，供后台学习引擎持续优化",
                "NUO 深度体检发现会进入 StateLedger 当前状态视图，任务看板能看到系统风险",
                "Pending action executor 会消费 WorldGateway handler 体检，缺配置/缺补偿/blocked 时拦截真实外发",
                "WorldGateway 拦截/失败会进入启的问题队列，作为后续优化输入",
                "NUO 可把 WorldGateway handler 持久化 quarantined/disabled/enabled，执行器会消费这个状态",
                "NUO 可根据 handler 失败率、补偿缺口、配置缺口生成自动 quarantine 决策；默认 dry-run，确认后才写入",
                "idle-batch 会定期跑 WorldGateway handler 自动 quarantine 建议，默认只报告不静默改控制",
                "傩系统体检会扫描审批、暂停任务和 handler 控制之间的协同冲突",
                "协同冲突会生成 dry-run 处置票据，标明能否自动执行、风险级别和建议命令；真实外发默认仍需人工确认",
                "傩系统体检的 warn/error/critical findings 会进入 blackboard 全局状态，并显示在主工作区",
                "NUO 深度体检现在统一覆盖 compiler、context/memory、skill、WorldGateway handler、Qi StrategyPack 草案、多车道调度器和 production/deployment risk",
                "NUO 深度体检会把 findings 转成治理建议，明确 risk_level、default_dry_run、can_apply 和是否需要人工批准",
                "高风险治理建议只进入 NUO report / StateLedger / Qi problem queue，不会由 idle-batch 静默执行",
                "skill 治理会检查 SkillRegistry manifest、dispatcher executor、skill 资产、capability card 和 resource credit，发现 manifest-only、低可靠性或未贡献 skill",
                "Qi 草案治理会检查 review-only StrategyPack methodology 资产，发现 production_action=true、缺证据、强评审和待人工复核状态",
                "多车道治理会检查 fast/mission/qi/nuo/world/high_risk lane 配置和活跃任务压力，避免后台治理或真实外部动作挤占普通任务",
                "production/deployment 风险会把 production_safety_issues、secret audit 和 delivery_status partial/not_ready 汇总到 NUO report",
                "主工作区可开启当前浏览器页级别提醒；有待确认动作或高风险 finding 时会发浏览器 Notification",
                "主工作区会注册本地 service worker，在页面后台也能显示当前浏览器的本机提醒；这不是远程 Push",
                "NUO governance_recommendations 已有显式 apply 入口：目前只允许低风险 context maintenance dry-run/apply，高风险或需要人审的建议会返回结构化 blocked 和 action ticket",
                "NUO 已有只读 context governance audit 入口，能把低价值、重复、高频可抽象、过期/长尾、缺信用归因资产暴露成 review-only 建议",
                "idle-batch 已注册 coordination_remediation step：默认 dry-run 消费傩协同体检票据；显式设置 KUN_COORDINATION_REMEDIATION_MODE=auto_low_risk 后，只会触发已批准且低风险的卡住动作执行器",
                "coordination_remediation 会阻断真实外发、高风险、handler 隔离、暂停无审批门等不适合自动处理的问题，把它们继续留给人工/NUO 治理队列",
                "后台 idle-batch worker 和 hourly cron 默认走 anchor-expand：先跑最高优先级体检/维护，再按轮次预算展开，避免傩/启维护一启动就全量烧完",
            ],
            evidence_refs=[
                "kun/api/nuo/health_panel.py",
                "kun/api/nuo/action_panel.py",
                "kun/api/blackboard.py",
                "kun/api/blackboard_data_sources.py",
                "kun/world/handler_auto_control.py",
                "kun/world/handler_control.py",
                "kun/engineering/nuo_system_health.py",
                "kun/context/governance_audit.py",
                "kun/engineering/coordination_remediation.py",
                "kun/engineering/action_executor.py",
                "kun/qi/problem_queue.py",
                "frontend/public/kun-notifications-sw.js",
                "frontend/src/browserNotifications.ts",
                "tests/unit/test_delivery_status.py",
                "tests/unit/test_action_executor.py",
                "tests/unit/test_system_coordination.py",
                "tests/unit/test_nuo_system_health.py",
                "tests/unit/test_context_governance_audit.py",
                "tests/unit/test_coordination_remediation.py",
            ],
            missing=[
                "已有当前浏览器本地 service worker 提醒；还没做远程 Push、移动端或多设备主动推送",
                "handler 自动 quarantine 已接入定时体检 dry-run，但真实自动执行仍需用户/运维确认",
                "协同体检已能默认 dry-run、显式开启后低风险触发执行器；但还没有自动暂停/恢复所有冲突任务，高风险仍必须人工",
                "NUO 现在能生成统一治理建议，也有低风险显式 apply API；但还没有完整人工批准 UI 和多类治理动作执行器",
            ],
            next_steps=[
                "把 auto-quarantine 高风险建议推送到主看板，并保持真实外发默认人工确认",
                "让守望消费协同体检结果，对低风险卡住任务做安全恢复，对高风险任务升级人工",
                "把 NUO governance_recommendations 做成前端可筛选队列，并逐步扩展更多低风险治理 action",
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
                "最终 execution_mode 选择已进入统一 DecisionTicket、EventRow、StateLedger 和任务结果记忆，后续能学习为什么快跑/深跑/多路径",
                "OODA checkpoint 已进入统一 DecisionTicket、EventRow、StateLedger 和结果记忆，后续能把“何时该纠偏/继续/停下”纳入策略复用",
                "TaskRouter 角色/模型用途选择已进入统一 DecisionTicket 和 StateLedger",
                "真实 LLM provider/model/tier 选择已进入统一 DecisionTicket 和 StateLedger",
                "LLMRouteGovernor 已接入 LLMRouter 调用前热路径，能在真实模型调用前执行成本上限、模型信任和隐私脱敏治理",
                "ContextPacker 上下文选择已进入统一 DecisionTicket 和 StateLedger",
                "MemoryPolicy 和 Hermes 步骤动作选择已进入统一 DecisionTicket，StateLedger 会消费这些票据更新当前动作/记忆策略",
                "SkillSelector 候选技能选择已进入统一 DecisionTicket 和 StateLedger",
                "ValidationPipeline 验证强度选择已进入统一 DecisionTicket 和 StateLedger",
                "BudgetTracker 运行时预算档位已进入统一 DecisionTicket、StateLedger 和事件流",
                "PreDeliverGate 交付审核结果已进入统一 DecisionTicket 和 StateLedger",
                "Preflight / proactive tool / anti-gaming / emergent switch 已进入统一 DecisionTicket，且 StateLedger 会把拦截、风险和动态切路反映到当前状态",
                "信用分配会把 execution_mode / llm_route / decision_ticket 作为可学习资源记录",
                "启 idle replay 生成的 StrategyPack 草稿会附带 qi_experiment DecisionTicket，明确 review-only / needs_review / production_action=false",
                "NUO context maintenance 可压缩过长摘要、软遗忘/硬删除长期未用资产",
                "idle-batch 会把情节规则蒸馏为 methodology 资产，供 ContextPacker 后续复用",
                "NUO / CLI 可查看 resource_credit_stats 里的 top 贡献资源，避免信用只停留在流水账",
                "StateLedger 当前快照和 EventRow 回放故事会消费 credit.assignment.completed，能看到本次任务主要由哪些资源类型和具体资源 key 贡献成功",
                "相似任务的结果记忆 / 元决策记忆会召回成小证据包，接入 Watchtower Decision Plane",
                "相似失败任务现在会形成 strategy_penalty，守望会对失败过的相似策略路径扣分；KUN 不只学成功路径，也会避开烂路",
                "相似任务的执行过程记忆会召回成过程经验摘要，并进入 ContextPacker prompt 摘要",
                "启的问题信号可持久化到 qi_problem_signals，重启后不会全部丢失",
                "傩健康 findings 会进入启的问题队列，Context / WorldGateway / Runtime 等真实问题会成为后台学习输入",
                "任务结果记忆会携带 strategy_pack / execution_mode / skill / context / decision_path，供相似召回和信用分配复用",
                "SkillSelector 会消费 MoE 贡献信用热缓存，让同类候选里历史贡献高的 skill 前排",
                "LLMRouter 会消费模型档位/模型/路线贡献信用，在真实调用前对模型档位做谨慎覆盖",
                "Orchestrator 选 skill 前会预热持久 skill 贡献信用，并走 graph/capability 选择器",
                "required_skills / Watchtower skill_hints 是强信号，但不再绕过贡献信用和能力卡排序",
                "ValueGate 会把 task_type / execution_mode / strategy_pack / step skill 写入贡献信用",
                "ProductionValueEstimator 会读取 ValueGate 历史信用，让同类任务的过往效果影响是否继续投入",
                "相似执行过程记忆里的成功 skill 会进入 Watchtower skill_hints，直接影响 Orchestrator 下一次 required_skills",
                "Context/Memory 资产启动时可按 KUN_CONTEXT_STORE_BACKEND=auto/redis 装 Redis 持久 store，Redis 不可用时诚实降级为 memory",
                "Context maintenance 会识别编译资产的风险、来源和 profile 缺口，傩体检可把 compiler 问题暴露成 finding",
                "傩深度体检会把 StateLedger 漂移/缺历史暴露成 finding，避免账本漂移只藏在 API 里",
                "主前端任务详情已接用户反馈入口，用户满意度不再只停在后端 API",
                "守望决策层会生成 MemoryPolicyTicket，先决定是否用记忆、用哪几类记忆、最多拉几条",
                "新增 MemoryInvocationPolicy 稀疏调用层：会按任务类型、风险、复杂度、失败重试、显式模式和历史资源信用决定是否用记忆、用哪几层、拉多深",
                "Context 选择会把具体资产、资产类型、记忆层和策略标签都写入贡献信用；守望下次会消费这些信用，反向影响 MemoryInvocationPolicy",
                "低信用且有足够历史样本的 memory_layer 会被守望转成 avoid hint；Orchestrator 会把 avoid_memory_layers 真实传给 ContextPacker，避免失败记忆层继续被一锅端塞进 prompt",
                "Orchestrator 已消费 MemoryPolicyTicket：no_memory 会跳过 context，max_items 会限制 context 拉取数量",
                "ContextPacker 已消费 MemoryPolicyTicket 的 memory_layers / avoid_memory_layers，能按任务稀疏激活结果记忆、过程记忆、元决策和方法论",
                "MemoryPolicyTicket 已携带 asset_kinds / preferred_tags，守望策略包能稀疏激活最相关的知识、skill、methodology 和 context 标签",
                "ContextPacker 会软提升匹配策略标签的资产，不硬过滤，避免误分类时错杀真正相关资料",
                "ContextPacker 已消费傩/维护治理标签：soft_forgotten、duplicate_merged、compiler_recompile_recommended、stale_or_risky、low_value 会影响排序或高风险过滤",
                "傩 context maintenance 会写入 fade_score、low_value、stale_or_risky 等显式治理标签，后续 ContextPacker 会消费这些标签",
                "kun context maintenance-run 可 dry-run 或 --apply 执行傩的压缩、软遗忘、硬删除、Fade 打标；--merge-duplicates 会串起重复发现和软合并执行",
                "傩 context governance distill 会把重复、低价值、过期高风险、编译质量差这些反复出现的问题沉淀成 review-only 方法论草案",
                "context_governance_rule_distill 只写 methodology 草稿和证据资产列表，不会自动删除资产或改变生产路由",
                "傩 context governance audit 已能只读发现低价值、重复、高频可抽象、过期/长尾、缺信用归因的记忆或 context 资产，并明确 production_action=false",
                "kun context governance-audit 和 NUO /context-governance/audit 都只输出 review-only 建议，不压缩、不删除、不改生产资产",
                "Context DecisionTicket 会记录 memory_policy，后续可追踪“为什么这次用了/没用记忆”",
                "启 idle replay 已能从问题信号或历史任务生成 review-only 策略候选，用于后台探索更优路径",
                "启 idle replay 已能把候选转成 review-only StrategyPack 草稿，并写入持久化 Qi 信号 evidence，供人/强模型/NUO 后续审查",
                "启 idle replay 会把 StrategyPack 草稿同步沉淀成 review-only methodology 资产，Context/NUO 可审查但不会自动上线",
                "启 idle replay 已有小预算、低并发的离线评估池，可对候选/草稿做 heuristic review-only 评分；无本地模型时会明确 unavailable",
                "启 idle replay 可通过 KUN_QI_LOCAL_REPLAY_EVALUATOR_CMD 接入显式配置的本地/便宜模型评估命令；输出仍是 review-only，不会自动推入生产",
                "启 idle replay 可通过 KUN_QI_STRONG_REVIEW_ENABLED=1 显式接入强模型 judge，对高风险 StrategyPack 草稿做 review-only 复审，并把评审记录沉淀到草稿资产",
                "启 idle replay 可通过 KUN_QI_LAB_REPLAY_ENABLED=1 显式接入 KUN-Lab 历史任务回放，把 StrategyPack 草稿放进沙箱任务里测试，并把回放证据沉淀到草稿资产",
                "启 StrategyPack review step 会把草稿按证据链分成 needs_evidence / blocked / ready_for_human_review，并写回 methodology 资产标签",
                "启 StrategyPack review 明确 promotion_allowed=false / production_action=false，只做审核准备，不会静默改生产路由",
                "启 StrategyPack 草稿后续获得的强模型复审或 Lab 回放证据会合并回原资产，避免候选资产变成过期流水账",
                "启 rollout plan step 会给 ready_for_human_review 草稿生成 shadow→canary→rollout 护栏计划，但不创建或启动生产实验",
                "守望决策层会加载启的 shadow_plan 策略草稿做影子观测，记录 would_outscore_live / would_execution_mode，但不改变真实执行",
                "高严重度或高风险 idle replay 候选会标记 requires_strong_review，不会直接推入生产",
                "idle-batch 已注册 qi_idle_replay step，会把真实问题和已完成任务历史转成 review-only 候选信号",
                "idle-batch 已注册 qi_strategy_pack_review step，会定期刷新启策略草稿的审核状态",
                "idle-batch 已注册 qi_strategy_pack_rollout_plan step，会定期为可审核草稿生成安全推广计划",
                "idle-batch 已注册 external_emergent_scan step，可消费显式数据源或 KUN_EXTERNAL_SCAN_SOURCE_FILES 配置文件，把外部/内部策略线索写入 EmergentSolution 候选库；默认不联网、不爬网、不伪装全网扫描",
                "external_emergent_scan 可通过 KUN_EXTERNAL_SCAN_STRONG_REVIEW_ENABLED=1 显式接入强模型 judge，先复审外部线索再写入候选库",
                "external_skill_scout_plan 已能根据真实 Qi 问题信号/历史任务生成 review-only 外部能力 scout 计划，明确该找什么、去哪类来源找、需要哪些安全验证；不会自动抓取、安装或注册生产 skill",
                "内置 external-skill-scout skill 已接入 dispatcher；执行中发现能力缺口时可生成 review-only 外部能力搜索计划，不联网、不安装、不注册生产 skill",
                "内置 external-skill-review skill 已接入 dispatcher；执行中可对离线外部 skill/工程模板候选做 review-only 安全鉴别，不联网、不安装、不注册生产 skill",
                "external_skill_candidate_review 已能消费离线 GitHub repo / skill metadata，也能通过 KUN_EXTERNAL_SKILL_GITHUB_REPOS 显式 opt-in 抓取 GitHub repo 元数据，做来源、许可、执行脚本、外部网络、密钥、文件写入和 sandbox suitability 的保守鉴别，并把 review-only 候选写入 Qi 问题队列；idle-batch 还会用当前 Qi 问题信号和历史任务生成 task need，把外部候选和真实需求做 task-fit review package 匹配",
                "内置 external-skill-scout 可消费离线 source_registry / candidates，生成外部能力源 + 候选的 review-only scorecard；评分包含安全、许可证、维护度和任务适配度",
                "external_skill_source_plan 已能把任务缺口、推荐来源、离线 source registry 和候选 metadata 合成一张可审查计划，并写入 Qi/NUO review queue；仍然禁止自动 fetch / install / production registration",
            ],
            evidence_refs=[
                "kun/memory/writeback.py",
                "kun/memory/similar_task_recall.py",
                "kun/memory/policy.py",
                "kun/qi/problem_queue.py",
                "kun/qi/idle_replay.py",
                "kun/qi/lab_replay.py",
                "kun/qi/strategy_pack_review.py",
                "kun/qi/strategy_pack_rollout.py",
                "kun/context/packer.py",
                "kun/context/maintenance.py",
                "kun/context/governance_distill.py",
                "kun/context/governance_audit.py",
                "kun/skills/selector.py",
                "kun/engineering/orchestrator.py",
                "kun/engineering/credit_assignment.py",
                "kun/engineering/external_scan.py",
                "kun/engineering/idle_batch.py",
                "kun/qi/external_skill_review.py",
                "kun/api/nuo/health_panel.py",
                "kun/cli.py",
                "kun/watchtower/decision_plane.py",
                "tests/unit/test_watchtower_decision_plane.py",
                "tests/unit/test_llm_route_governance.py",
                "tests/unit/test_v3_memory_scoring_gateway.py",
                "tests/unit/test_memory_policy.py",
                "tests/unit/test_qi_idle_replay.py",
                "tests/unit/test_qi_lab_replay.py",
                "tests/unit/test_qi_strategy_pack_review.py",
                "tests/unit/test_qi_strategy_pack_rollout.py",
                "tests/unit/test_context_governance_distill.py",
                "tests/unit/test_context_governance_audit.py",
                "tests/unit/test_emergent_switch_external.py",
                "tests/unit/test_external_scan_candidates.py",
                "tests/unit/test_idle_batch.py",
                "tests/unit/test_qi_problem_queue.py",
                "tests/unit/test_skill_pheromone_boost.py",
                "tests/unit/test_skill_selector_graph_capability.py",
            ],
            missing=[
                "相似任务召回目前是确定性轻量检索，还不是向量库 / 跨租户匿名经验池",
                "MemoryPolicyTicket 已进入 ContextPacker 过滤和策略标签加权；Hermes use_memory 已接 step 级 action/query 过滤；历史 context 贡献信用已能影响下次记忆策略，但还没接向量库和跨租户匿名经验池",
                "MemoryPolicyTicket 已开始消费傩治理标签，傩也会写入显式 Fade/低价值/风险标签，并能把重复治理模式沉淀成 review-only 方法论草稿；只读治理审计已覆盖低价值/重复/高频抽象/长尾/缺信用，但还没做完整 MemPalace/FadeMem 语义抽象和强模型规则蒸馏",
                "Qi idle replay 目前已有 heuristic_local、可配置本地模型评估口、显式 opt-in 强模型复审口、显式 opt-in KUN-Lab 历史任务回放口、显式 opt-in AI Scientist tree search 证据、草稿审核状态机、shadow/canary 护栏计划和守望影子观测；但仓库不内置具体模型权重，也还没接真实流量 canary 执行链路",
                "StrategyPack 草稿目前只做 review-only，不会自动 promotion；已经能判断是否可交给人审核、生成推广计划，并进入守望 shadow 观测，但还没接人工批准 UI 或真实实验创建",
                "external_emergent_scan 目前只消费显式线索；external-skill-scout / external-skill-review / external_skill_candidate_review 已有离线 source/candidate scorecard、显式 opt-in GitHub repo 抓取、鉴别闭环、真实任务需求匹配和 Qi review queue 接入，但还没接 arXiv/竞品 changelog 抓取器、自动安装、生产 skill 注册或 canary 推广链路",
                "执行过程经验已能影响 Watchtower skill_hints；Hermes use_memory 已开始按单步 action/query 稀疏选择记忆层，但还没有做到全 action choice 的策略改写",
                "贡献信用对模型路由已进热路径，但还需要真实 dogfood 样本校准阈值",
                "ValueGate 已接轻量历史信用，但还没用真实 dogfood 样本训练成稳定的跨任务 gate estimator",
            ],
            next_steps=[
                "用真实 dogfood 样本校准相似经验权重，避免过拟合单次成功路径",
                "把 memory layers 变成 ContextPacker / AssetStore 的真实检索过滤条件",
                "让启用本地模型批量重放历史任务，再由强模型复审少量高价值候选",
            ],
        ),
        DeliveryCapability(
            capability_id="code_capability",
            label="CodeCapability 编程能力",
            status="partial",
            summary="reader / writer / executor / debugger / reviewer 已有基础模块；CodeCapability 现在会安装到 API runtime，并提供只读 review/diff、显式 sandbox run/check，以及默认 dry-run 的单文件改写工作流。它还不是完整 autonomous coder，不能声称已接管 Orchestrator coding task 或自动晋升 skill。",
            done=[
                "CodeCapability facade 聚合 reader、writer、executor、debugger、reviewer",
                "reader 可按 anchor-expand 查找文件、依赖、调用点和基础解释",
                "executor 可在 workspace 软 sandbox 内运行 Python、pytest 和 ruff/black/mypy check，并带 timeout / cwd 限制",
                "reviewer 可对 diff 和 workspace-local 文件做确定性安全 review，识别 eval/exec、shell=True、硬编码 secret 等风险",
                "debugger 可从执行/测试/lint 输出做基础失败分类和修复提示",
                "install_runtime 会安装 app.state.code_capability，workspace root 固定为 KUN_CODE_CAPABILITY_WORKSPACE_ROOT 或进程 cwd",
                "GETTER get_code_capability 会从真实 API runtime 取同一个能力实例",
                "POST /api/code-capability/review-diff 和 /review-file 已接入只读 review 链路",
                "POST /api/code-capability/run-python 和 /check 可显式触发 bounded executor；路径逃逸会被拒绝",
                "CodeCapability API 已接租户 scope 守门：review 需要 code:read，run/check 需要 code:execute；dev 无 scopes 时不增加本地调试摩擦",
                "POST /api/code-capability/propose-change 已接默认 dry-run 的单文件改写闭环：先 review，再 dry-run/apply，再跑 lint/test；apply 后检查失败会自动恢复原文件",
                '内置 code-review skill 已接 CodeCapability reviewer，Orchestrator agent-loop 可通过 `<skill name="code-review">` 做只读 diff/path 审查；不会写文件、不会执行代码、不会自动修复',
                "内置 code-propose-change skill 已接 CodeCapability workflow，默认只做 dry-run 改动验证；真实写入必须显式开启 KUN_CODE_PROPOSE_CHANGE_SKILL_ALLOW_APPLY=1",
                "propose-change 现在会写 code.change.proposed 事件；传入 task_id 时也会进入 StateLedger，记录路径、模式、检查结果、回滚状态和 diff hash，方便傩/启/守望复盘",
                "propose-change 现在还会把改动模式、文件后缀、review/check 结果写入 resource_credit_stats，并更新热贡献信用 cache；后续守望/MoE 能知道哪些编程路径更可靠",
                "成功且通过检查的 propose-change 会生成 review-only 的 draft skill LayeredAsset；它不会自动注册、不会自动安装、不会进入生产路由，只供傩/启/人审核复用",
                "可选开启 KUN_CODE_STRATEGY_TREE_SEARCH_ENABLED=1 后，CodeCapability 会对成功改动做 review-only 树搜索，把更优代码工作流建议写入 draft skill 的 strategy_search_records",
                "代码/调试/重构/测试类任务即使意图层没有显式 required_skills，SkillSelector 也会把 code-review / code-propose-change 作为语义候选塞进 MoE 排序；这只是候选，不会默认写真实工作区",
            ],
            evidence_refs=[
                "kun/skills/code_capability/__init__.py",
                "kun/skills/code_capability/reader.py",
                "kun/skills/code_capability/writer.py",
                "kun/skills/code_capability/executor.py",
                "kun/skills/code_capability/debugger.py",
                "kun/skills/code_capability/reviewer.py",
                "kun/skills/code_capability/workflow.py",
                "kun/skills/builtin/code_review.py",
                "kun/api/runtime.py",
                "kun/api/code_capability.py",
                "tests/unit/test_code_capability.py",
                "tests/unit/test_code_capability_api.py",
                "tests/unit/test_builtin_code_review_skill.py",
                "tests/unit/test_builtin_code_propose_change.py",
                "tests/unit/test_builtin_external_skill_review.py",
                "tests/unit/test_code_skill_draft.py",
                "tests/unit/test_credit_assignment.py",
                "tests/unit/test_api_runtime.py",
            ],
            missing=[
                "API 已暴露受控单文件 propose-change，但默认 dry-run；还不是让 KUN 自主大范围改仓库",
                "sandbox 仍是软隔离；还不是 OS/container 级强隔离，也没有真实网络封锁保证",
                "Orchestrator coding task 已能更容易选到 code-review / code-propose-change 候选，但还没把自动生成补丁、skill draft 审批晋升串成完整 autonomous coding workflow",
                "还没接长周期 dogfood 样本来校准何时生成临时代码、何时沉淀为 skill",
            ],
            next_steps=[
                "把 Orchestrator 的 coding task 显式接到 CodeCapability 服务，而不是只由 HTTP 人工触发",
                "把 code_capability 资源信用和 strategy_search_records 接到更多守望策略和启的代码实验选择里",
                "把 review-only draft skill 接到多次验证、人工审核和受控晋升流程",
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
                "ops backup-drill-object-store-roundtrip 可把备份包上传到对象存储、下载校验并跑 no-write restore 演练",
                "ops secret-audit / NUO secret-audit 可检查默认密码、缺失密钥和半启用外部 handler",
                "NUO 面板会展示 secret-audit 摘要和阻塞/提醒项，密钥风险不再只藏在 API 里",
                "支持 KUN_SECRET_STORE_FILE 外部 JSON secret store，WorldGateway 租户级密钥不再只能散落在 env",
                "ops secret-store-set 可把 KUN_WORLD_* 租户外部动作配置写入本地 JSON secret store，输出不会回显密钥值",
                "NUO /nuo/health/secret-store/set 可受控写入 KUN_WORLD_* 外部动作配置，响应不回显密钥值",
                "NUO 写入真实 handler 启用开关时强制使用全局 scope，并刷新 WorldGateway 注册表，避免用户以为租户级开关已生效",
                "NUO 外部动作网关面板可写入 KUN_WORLD_* 本地 secret-store 配置，帮助邮件/浏览器/企业 API handler 补齐密钥或开关",
                "ops onboard-tenant CLI 可生成租户启动 token、权限 scope 和 smoke curl",
                "ops account-bootstrap CLI 可把租户、owner 成员、token 签发记录写入持久账本",
                "production API 会检查账号账本里的 revoked token，已撤销 token 会被拒绝",
                "账号会话已有最小 refresh token 闭环：refresh/access token 入账、refresh 续短期 access、refresh token 不能访问普通 API",
                "ops account-session CLI 可签发持久入账的 access+refresh 会话包，避免 refresh 功能只停在测试里",
                "已补默认关闭的邀请码注册 API：可创建租户账本、owner 和 access+refresh 会话，但必须显式配置邀请码",
                "已补默认关闭的邀请接受 API：可用全局邀请码或一次性邀请 token 激活 invited 成员并签发 access+refresh 会话",
                "已补默认关闭的最小密码登录：用户可设置 PBKDF2 salted hash 密码，并用 tenant_id/user_id/password 换 access+refresh 会话",
                "已补最小用户会话自助面板 API：查看当前 session、列出自己的 token、撤销自己的 token",
                "生产请求会记录 token 最后使用时间、使用次数、UA 摘要和 IP 哈希，NUO 账号面板可查看最小会话使用账本",
                "NUO 账号面板会把长期 token、缺 IP 指纹、缺 UA 摘要等最小会话风险汇总成会话风险提示",
                "NUO 账号面板可查看当前租户账号、成员和 token 签发账本，且不会暴露 raw bearer token",
                "NUO 账号面板会验证当前请求 user_id 是否为当前租户 active 成员，避免只靠前端本地切租户造成误判",
                "NUO 账号面板可写入成员邀请账本；不会伪装成已发送邮件或已完成成员登录",
                "NUO 账号面板已接成员邀请入口，可生成一次性 token 和可复制交付文案，但仍不会自动发邮件",
                "成员邀请 API 会返回结构化邮件草稿，方便复制交付；草稿明确 draft_only，不会伪装成真实发送",
                "NUO 账号面板可撤销当前租户已签发 token，生产请求中间件会消费撤销结果",
                "前端主入口 / NUO / billing 已通过统一 apiClient 从 localStorage 或 NEXT_PUBLIC_KUN_TENANT_ID/NEXT_PUBLIC_KUN_USER_ID 注入租户与用户 header，避免页面散落 u-sylvan/sylvan",
                "前端已补 /account 会话入口，可手动录入 bearer token 并显示服务端 session，不再只能改 env/localStorage",
                "前端 /account 可调用邀请码注册、接受邀请和 refresh-token 续期接口；仍诚实标注这不是密码登录/OAuth",
                "前端 /account 可设置当前用户密码，也可用 tenant_id/user_id/password 登录并保存 access+refresh session；仍诚实标注这不是 OAuth/设备风控",
                "前端 /account 可查看自己的 token 账本并撤销 token，且不展示原始 token/hash",
                "前端 /account 已补本地租户/用户档案切换器；切换时会清除旧 token，避免跨租户误用",
                "WebSocket 支持短期 ws_ticket；生产浏览器连接必须先用 /api/auth/ws-ticket 换短票据，不再把长期 access token 直接挂在 URL 上",
                "前端 WebSocket 有 auth token 时会先换 ws_ticket；没有 token 时才走本地开发 tenant_id/user_id fallback",
                "ops dogfood CLI 可跑 V4 低风险 smoke，验证 preflight / token / WorldGateway / 诚实边界",
                "ops dogfood --include-db-mission 可额外验证 Mission/RuntimeState/Orchestrator runner 的真实 DB 续跑闭环",
                "ops dogfood --include-db-account 可额外验证账号账本、token 使用账本、refresh session、成员邀请和接受邀请的真实 DB smoke",
                "ops dogfood --include-db-state-ledger-repair 可额外验证 EventRow 回放修复 state_ledger_entries 的真实 DB smoke",
                "ops dogfood --include-db-long-horizon-drill 可额外验证长期 Mission 多轮推进、复盘和故事线回放",
                "TaskBoundaryGuard 已接入 Orchestrator 启动链路；配置 KUN_TASK_BOUNDARY_SCOPE_JSON/FILE 后，会在规划前拦截越界任务并写入 DecisionTicket / EventRow / StateLedger",
                "ops delivery-status CLI 可直接查看 ready / partial / not_ready，防止伪功能被误认为已完成",
                "CI 会检查 scripts/alembic 的 ruff/format，并在 honesty gate 里检查 Alembic 单 head",
                "ops release-check CLI 会检查 V4 release checklist、preflight、legal guard、git dirty/tag、rollback/hotfix 文档",
                "V4 release checklist 和 release gate 已要求记录对象存储往返演练命令，避免 S3/MinIO restore 演练被文档漏掉",
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
                "kun/ops/password_auth.py",
                "kun/api/nuo/account_panel.py",
                "kun/api/session.py",
                "frontend/src/kunApiClient.ts",
                "frontend/src/app/page.tsx",
                "frontend/src/app/nuo/page.tsx",
                "frontend/src/app/billing/page.tsx",
                "kun/api/main.py",
                "kun/security/task_boundary_guard.py",
                "kun/ops/backup_restore.py",
                "kun/ops/dogfood.py",
                "scripts/backup_restore_drill.py",
                "kun/cli.py",
                "tests/unit/test_ops_preflight.py",
                "tests/unit/test_account_sessions.py",
                "tests/unit/test_password_auth.py",
                "tests/unit/test_nuo_account_panel.py",
                "tests/unit/test_ops_backup_restore.py",
            ],
            missing=[
                "完整 OAuth 账号体系",
                "前端已有手动会话/邀请码注册/接受邀请/refresh/最小密码登录和本地租户档案切换入口，NUO 会验证当前租户成员身份，WebSocket 已用短期 ws_ticket；但还没有 OAuth、跨租户服务端切换器、CSRF/设备态风控",
                "已有最小 token 使用风险提示，但还不是完整设备登录态、异常登录风控或多设备管理",
                "前端已有本地 JSON secret-store 写入入口，但还不是云 KMS / 托管 Secret Manager / 自动轮换 / 完整租户自助密钥平台",
                "对象存储备份包往返演练已有命令；真实生产数据库和生产 S3/MinIO 账号的定期恢复演练还没跑过",
                "跨周真实产品运营 dogfood 验收场景",
                "成员邀请已有邮件草稿，但还没有真实邮件发送和账单闭环",
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
        "WorldGateway handler 健康卡会返回缺失 env 和配置步骤；NUO 会把未启用/缺配置的真实外部通道也展示出来",
        "WorldGateway handler 健康卡会返回只读 diagnostics 和 EventRow event_stats，标明缺补偿描述、真实外发/高风险、缺租户密钥配置、失败/异常/blocked 事件数",
        "WorldGateway handler health summary 会汇总真实外发、缺补偿、缺配置、高风险、近期失败、失败/异常事件和缺 handler 数量，给傩做外部风险体检",
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
        "真实 email.send 需要 KUN_WORLD_EMAIL_ALLOWED_DOMAINS 收件人域名白名单，避免 SMTP 配好后误发陌生域名",
        "可开启 KUN_WORLD_REQUIRE_APPROVAL_CONTEXT，让 WorldGateway 真实执行必须带 pending_actions 持久审批上下文，防止直接调用执行路径",
        "email.send / browser.execute / enterprise_api.post 的真实 handler 代码已存在；默认不注册，必须显式开启 env、白名单和审批链",
        "payment.plan / content.publish_plan / deployment.plan 已有默认方案类 handler，只生成计划产物，不支付、不公开发布、不部署",
    ]
    done.extend(_handler_done_line(item) for item in descriptors)

    missing = []
    for action_type, (label, enable_hint, config_hint) in REAL_WORLD_HANDLER_SETUP.items():
        if action_type not in by_type:
            missing.append(
                f"{label}（{action_type} handler 已实现但当前未注册；"
                f"需 {enable_hint} + {config_hint}）"
            )
    missing.extend(
        [
            "集中 Secret Manager、密钥轮换和租户自助密钥配置",
            "真实支付执行 handler 未实现；当前只有 payment.plan 方案产物，涉及钱仍不允许真实自动执行",
            "真实公开发布 handler 未实现；当前只有 content.publish_plan 方案产物，涉及公开发布仍不允许真实自动执行",
            "真实部署/回滚 handler 未实现；当前只有 deployment.plan 方案产物，生产变更仍必须走人工或现有工程流程",
        ]
    )

    real_handlers = set(REAL_WORLD_HANDLER_SETUP) & set(by_type)
    summary = (
        f"已从 WorldGateway 注册表自动识别 {len(descriptors)} 个 handler；"
        f"真实外部 handler 当前启用 {len(real_handlers)}/{len(REAL_WORLD_HANDLER_SETUP)} 个。"
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
    if handler.mode == "plan":
        return f"{handler.action_type} 已注册方案类 handler：{handler.user_label}（不真实外发）"
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
