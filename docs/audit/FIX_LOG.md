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
2026-06-23 F027 ✅ — 长任务重复终结事件+非流式崩溃：过滤 LT 终结事件 + run() 守卫 result 键，+2 测试
2026-06-23 F013 ✅ — 人工验收否定句误判 accepted：加否定守卫→rework_required，+10 测试
2026-06-23 F034 ✅ — 方法论 loader 支持 dict 形字段(_listify 递归)，恢复 RSI 读侧丢失的 action，+4 测试
2026-06-23 F019 📐 — needs-design：FileControlPlaneStore O(N²) 写放大 + 静默丢未知字段，写方案 docs/audit/proposals/F019.md（持久层重设计，不硬改）
2026-06-23 F033 ✅ — 自指护栏覆盖 RSI 判定真实路径(external_supervisor/governance/watchtower)，+3 测试
2026-06-23 F035 ✅ — shell-exec 命令级策略守卫(默认拦灾难命令+env deny/allow)，python-exec 隔离+死 allowed_commands 拆 F035a，+6 测试
2026-06-23 F026 ✅ — bypass_rls 系统 session 不再强制 current_tenant()，修生产 outbox/NATS/GC 每 tick 崩溃，+4 测试
2026-06-23 F054 ✅ — EntityType 枚举↔DB CHECK 对齐为并集 + 迁移 0019 + 漂移守卫，+3 测试
2026-06-23 F046 ✅ — router 重试只针对传输错误，HTTP status 交 SDK(消除429放大/确定性400空转)+reraise，+8 测试
2026-06-23 F051 ✅ — 已由 G01 修复(集成测试去 soft-fail)，本轮核实
2026-06-23 F052 ✅ — CI 加 alembic check 步骤捕获 ORM↔迁移漂移(纯 CI 配置)
2026-06-23 F055 ✅ — 删除 4 个无 producer 死指标(避免空 series 假绿)，+5 守卫测试；再加回需带 emit→F055a
2026-06-23 F047 ✅ — decisions.md ADR-023 加实现状态修正(3硬约束未实现)
2026-06-23 F048 ✅ — 已由 F007 修真(auth 中间件接线，flag flip 生效)，本轮核实
2026-06-23 F049 ✅ — PROGRESS L6.E 降级[~]+修正注记(库就绪未接线)
2026-06-23 F050 ✅ — promotion sweeper 未调度：ADR-024/PROGRESS L5.2 如实修订(逻辑就绪未在生产调度)，接线归 RSI 主链方案
2026-06-23 F021/F022/F025/F039/F040/F041/F042 📐 — needs-design 合并方案 docs/audit/proposals/rsi-mainline-wiring.md（RSI 闭环生产化：逐环断点+最小接线顺序）
2026-06-23 F030/F031/F032 📐 — needs-design 合并方案 docs/audit/proposals/external-supervisor.md（外部监督落地：fail-close 守卫/独立进程/裁决强制力）
2026-06-23 F029 ✅ — NATS watchtower handler 加载真实规则集(缓存单例)，跨进程规则不再永不触发，+3 测试
2026-06-23 F012/F036/F037/F038/F053 📐 — needs-design 架构债合并方案 docs/audit/proposals/architecture-debt.md（平台/产品分离+治理链补 ADR+ORM CHECK 对齐）
2026-06-23 F010/F011/F014/F015 📐 — needs-design 并发/多副本一致性方案 docs/audit/proposals/concurrency-multi-replica.md（CAS/lease 续期/ledger 序列/单写者）
2026-06-23 F124 ✅ — finish_reason 映射(refusal→error/ctx→length)，失败不再伪装成功，+9 测试
2026-06-23 F092/F121/F136/F097/F138 ✅ — 核实为已修项的重复(F046/F024/F020/F055/F005)，标 done 引用
2026-06-23 F122 ✅ — temperature 拒绝模型集补全(opus-4-8/fable-5/mythos)，防误发 400，+8 测试
2026-06-23 F083 ✅ — activation 现按 (mission_id,version) 解析当前 plan（原来用 version 查 plan_id 字典恒 None），技能/外部 ref 匹配恢复，+3 测试
2026-06-23 F079 ✅ — mission_director._latest_gate 按 gate_evaluation_id(ULID 时序) 取最新，不再依赖 dict 插入序，+2 测试
2026-06-23 F135+F096 ✅ — TaskRow 补 complexity/priority_profile/estimated_steps 列+orchestrator 映射+迁移 0020，L1 Director 字段不再写库即丢，+2 测试
2026-06-23 F085 📐 — Redis 锁释放原子性并入 concurrency-multi-replica.md(离线无法忠实单测 TOCTOU，随 F010 带集成测试)
2026-06-23 F095 ✅ — Event.build subject 去重复 domain 段(kun.{tenant}.{event_type})，符合 ADR/模块约定；同步修正 4 处测试固化
2026-06-23 F091 📐 — StrategyExperiment.to_row_payload 字段缺失=runtime_experiments 缺列(实验环 orphan)，并入 rsi-mainline-wiring.md 第2/3环接通时补列+补payload
2026-06-23 F123 ✅ — CodexMcpProvider 工具示例改 JSON body，对齐 host 解析器 _CALL_RE，不再静默丢工具调用，+2 测试
2026-06-23 F094 ✅ — Automation APIAdapter 落实 timeout_sec+max_retries 契约(仅 transient 重试、业务失败不重试、注幂等前提)，+4 测试
2026-06-23 F062/F088 📐 — 进程内线程安全(daemon worker_pool>1)+checkpoint sequence 并入 concurrency-multi-replica.md(离线无法忠实复现线程时序，带并发测试落地)
