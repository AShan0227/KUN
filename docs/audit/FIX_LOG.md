# 修复日志（loop 逐条追加）

格式：`<时间> <id> <结果> <commit> — <说明>`

2026-06-23 G01 ✅ — CI 触发加 鲲V1.1-dev(push+PR)、集成测试去 soft-fail；typecheck 硬化拆为 G09、覆盖率门槛拆为 G10
2026-06-23 F002+F006 ✅ — logging._add_tenant 捕获 MissingTenantContextError，防生产启动崩溃（+5 单测）
2026-06-23 F004 ✅ — 与 G01 重复（CI 已在 鲲V1.1-dev 触发）
2026-06-23 F003 ✅ — 关闭 prompt 自动执行代码块 RCE：NEVER_PROACTIVE 硬拦 python-exec/shell-exec(三层)+删触发器；改写/新增 7 测试
2026-06-23 F001 📐 — needs-design：门禁自批+覆盖人审是信任模型缺陷，写方案 docs/audit/proposals/F001.md（不在巨型文件里盲改）
2026-06-23 F005 ✅ — 跨租户越权检测：session_scope 显式 tenant_id≠ambient(非bypass)→ emit 指标+CRITICAL log(+5 测试)
2026-06-23 F007+F023 ✅ — 接通 JWT 鉴权中间件(auth on→JWT强校验/忽略自报头/401；auth off→dev现状)，抽 resolve_request_tenant 纯函数+7测试
2026-06-23 F008/F009/F016/F017 📐 — needs-design 合并方案 docs/audit/proposals/F008-F009-F016-F017.md（RSI 自产证据/硬编码门禁分数+god-module 拆分，不盲改）
2026-06-23 F018 ✅ — feature_activation_audit rmtree 脚枪：加 _prepare_audit_output_dir 守卫(拒危险根/只清自有目录)+8 测试
2026-06-23 F020 ✅ — ValidatorKind 加 'ensemble'，修复 tier3 聚合 Pydantic 必崩 +3 测试
2026-06-23 F024 ✅ — 修正 Anthropic 定价表(权威来源)+计入 cache-write 成本(此前漏计)，+6 测试
