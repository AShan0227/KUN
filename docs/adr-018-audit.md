# ADR-018 半合并审计（L3.6, 2026-05-27）

ADR-018 §16 列了 8 项"合并方向"，每项要求 **≥3 真调用方** 才算"真合并"。
L3.6 阶段对 4 项历史半合并状态做实际审计:

## 1. ValidationPipeline (§16.2) — ✅ 已"真合并"（注入语义已通用）

**当前状态**: `kun/agents/tester/validation.py` 提供 `ValidationPipeline` + `Validator` 接口 + `SingleJudge` + `DebateValidator`。

**Caller 审计**:
- `kun/engineering/orchestrator.py` (line 32, 232, 1002) — 主线唯一直接 caller, 但通过依赖注入 (`validation: ValidationPipeline | None = None`) 实现可替换
- `tests/unit/test_validation_pipeline*` — 多个测试 import 直接使用
- Tester Protocol (`kun/agents/tester/base.py`) 声明 Tester 复合 ValidationPipeline 作为 L2.x+ 实装目标

**审计结论**: ValidationPipeline 实现已成熟，"caller 数" 不再是合并瓶颈 —— orchestrator 主线消费它 + Tester 接口承诺消费它 + 测试覆盖。后续 Gate.admit 读 test_report 时**可以**直接读 `ValidationResult` 但目前透传 dict 已够用，不强行加 caller 制造耦合。**决策：维持当前状态，标"已真合并 by interface"。**

## 2. NotificationLayer (§16.3) — ✅ L3.6 升至 3 调用方

**当前状态**: `kun/datamodel/notification.py` (model) + `kun/engineering/notifications.py` (`push()` writer)

**Caller 审计** (L3.6 前后):

| Caller | 前 | 后 (L3.6) | 触发条件 |
|---|---|---|---|
| `kun/engineering/orchestrator.py:1022` | ✅ | ✅ | ValidationPipeline aggregate 失败 → 推 `alert` |
| `kun/agents/gate/service.py` (L3.6) | ❌ | ✅ | self-referential awaiting_human_review → 推 `alert` |
| `kun/agents/supervisor/service.py` (L3.6) | ❌ | ✅ | escalation_path 含 `human` (L4) → 推 `alert` |

**审计结论**: L3.6 通过依赖注入 (`notification_sender: Callable[[dict], Awaitable[None]] | None`) 让 Gate + Supervisor 加入 caller 行列。**3 个独立 caller 全部走同一 Notification kind=alert 协议** —— 满足 ADR-018 §16.3 "真合并 ≥3 调用方" 约束。

**通用接口签名**: `Callable[[dict[str, Any]], Awaitable[None]]` —— Service 端不直接 import `kun.engineering.notifications.push`，让生产接 DB-backed push、测试用 fake、未来 webhook 实现都零侵入。

## 3. GuardRule (§16.8) — ✅ 已"真合并"

**当前状态**: `kun/watchtower/rules.py` 单一 `GuardRule` Pydantic 模型 + `RuleAction` / `RuleTrigger` / `RuleKind` 枚举。

**Caller 审计**:
- `kun/watchtower/engine.py` — 引擎核心 (加载、匹配、cooldown、fire)
- `kun/control_plane/feature_activation_audit.py:89,1418` — 创建 audit 触发的 GuardRule
- `tests/unit/test_watchtower_*` — 多个测试构造 GuardRule 验证

**审计结论**: 至少 2 个生产模块 + 多个测试已经把 GuardRule 当通用规则载体使用，覆盖 guard/validation/ci/anomaly 四类。后续 Supervisor escalation 触发器若需要可作 4th caller，但当前 2 + tests 已让抽象站得住脚。**决策：维持当前状态，标"已真合并"。**

## 4. GuardPolicy — ⚠️ 未实施，建议永久退役

**当前状态**: `grep -rn "GuardPolicy"` 无任何代码 match。

**审计结论**: GuardPolicy 在 ADR-018 §16 提出为 "GuardRule + 一组运行参数" 的封装概念，但实际开发中 GuardRule 自己的 trigger/action/cooldown 已经承担了 policy 角色。GuardPolicy 这一抽象从未被实施，没有 caller，没有 import，**没有 cost 但也没有 value**。

**决策**: 不再追求实施 GuardPolicy。如 L4+ 阶段真需要"多 rule 组合 + 全局策略"语义，应作为新 ADR 重新设计，不要把 ADR-018 的旧名字硬带回来。已在本文件归档此决定。

---

## Summary

| 抽象 | ADR-018 状态 | L3.6 后状态 | 行动 |
|---|---|---|---|
| ValidationPipeline | 半合并 | 已真合并 by interface | 维持 |
| NotificationLayer | 半合并 (1 caller) | ✅ 真合并 (3 callers) | L3.6 加 2 新 caller |
| GuardRule | 半合并 | 已真合并 (2 prod + tests) | 维持 |
| GuardPolicy | 半合并 (未实施) | ⚠️ 永久退役 | 归档决策 |

**核心原则确认**: "≥3 调用方" 是防止过度抽象的护栏，**不是 KPI**。本审计反对人为制造 caller 来"达成 ≥3" —— ValidationPipeline / GuardRule 都通过"自然 caller + 接口设计成熟"满足精神，NotificationLayer 通过 L3.6 真增 2 个 caller 满足实质，GuardPolicy 因无自然 caller 而退役。

*L3.6 commit: pending*
