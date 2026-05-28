"""交付总监 (Mission Director) — V7 §9.7 一级子系统.

身份 (V7 §9.7): 任务级监督角色, **可单独配置 model / provider / 档位**, 不替代
启 / 傩 / 具体执行 runner, 只对 mission 交付闭环拥有**监督和阻断权**.

核心职责 (V7 §9.7):
1. 任务方案对齐监督 (方案线主线): 审查任务执行是否仍在 TaskPlanVersion 范围内
2. 信息缺口监督: 审查 info_gap 是否真补齐, 避免半补齐就开跑
3. 任务拆解审查: 审查 work item 拆解是否覆盖任务方案的所有 deliverable
4. worker 分配审查: 匹配 work item 风险等级和 skill 要求
5. 证据审查: 审查 evidence ledger 是否覆盖 TaskPlan 声明的证据计划
6. 验收状态审查: **禁止把测试/自评分/门禁通过误判为最终交付完成**
7. 方案错漏发现: 触发 PlanChangeProposal (V7 §10.3.2)

调度优先级 (V7 §9.7): **高于普通业务执行**, 监督先于继续开发或关闭任务.

输出产物:
- MissionAlignmentReview (每 tick / 每 milestone)
- PlanChangeProposal (发现方案错漏时, 三档严重等级)
- GateEvaluation (阻断 deliver / accept 时)
"""

from kun.agents.mission_director.service import (
    AlignmentVerdict,
    MissionAlignmentReview,
    MissionDirectorService,
    MissionReviewEmitter,
    PlanChangeProposal,
    PlanChangeSeverity,
)

# Public alias — Mission Director 是对外角色名
MissionDirector = MissionDirectorService

__all__ = [
    "AlignmentVerdict",
    "MissionAlignmentReview",
    "MissionDirector",
    "MissionDirectorService",
    "MissionReviewEmitter",
    "PlanChangeProposal",
    "PlanChangeSeverity",
]
