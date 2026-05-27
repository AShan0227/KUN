# L6 · 进度微日志（追加式）

> 完整回顾在 `L6-retrospective.md`（L6 全部完成时写）。
> 本文件每完成一个子任务追加 3-5 句。
>
> Phase 2 商业化推进中, **不上线打磨产品**。

---

## L6.A · Automation Router framework — Browser-first hybrid

**完成**：2026-05-27 / commits 738a5df + 803e8ac

**做了什么**：
- 新建 `kun/interface/automation/` 子系统区分于 `adapters/` (输出翻译)
- `Action` / `ActionResult` frozen dataclass + `AutomationAdapter` Protocol (kind / platform / supported_operations / execute / health_check)
- `AdapterRegistry`: per-(platform, operation) list
- `AdapterRouter`: damped score `0.5 + min(1, sample/30) * (success_rate - 0.5)` 与 LLM Router 同公式; DEFAULT_API_PREFERENCE_SCORE=0.7
- API/Browser base 类: HttpCaller / PageFactory 全 DI 注入, 无 Playwright dep
- BrowserAdapter auto page.close() + 异常 auto-screenshot
- 26 单测

**关键决策**：
- **不引 Playwright dep**: PageFactory Protocol 推迟到具体平台实现, 现在 KUN 不付 Playwright 二进制安装/CI 代价
- **冷启动 damping 复用 LLM Router 公式**: 一致性 > 重新发明; sample/30 weight 在所有 Router 共享认知
- **Action 是 frozen + payload dict**: 跨 process 序列化友好; payload 由 adapter 自校验

---

## L6.B · IndustryEvalSuite — 行业评测集框架

**完成**：2026-05-27 / commit 5b2ae43

**做了什么**：
- `GoldenTask` frozen + `IndustryEvalSuite.run(executor) → EvalReport`
- 4 built-in metric: exact_key_match / status_ok / keys_present / jaccard_payload
- PASS_THRESHOLD = 0.8 (boundary 包含)
- by_platform + by_operation 聚合在 report 里; executor exception 计 error_tasks
- 17 单测覆盖所有 metric + executor 异常 + custom metric + aggregation

**关键决策**：
- **industry-agnostic framework**: 同一套骨架支持 4 个行业 (电商/投放/内容分发/CRM), caller 只填 GoldenTask 列表
- **custom_metrics 优先于 built-in**: 让特殊行业 (如内容分发需要内容质量评分) 可注入领域专家 metric
- **error vs fail 分开计数**: executor 抛异常和 metric 给低分是两件事; report 同时给 pass/fail/error 才能定位是 KUN 跑挂了还是质量没达标

---

## L6.C · ADR-026 接入层架构决策

**完成**：2026-05-27 / 写入 decisions.md

**做了什么**：
- 写 ADR-026 "Phase 2 接入层架构 — Browser-first hybrid"
- 论证: 为什么不上 SDK (每个行业各 SDK 总和 > 浏览器自动化稳定性 ROI)
- Router 算法 + Fallback policy 文档化
- 自演化路径: Browser-first → API warmup → API primary + Browser fallback

**关键决策**：
- **Browser primary / API accelerator / 无 SDK**: 跟产品方向耦合 — 用户要的是"快进入 4 个行业先打磨", SDK 学习曲线和厂商更新打断节奏
- **API 当加速器不当主路径**: 大部分平台 API 限流严, 401/403 比 browser 多; browser 反而稳

---

## L6.D-Shopify · Shopify adapter (API + Browser)

**完成**：2026-05-27 / commit a87c31a

**做了什么**：
- `ShopifyAPIAdapter`: 3 ops (create_product / list_orders / get_product) via _OPERATION_MAP (dict-driven, 易扩展)
- URL template `{product_id}` 自动从 payload 替换
- create_product wraps body 在 `{"product": {...}}` (Shopify 要求)
- HTTP 401/403 → auth_required, 429 → rate_limited, 5xx → failed
- `ShopifyBrowserAdapter`: 同 3 ops 走 admin panel selectors (_SELECTORS dict)
- 20 单测 + 3 个 Router 集成测试 (cold start picks browser / warmup prefers API / 429 fallback)

**关键决策**：
- **国内电商可复用骨架**: 淘宝/京东/拼多多/小红书只换 _OPERATION_MAP + _SELECTORS, 不动 Router/Registry/Action
- **Auto-screenshot on browser exception**: browser 自动化失败时, screenshot 比 log 更有诊断价值, 应该走 artifact_refs

---

## L6.D-EcomEval · 电商 Shopify 评测集 (6 golden tasks)

**完成**：2026-05-27 / commit c7307bb

**做了什么**：
- `kun/evaluation/ecommerce_shopify.py` · 6 个 golden task (3 op × 2 场景: happy + edge)
- 都用 `metric_name="keys_present"` (Shopify 动态返回 id, 不能 exact_match)
- 11 单测 + 端到端 eval: pass_rate ≥ 0.85 视为冷启动 OK
- executor 用 `requested_kind="api"` 跑 happy path (cold-start router 否则会 pick browser)

**关键决策**：
- **冷启动用 `requested_kind="api"` 跑 eval**: golden_output 是 API 形态, 应该测 API path; warmup 后 router 自然偏好 API
- **edge case 用 exact_key_match + 空 golden**: 缺 payload 的边界 case adapter 不应崩, 但具体输出形态宽松接受

---

## L6.AuthScaffold · JWT + tenant_id + RLS 绑定 (默认 disabled)

**完成**：2026-05-27 / commits 3da10f3 + 23c8150

**做了什么**：
- 新建 `kun/api/auth/` 子系统: jwt_token.py + middleware.py + rls.py + __init__.py
- HS256 JWT 用 stdlib hmac + hashlib, **不引 PyJWT dep** (单 secret + 内部 issuer-verifier ROI 不值)
- `decode_jwt` 验签 + exp + leeway + reserved-claim 防覆盖 + 拒非 HS256 alg
- `resolve_tenant_id` 纯函数: disabled → default_tenant_id; enabled → 必须 valid Bearer JWT
- `bind_tenant_to_session` 包装 `SET LOCAL app.tenant_id`, 字符白名单防 SQL injection (PG SET 不支持 bind parameter)
- config: `auth_enabled` / `auth_jwt_secret` / `auth_token_ttl_seconds` + validator 强制 enabled 时 secret ≥32 chars
- 44 单测: JWT round-trip / tamper rejection / expiry+leeway / non-HS256 / missing claim / non-object payload / SQL injection / config validator

**关键决策**：
- **stdlib HMAC 而非 PyJWT**: 单 secret + 内部 issuer-verifier, ROI 不值 10MB 依赖; 升级 RS256/ES256 (multi-issuer) 时再换 PyJWT
- **默认 `KUN_AUTH_ENABLED=false`**: 用户要求"先完善但不启用"; flag flip 即切换, 现有 default_tenant_id fallback 完全不变
- **RLS SQL injection 防御靠白名单**: PG `SET` 不允许 bind parameter, 必须字符校验; 拒 `'`/`;`/`\n`/空格等危险字符
- **resolve_tenant_id 是纯函数**: 不绑 FastAPI Request 类, 便于单测; decoder DI 注入 stub 避免 JWT 计算

**升级路径** (Phase 2 中期生产前):
  1. KUN_AUTH_ENABLED=true + KUN_AUTH_JWT_SECRET=`<48 char random>`
  2. alembic 改 RLS policy 用 `current_setting('app.tenant_id')` (不带 true → 缺失即报错)
  3. KUN_DEFAULT_TENANT_ID=null (production 拒启动 if 未设)
  4. FastAPI middleware wire: 用 resolve_tenant_id() 落 request.state.tenant_id, 然后每个 session_scope 入口调 bind_tenant_to_session()

---

## L6.E · Director → Executor → AdapterRouter e2e wiring

**完成**：2026-05-27 / commits (L6.E-1 + L6.E-2)

**做了什么**：
- `kun/agents/director/automation_intent.py` · `extract_automation_action(parsed, *, tenant_id)` 纯函数
  - 从 IntentInterpreter 已 parse 的 JSON 抽 `automation: {target_platform, operation, payload}`
  - 字段校验 (类型 + 非空) + 可选 requested_kind (api/browser)
  - target_platform 自动 trim + lowercase
- `kun/agents/executor/automation_runner.py` · `AutomationRunner.run(action) → RouterDecision`
  - 桥接 AdapterRouter, emit `action.started` / `action.completed` / `action.failed` 事件
  - 状态累积 emit raise 被吞 (不打挂主路径, ADR-024 frozen_dataclass 模式)
  - `action_result_to_artifact()` 转 Phase 1 风格 dict, 供 capability writeback
- 34 测: 18 director-side + 10 executor-side + 6 integration (e2e: parsed JSON → Action → Router → Shopify API → ActionResult)
- 不需改 IntentInterpreter — 用现有 parsed dict 抽取 (single-responsibility 保持)
- TaskRef `extra='allow'` 已经支持后续 caller 挂 `automation_action` 字段, 本次不强制改 IntentInterpreter

**关键决策**：
- **不改 IntentInterpreter, 抽取作独立纯函数**: 让 LLM intent parsing 与 Action 抽取解耦. IntentInterpreter 继续单一职责 (NL → parsed dict), 任何 caller 可独立调 extract_automation_action.
- **AUTOMATION_PROMPT_HINT 作 system prompt 片段**: 暴露给上层让用户/调用方按需贴到 IntentInterpreter 的 system prompt. 不强制改 IntentInterpreter 内部 prompt — 留出灵活性.
- **AutomationRunner 是薄 wrapper 不是新 dataclass**: 直接复用 Action / ActionResult / RouterDecision. 价值在事件 emit + 单测易写.
- **emit 失败不打挂主路径**: `try/except Exception` 包 `_safe_emit`. action 执行本身的异常上抛 (这是 Executor 的决策, 不是基础设施的).
- **集成测全 DI 替身**: 0 LLM 实调 / 0 HTTP / 0 Playwright. _StubLLMRouter + _FakeHttpCaller + _FakePage. 6 个 integration 测 0.11s 跑完.

**端到端示例 (test_e2e_director_to_router_happy_path)**:
  parsed_intent = {"automation": {"target_platform": "shopify", "operation": "create_product", "payload": {"title": "Test Mug"}}}
  → extract_automation_action() → Action(target_platform=shopify, op=create_product, ...)
  → AutomationRunner.run() → Router 选 ShopifyAPIAdapter → fake_http_caller 收到 POST /admin/api/.../products.json
  → ActionResult(status="ok") → action_result_to_artifact() Phase 1 风格 dict

---
