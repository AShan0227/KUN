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
2026-06-23 F126/F132 ✅ — decisions.md 就地诚实订正(ADR-019 query 租户 opt-in / ADR-024 rsi_loop.py 不存在)
2026-06-23 F128/F129/F130 📐 — ADR-020/022 治理链漂移枚举并入 architecture-debt.md F038(追溯补 ADR)
2026-06-23 F125/F131 ✅ — decisions.md ADR-023 Mode A stub 注记 + PROGRESS.md 陈旧横幅(以 git log/docs/audit 为准)
2026-06-23 F127/F100/F117 📐 — RCDH 引擎就绪零接线并入 rsi-mainline-wiring.md
2026-06-23 F068 ✅ — PG check-constraint 测试 skip-guard 扩为 _is_pg_unavailable(类型+11子串)，无 Docker 时 13 项全 SKIP 不再 FAIL
2026-06-23 F150 ✅ — 核实为 F054 重复(EntityType vs capability_cards CHECK)，标 done 引用
2026-06-23 F145 ✅ — cockpit._current_plan 计划版本改数值排序(v10>v9)，+3 测试
2026-06-23 F157 ✅ — bug_root_cause_cases ORM 改 unique Index 对齐迁移 0013，消除 alembic check 漂移，+2 测试
2026-06-23 F061 ✅ — 删 daemon.py 被遮蔽的重复死函数(_ready/_has_ready_current_plan_product_work 前组)，零行为变更，全套件绿
2026-06-24 F080 ✅ — runtime gate_evaluation_id 纳入输出 content_hash，重试不再复写历史评估；+4 测试，10 处现有测试改 subject_ref 定位(保留断言)
2026-06-24 F073 ✅ — daemon gate 证据 JSON 的 float() 解析改 _safe_float(保守默认/不抛)，坏数据不再杀死 tick，+9 测试
2026-06-24 F144 ✅ — frontier50 workdir 改 env 可配置 + can_run 'ab' 改词边界匹配，消除误触+硬编码他人路径，+2 组测试
2026-06-24 F143 ✅ — record_plan_change 状态变更走 assert_transition_allowed(消除状态机绕过)，零行为变更，+6 测试
2026-06-24 F152 ✅ — skills/watchtower 默认路径缺失时回退 repo-root 锚定，非 repo-root 启动不再静默零加载，+3 测试
2026-06-24 F154 ✅ — mission_director V6 单测关闭 V7 桥(autouse)，不再后台连 55432，单测密闭化，断言不变
2026-06-24 F149 ✅ — shopify browser 读操作区分真成功/降级(page_confirmed)，加载失败→failed、成功标 navigation_only，+4 测试
2026-06-24 F146 ✅ — strategist 配额/探索惩罚透传真实 tenant(替代硬编码 default)，多租户限流生效，+2 测试
2026-06-24 F156 ✅ — spark_world_run 矛盾注释对齐实际 Codex-pin 决策(纯注释，零行为变更)
2026-06-24 F158/F159 ✅ — 正向确认 11 个指标为生产真产数(非缺陷)，加 live-metrics 守护测试防误删(补 F055 删死指标的反向守护)
2026-06-24 F155 ✅ — evidence_ledger 补契约单测(零覆盖→有覆盖)，notifications/idempotency_gc 因需 DB 留 CI；接线属 RSI 方案
2026-06-24 F044 ✅ — AnthropicProvider role=tool→tool_result block(按 claude-api 权威格式)，多轮工具循环不再 400，+5 测试
2026-06-24 F028 ✅ — 核实为 F003 重复(proactive 自动执行 python 代码块 RCE)，NEVER_PROACTIVE 已覆盖，标 done 引用
2026-06-24 F045 ✅ — StubProvider 生产(KUN_ENV=production)下 fail-closed，不再伪造成功响应，+4 测试
2026-06-24 F056-F060 📐 — 演示脚本诚实化方案 docs/audit/proposals/demo-script-honesty.md + 6 脚本头加 FIXTURE/DEMO 横幅(不删/不改逻辑)
2026-06-24 F007a ✅ — 生产 auth 关闭 fail-closed(KUN_ENV=production+auth off→拒启动)，+3 测试
2026-06-24 F043 ✅ — one_click_deploy 加 provider 预检(缺凭据拒装 daemon，可显式覆盖)，bash -n + 行为冒烟通过
2026-06-24 G09 📐 — mypy 类型债方案 docs/audit/proposals/typecheck-debt.md(实测145错/42文件，5批清理→CI硬门禁)
2026-06-24 F089 📐 — Gate enable_capability 自指绕过：函数 orphan + 无 token 验证机制，并入 rsi-mainline-wiring.md(接通时 fail-closed+真 token 验证)
2026-06-24 F066/F067/F105/F106/F107/F108 📐 — 前端缺口合并方案 docs/audit/proposals/frontend-gaps.md(鉴权/样式/WS协议+重连/死链/竞态)
2026-06-24 F074/F075/F076/F077/F081/F093 📐 — 并发/资源簇并入 concurrency-multi-replica.md §4b(膨胀/隔离/进程级一致性)
2026-06-24 F063/F064/F065/F099/F101/F103/F104/F114 📐 — 孤儿/接线簇并入 rsi-mainline-wiring.md §2b；F115 并入 external-supervisor.md
2026-06-24 F070/F071/F072/F082/F084/F086/F112/F113 📐 — 架构/分层簇并入 architecture-debt.md §1b(实现质量+模块化债)
2026-06-24 F118 ✅ 16af265 — 升级 4 个传递依赖避开 7 个已知 CVE(starlette/urllib3/idna/mako),全套件绿
2026-06-24 F110/F111/F119/F120/F133/F134/G10 📐 — 新建 ci-and-supplychain.md(CI 门禁强制力+供应链)；F139/F140/F141 📐 并入 demo-script-honesty.md §1b
2026-06-24 F069 ✅ — PROGRESS.md L5 'RSI 真闭合已达成' 诚实化为'代码路径存在、主链未接通'(引 rsi-mainline-wiring)
2026-06-24 F102/F109/F148/F151 📐 — 新建 security-posture.md;F078 📐 并入 architecture-debt §1b;F116/F160 📐 并入 demo-script-honesty §1b
2026-06-24 F087 📐 rsi-mainline §2b;F090 📐 rsi-mainline §2b(判官独立性,模型约束);F147 📐 concurrency §4b(锁粒度);F153 📐 frontend-gaps(/cockpit rewrite);F055a ⏸ deferred(特性未做占位)
2026-06-24 收尾核销：F137 ✅(stale,=F052 已加 ci alembic check);F142 📐(=F036)/F098 📐/F035a 📐 并入 architecture-debt/security-posture;G02-G08 ✅(roll-up 同 G01)。验证 workflow wf_cde3c00e-0e0：92 needs-design 全部有方案引用，0 silent gap。
2026-06-24 [Loop-2 #1] F035a 诚实子集 ✅ — 删 SkillManifest 3 死字段 + 订正 shell-exec 描述(非 per-skill allowlist) + 回归测试；隔离半/wire-vs-strip 仍 needs-design
2026-06-24 [Loop-2 #2] F119 ✅ — .env.example 补全 ~50 个未文档化变量(V7 开关族/auth/external-supervisor/budget/quota/codex/skill-exec)+auth 生产标注+覆盖测试
2026-06-24 [Loop-2 #3] F098 去重子集 ✅ — _terms 抽到 kun/context/text_terms.py 单一源(importance+packer 共用)+回归测试；ImportanceScorer 接线 vs 删除仍 needs-design(按孤儿处置=接线非删)
2026-06-24 [Loop-2 #4] F111 ✅ — 20 个 integration 文件补 integration/e2e marker(含 7 个 asyncio-only 改 list)+静态守卫测试;-m 'not integration and not e2e' 现真正隔离离线单测子集
2026-06-24 [Loop-2 #5] F053 ✅ — 8 个 ORM Row 镜像 0011/0012 共 23 条 CHECK 到 __table_args__ + 解析式漂移守卫测试;命名约定前缀差异诚实注记(rename 迁移留后续)
2026-06-24 [Loop-2 #6] F133+G10 ✅ — ci.yml unit job 加覆盖率硬门禁 --cov-fail-under=80(本机实测 83%,本机等价命令 RC=0 验证)
2026-06-24 [Loop-2 #7] F134 ✅ — 加 .github/CODEOWNERS;CI 触发分支已含 鲲V1.1-dev(过时点);branch-protection/required-checks 记入 ci-and-supplychain 需人 GitHub 设置
2026-06-24 [Loop-2 #8] G09 批1 ✅(部分) — 删 12 unused-ignore + 补 17 type-arg,mypy 145→116,无新错误类型;批2-5 仍 needs-design(typecheck-debt.md)
2026-06-24 [Loop-2 #9] F147 ✅ — SupervisorService.observe 锁粒度收窄(I/O 移出锁,dedup 仍锁内记录保证不重复 emit)+并发单测(锁释放/IO 重叠)
2026-06-24 [Loop-2 #10] F153 ✅ — next.config.mjs 加 /cockpit rewrite(node --check 通过)+.env.example CORS 多端口注释(不改后端默认);残留需人目检 dev 栈
2026-06-24 [Loop-2 #11] F067 ✅(TIER-1 收官) — globals.css 定义全部 25 个 kun-* 类(tailwindcss 编译 RC=0 验证)+静态守卫测试;残留纯审美目检
2026-06-24 [Loop-2 TIER-2 #1] ADR-027 持久层重设计草案 📝 needs-review — drafts/ADR-027-persistence-redesign.md(F019 决议:DB+CAS+append-only ledger+迁移路径+分步验证)
2026-06-24 [Loop-2 TIER-2 #2] ADR-028 control_plane 去留裁决草案 📝 needs-review — drafts/ADR-028(A/B/C 选项+推荐 C 混合域化+分步 import-linter 验证;F038/F036)
2026-06-24 [Loop-2 TIER-2 #3] RSI 主链接线分步落地草案 📝 needs-review — drafts/RSI-mainline-wiring-plan.md(前置 ADR-027/028;逐环 wired 验收断言;§2b 旁路组件接入点;F090 去相关靠多模型/多视角(claude-api:temperature 现模型已移除);影子→canary;诚信红线;M1-M5 里程碑)
2026-06-24 [Loop-2 TIER-2 #4] 外部监督 enforce 草案 📝 needs-review — drafts/external-supervisor-enforce-plan.md(F030/F031/F032/F115/F151;含抗注入硬化与 advisory→enforce 灰度)
2026-06-24 [Loop-2 TIER-2 #5] 真沙箱隔离草案 📝 needs-review — drafts/sandbox-isolation-plan.md(F109/F035a 隔离半;子进程降权→nsjail/容器分阶段+能力白名单+逃逸 PoC 验收)
