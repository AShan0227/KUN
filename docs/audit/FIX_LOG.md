# 修复日志（loop 逐条追加）

格式：`<时间> <id> <结果> <commit> — <说明>`

2026-06-23 G01 ✅ — CI 触发加 鲲V1.1-dev(push+PR)、集成测试去 soft-fail；typecheck 硬化拆为 G09、覆盖率门槛拆为 G10
2026-06-23 F002+F006 ✅ — logging._add_tenant 捕获 MissingTenantContextError，防生产启动崩溃（+5 单测）
2026-06-23 F004 ✅ — 与 G01 重复（CI 已在 鲲V1.1-dev 触发）
2026-06-23 F003 ✅ — 关闭 prompt 自动执行代码块 RCE：NEVER_PROACTIVE 硬拦 python-exec/shell-exec(三层)+删触发器；改写/新增 7 测试
2026-06-23 F001 📐 — needs-design：门禁自批+覆盖人审是信任模型缺陷，写方案 docs/audit/proposals/F001.md（不在巨型文件里盲改）
