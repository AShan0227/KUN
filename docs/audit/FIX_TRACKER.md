# 鲲 审查问题修复 · 进度看板

来源：`wf_b8ef4518-5bd` ｜ 分支：`鲲V1.1-dev` ｜ 协议：[FIX_LOOP_PROTOCOL.md](FIX_LOOP_PROTOCOL.md)

**总计 172 项** — ✅done 18 ｜ ⛔blocked 0 ｜ 📐needs-design 6 ｜ ⬜pending 148
严重度：critical 6 / high 57 / medium 82 / low 19 / meta 8（另 54 低危暂缓）

| id | sev | class | 状态 | 标题 | 文件 |
|---|---|---|---|---|---|
| G01 | meta | process | ✅ | CI 门禁姿态从未被审计(最重要的元缺口): .github/workflows/ci.yml 中 typechec | `` |
| G02 | meta | process | ⬜ | 数据库 schema 与代码一致性(列级)未被验证: kun/core/orm.py 用 SQLAlchemy 声明 | `` |
| G03 | meta | process | ⬜ | 可观测性是半空壳: kun/core/metrics.py 定义 15 个 Prometheus 采集器,实测仅 6 | `` |
| G04 | meta | process | ⬜ | git 历史卫生与分支血缘混乱: 本机检出的是 鲲V1.1-dev,git log 显示 origin/main 反 | `` |
| G05 | meta | process | ⬜ | 前后端契约无防漂移机制: frontend/src/kunApiClient.ts(70 行)手写拼 URL(`${ | `` |
| G06 | meta | process | ⬜ | dogfood 脚本质量与诚实度: scripts/ 下有 11 个 dogfood_v9~v15 + e2e_rs | `` |
| G07 | meta | process | ⬜ | 错误预算/降级/背压未被审查: LLM 路由层已知有无差别重试×3 叠加 SDK 重试(上一轮报过),但更广的降级策 | `` |
| G08 | meta | process | ⬜ | 并发/幂等的端到端验证缺位: idempotency_keys 表 + control_plane/store.py | `` |
| F001 | critical | fix | 📐 | "外部监督+真人评审"门禁被同进程代码自批通过，且能覆盖人类的 fail 裁决 | `kun/control_plane/game_production.py:1791-1845` |
| F002 | critical | fix | ✅ | 生产环境下日志处理器必然抛 MissingTenantContextError，进程级核心流程(启动/后台 work | `kun/core/logging.py:26` |
| F003 | critical | fix | ✅ | 提示词中的 ```python``` 代码块被自动执行(python-exec)，可达 RCE 与密钥外泄 | `kun/engineering/config/proactive_triggers.yaml:5` |
| F004 | critical | fix | ✅ | 被审分支 鲲V1.1-dev 从未触发过 CI:门禁只认 main,而 main 已停更三周成废弃线 | `.github/workflows/ci.yml:4-7` |
| F005 | critical | fix | ✅ | 安全指标 tenant_cross_access_attempt 零 emit 且应用层无越权检测逻辑——越权告警永 | `kun/core/metrics.py:88` |
| F006 | critical | fix | ✅ | logging.py 的 _add_tenant 只捕获 LookupError，生产环境 MissingTenan | `kun/core/logging.py:20` |
| F007 | high | fix | ✅ | 生产 API 无任何鉴权：JWT 鉴权模块未接线，请求头 X-Tenant-Id/X-Scopes 被直接信任 | `kun/api/main.py:226-255` |
| F007a | high | fix | ⬜ | 生产环境 auth 关闭应 fail-closed（拒绝启动或拒绝请求） | `kun/core/config.py,` |
| F008 | high | fix | 📐 | 全部质量评分与门禁是硬编码常量+生成代码字符串指纹自检，RSI 评估信号为虚构 | `kun/control_plane/game_production.py:6653-6974,` |
| F009 | high | fix | 📐 | 7907 行单文件中约 60% 是单一游戏项目的内嵌源码/SVG/CSS/逐版本补丁，control plane 内 | `kun/control_plane/game_production.py:2913-6026,` |
| F010 | high | fix | ⬜ | 长任务运行期间不刷新 work-item 心跳/lease/资源锁 TTL，多副本部署下必然误判超时并重复执行 | `kun/control_plane/daemon.py:3238` |
| F011 | high | fix | ⬜ | 多进程共享 store 下大量治理方法用『tick 起点内存快照 + 无条件 put』写回，存在 last-writ | `kun/control_plane/daemon.py:1156` |
| F012 | high | architecture | ⬜ | 6927 行 god-module：通用控制面守护进程内嵌两个具体产品(游戏/RainFlow 广告)的业务剧本与文 | `kun/control_plane/daemon.py:5926` |
| F013 | high | fix | ✅ | 人工验收自由文本解析把否定句误判为 accepted，直接错误关闭任务 | `kun/control_plane/runtime.py:947-970` |
| F014 | high | fix | ⬜ | 多写者(API 进程/多 daemon 副本)下 ledger 序列冲突，审计事件被静默丢弃 | `kun/control_plane/runtime.py:2508-2542` |
| F015 | high | fix | ⬜ | API 进程持有从不刷新的控制面副本：读到陈旧状态、写回时整记录覆盖 daemon 新状态 | `kun/api/control_plane.py:176-185` |
| F016 | high | fix | 📐 | 质量门禁分数全为硬编码常量：finalize_mission 无条件自评 pass 并 ready_to_deliv | `kun/control_plane/kun_runtime_runner.py:338-368` |
| F017 | high | fix | 📐 | productization.py 用硬编码评估结果把外部行为信号直通 production 能力，RSI 闭环自证 | `kun/control_plane/productization.py:2351` |
| F018 | high | fix | ✅ | run_feature_activation_audit 对用户传入目录无条件 shutil.rmtree，存在数据 | `kun/control_plane/feature_activation_audit.py:21` |
| F019 | high | fix | 📐 | FileControlPlaneStore 每次单条写入都全量重读+全量重写快照，O(N²) 累积成本，且静默丢弃未 | `kun/control_plane/file_store.py:432` |
| F020 | high | fix | ✅ | tier3 验证聚合必崩：'ensemble' 不在 ValidatorKind Literal 中，Pydanti | `/Users/petrarain/鲲/kun/agents/tester/validation.` |
| F021 | high | fix | ⬜ | ADR-022 Layer4 防漂移闭环断链：Executor 自评 JSON 从不被解析，submit_self_ | `/Users/petrarain/鲲/kun/agents/exec_loop.py` |
| F022 | high | fix | ⬜ | 监督线 RSI 主链(Supervisor→Strategist→Gate)无生产事件源接线，只在 demo 脚本里 | `/Users/petrarain/鲲/kun/agents/supervisor/service` |
| F023 | high | fix | ✅ | JWT 鉴权 + RLS 子系统是死代码，生产 API/WS 完全信任客户端自报的租户与权限头 | `kun/api/main.py:226-255;` |
| F024 | high | fix | ✅ | Anthropic 定价表错误：Opus 4.7 高估 3 倍、Haiku 4.5 低估 4 倍，污染 ADR-00 | `kun/interface/llm/anthropic_provider.py:42-58` |
| F025 | high | fix | ⬜ | WS 长任务输入路由（ADR-022 Layer 3）永远不触发：task_state 没有任何填充路径 | `kun/api/ws.py:112-115,141,160;` |
| F026 | high | fix | ✅ | session_scope(bypass_rls=True) 仍强制要求租户上下文，生产环境 outbox/订阅者每 | `kun/core/db.py:114-123` |
| F027 | high | fix | ✅ | 长任务分支重复发出 answer 与 done 终结事件,并使非流式 run 崩溃 | `kun/engineering/orchestrator.py:1422` |
| F028 | high | fix | ⬜ | proactive 层在无审批且仅 cwd 沙箱下自动执行用户消息中的 python 代码块 | `kun/engineering/proactive_tools.py:250` |
| F029 | high | fix | ⬜ | Watchtower 跨进程路径评估的是一台零规则引擎 — NATS 链路上的所有规则永远不会触发 | `kun/core/nats_subscriber.py:80` |
| F030 | high | fix | ⬜ | ADR-023 '独立进程 + fail-close 三硬约束' 完全未落地 — 实际是同进程、默认关闭、失败静默跳 | `kun/external_supervisor/runner.py:45` |
| F031 | high | fix | ⬜ | Mode A / Mode B / 自嗨检测 / ExternalSupervisorPool 全是死代码, Gat | `kun/external_supervisor/modes.py:134` |
| F032 | high | fix | ⬜ | 监督裁决全程 advisory-only: alarming 只发事件, 无任何消费者会暂停/终止主线 | `kun/engineering/long_task_orchestrator.py:931` |
| F033 | high | fix | ✅ | is_self_referential 漏判真实实现路径 — 自指护栏可被 kun/external_supervi | `kun/governance/self_referential.py:43` |
| F034 | high | fix | ✅ | 方法论 runtime loader 与 seeds YAML schema 不匹配，28/33 条方法论的 act | `kun/engineering/methodology_runtime_loader.py:20` |
| F035 | high | fix | ✅ | shell-exec 与 python-exec 对 LLM 生成命令无命令级过滤；SkillManifest 的  | `kun/skills/builtin/shell_exec.py:46` |
| F035a | high | architecture | ⬜ | python-exec 进程/容器隔离 + 清理死的 SkillManifest.allowed_commands | `kun/skills/builtin/python_exec.py,` |
| F036 | high | architecture | ⬜ | ADR-020 钦定架构与实际代码根本背离：control_plane 应消失却翻倍至全包 50% | `/Users/petrarain/鲲/decisions.md:380` |
| F037 | high | architecture | ⬜ | game_production.py 膨胀根因：平台层硬编码特定游戏的产品代码补丁与交付物，非业务复杂度 | `/Users/petrarain/鲲/kun/control_plane/game_produc` |
| F038 | high | architecture | ⬜ | 架构治理链断裂：V6/V7 两代架构无任何 ADR，decisions.md/PROGRESS.md 已失效为权威文 | `/Users/petrarain/鲲/decisions.md:1076` |
| F039 | high | fix | ⬜ | RSI 第 1 环断电：Supervisor 异常检测引擎没有接入任何生产事件流 | `kun/agents/supervisor/service.py:154` |
| F040 | high | fix | ⬜ | RSI 第 3 环（安全实验）整体缺失：runtime_experiments 生产零读写，Executor 是 P | `kun/agents/executor/base.py:35` |
| F041 | high | fix | ⬜ | RSI 第 4/5 环半假：Gate 唯一生产调用方喂合成证据，runtime_capabilities 生产零写零 | `kun/integration/methodology_to_gate_bridge.py:10` |
| F042 | high | fix | ⬜ | 6 张数据脊柱表中 5 张 + evidence_ledger 生产零流动，仅 plan_reviews 真接通 | `alembic/versions/0011_rsi_data_spine.py:37` |
| F043 | high | fix | ⬜ | one_click_deploy.sh 在新机器上部署出的 launchd daemon 没有任何可用 LLM pr | `scripts/one_click_deploy.sh:52` |
| F044 | high | fix | ⬜ | AnthropicProvider 将 role="tool" 消息原样透传, 多轮工具循环必 400 | `kun/interface/llm/anthropic_provider.py:127-150` |
| F045 | high | fix | ⬜ | 生产 fallback 链兜底是 StubProvider, 失败时伪造成功响应(已造成事故) | `kun/interface/llm/router.py:716-748` |
| F046 | high | fix | ⬜ | 路由层对所有异常无差别重试 ×3, 叠加 SDK 重试放大 429/卡死/确定性 400 | `kun/interface/llm/router.py:572-574` |
| F047 | high | fix | ⬜ | [文档漂移] ADR-023 External Supervisor '3 条硬约束 fail-close' 纯属文 | `kun/core/config.py:71` |
| F048 | high | fix | ⬜ | [文档漂移] L6.AuthScaffold 'flag flip 即切生产 posture' 为假——JWT au | `kun/api/main.py:237` |
| F049 | high | fix | ⬜ | [文档漂移] L6.E 'Director.intent → Executor → AdapterRouter e2 | `kun/api/ws.py:46` |
| F050 | high | fix | ⬜ | [文档漂移] promotion_queue 超时 sweeper 无任何生产调度——'超时自动 expired + | `kun/governance/promotion_queue.py:173` |
| F051 | high | fix | ⬜ | integration-tests 软失败:集成层回归不阻断合并,核心流程退化无法被 CI 拦截 | `.github/workflows/ci.yml:91` |
| F052 | high | fix | ⬜ | 无 alembic check / autogenerate-diff 步骤:ORM 模型与迁移漂移不被拦截 | `.github/workflows/ci.yml:89` |
| F053 | high | architecture | ⬜ | 0011/0012 八张 RSI 表的 DB CHECK 约束在 ORM 完全缺失（系统性漂移） | `kun/core/orm.py:486` |
| F054 | high | fix | ✅ | EntityType 枚举与 capability_cards.entity_type CHECK 双向不一致 | `kun/datamodel/capability.py:22` |
| F055 | high | fix | ⬜ | 5 个 Prometheus 指标定义后全仓零引用——纯摆设，抓取时 series 根本不存在 | `kun/core/metrics.py:19` |
| F056 | high | fix | ⬜ | 全部 dogfood/e2e/smoke 脚本在 CI 之外，无人持续校验，却是 RSI/L5-L6 叙事的主要证据 | `.github/workflows/ci.yml:1-120` |
| F057 | high | fix | ⬜ | e2e_rsi_demo.py：RSI 全链中段 anomaly 数据写死 + Gate 输入手喂使其必过，链路自评 | `scripts/e2e_rsi_demo.py:194-248` |
| F058 | high | fix | ⬜ | dogfood_v14 / dogfood_v15：用 _StubLLM 跑 orchestrator，把'RSI  | `scripts/dogfood_v14_rsi_closed_loop_demo.py:127-` |
| F059 | high | fix | ⬜ | dogfood_v12 / v13：调真 Haiku 但 PASS 判据只看 TrifectaState==OK，而 | `scripts/dogfood_v13_orchestrator_trifecta_real_l` |
| F060 | high | fix | ⬜ | multi_dim_test.py：10 维能力'打分'本质是 grep 文件是否存在 + git log 计数 + | `scripts/multi_dim_test.py:96-412` |
| G09 | high | fix | ⬜ | 清理 152 个 mypy 错误后把 CI typecheck 设为硬门禁 | `kun/` |
| F061 | medium | fix | ⬜ | 同名函数重复定义，前一组被静默遮蔽成死代码且语义不同 | `kun/control_plane/daemon.py:5633` |
| F062 | medium | fix | ⬜ | worker_pool>1 时 runner 在线程池内无锁迭代共享 dict，与 finish 写入并发可抛 Ru | `kun/control_plane/runtime.py:1326-1341` |
| F063 | medium | fix | ⬜ | Context 资产层实际只有进程内内存实现：RedisAssetStore 从未接线，资产重启即丢、跨进程不一致， | `kun/context/storage.py:166-174` |
| F064 | medium | fix | ⬜ | CANARY→PRODUCTION 审批校验器写好了但从未接线, 生产路径仍是 '非空字符串即通过' 的 honor | `kun/governance/capability_lifecycle.py:196` |
| F065 | medium | fix | ⬜ | evidence_ledger 是空 stub: append 只打日志, get_trace 返回空列表, ADR | `kun/governance/evidence_ledger.py:51` |
| F066 | medium | fix | ⬜ | 零鉴权 + 租户/用户身份三处硬编码, 审批按钮可被任意访问者点击执行真实副作用 | `frontend/src/app/page.tsx:40` |
| F067 | medium | fix | ⬜ | layout 与 control-plane 页面使用的 kun-* 组件样式类在整个仓库 (含全部 git 历史) | `frontend/src/app/globals.css:1` |
| F068 | medium | fix | ⬜ | PG skip guard 字符串匹配错误，13 个约束测试在无 Docker 时失败而非跳过 | `tests/integration/test_v7_xb_pg_check_constraint` |
| F069 | medium | fix | ⬜ | PROGRESS.md 宣称 'L5 已达成=RSI 真闭合的标志' 与代码现实不符 | `PROGRESS.md:140` |
| F070 | medium | fix | ⬜ | 字符串补丁机制对生成代码逐字节耦合，anchor 漂移时静默跳过并可产出引用未定义变量的源码 | `kun/control_plane/game_production.py:5876-5908,` |
| F071 | medium | fix | ⬜ | _final_delivery 对整个项目目录（含 node_modules 与 .npm-cache）做两次递归全 | `kun/control_plane/game_production.py:2233-2243,` |
| F072 | medium | fix | ⬜ | _run_internal_tests 13 个 copy-paste 命令块，串行最多 ~17 个 npm 命令、 | `kun/control_plane/game_production.py:1285-1465` |
| F073 | medium | fix | ⬜ | tick 无异常隔离 + 用 float() 解析业务 workspace 的未校验 JSON，一条坏数据即可杀死常 | `kun/control_plane/daemon.py:5440` |
| F074 | medium | fix | ⬜ | 每 tick 为每个 mission 生成 2 个带时间戳的新 artifact 且无任何清理，叠加全量 JSON  | `kun/control_plane/daemon.py:4394` |
| F075 | medium | fix | ⬜ | 单任务批次与多任务批次的异常隔离不一致：单批次时 finish_work_item_run 异常会击穿整个守护循环 | `kun/control_plane/daemon.py:1373` |
| F076 | medium | fix | ⬜ | claim_start 抢占 daemon 槽位是 load→check→save 的 TOCTOU，无进程间锁 | `kun/control_plane/daemon.py:321` |
| F077 | medium | fix | ⬜ | V7 Mission Director 周期 hook 每 tick 每 mission 起一个未节流的线程 + 独 | `kun/control_plane/daemon.py:794` |
| F078 | medium | fix | ⬜ | 中英文子串匹配作为核心控制流，误匹配直接改变状态机走向 | `kun/control_plane/runtime.py:169-226` |
| F079 | medium | fix | ⬜ | mission_director._latest_gate 取 dict 迭代序最后一个，'最新门禁'判断不可靠 | `kun/control_plane/mission_director.py:498-507` |
| F080 | medium | fix | ⬜ | 确定性 gate_evaluation_id 跨重试复写历史评估，审计追溯失真 | `kun/control_plane/kun_runtime_runner.py:1537` |
| F081 | medium | fix | ⬜ | mission.ledger_refs 无界增长 + 每条 ledger 事件全 mission 重写 + 文件存储 | `kun/control_plane/runtime.py:2536-2541` |
| F082 | medium | fix | ⬜ | RainFlow/游戏生产域逻辑硬编码进'通用'控制面核心，违反分层并已三处复制 | `kun/control_plane/runtime.py:889-934` |
| F083 | medium | fix | ⬜ | activation.py 用 plan 版本号查按 plan_id 键控的字典，task_plan 恒为 None | `kun/control_plane/activation.py:56` |
| F084 | medium | fix | ⬜ | workspace_snapshot：含 .git/node_modules 的工作区 complete_resto | `kun/control_plane/workspace_snapshot.py:94` |
| F085 | medium | fix | ⬜ | RedisResourceLockStore.release_holder 非原子 get→delete，可能误删其 | `kun/control_plane/work_item_governance.py:668` |
| F086 | medium | fix | ⬜ | control_plane/__init__.py 急切导入全部 ~45k 行，含 2200 行测试夹具型审计套件混 | `kun/control_plane/__init__.py:90` |
| F087 | medium | fix | ⬜ | SupervisorPool fan-out 双发同一异常：维度实例不按维度过滤检查项，与自述'不互扰'矛盾；且 P | `/Users/petrarain/鲲/kun/agents/supervisor/pool.py` |
| F088 | medium | fix | ⬜ | TaskCheckpointService sequence 仅进程内单调，重启/多进程下产生重复 sequence | `/Users/petrarain/鲲/kun/agents/executor/checkpoin` |
| F089 | medium | fix | ⬜ | Gate 自指能力 enable 强门禁可被绕过：metadata_lookup 缺省即跳过检查，approval  | `/Users/petrarain/鲲/kun/agents/gate/service.py:44` |
| F090 | medium | fix | ⬜ | MultiJudge '多判官'实为同一模型同温度调 N 次，票相关性极高，多数票独立性假设不成立 | `/Users/petrarain/鲲/kun/agents/tester/multi_judge` |
| F091 | medium | fix | ⬜ | StrategyExperiment.to_row_payload 丢弃 requires_human_review | `/Users/petrarain/鲲/kun/agents/strategist/service` |
| F092 | medium | fix | ⬜ | _invoke_with_retry 无差别重试，叠加 SDK 内建重试与 CLI 长超时，最坏情况单次调用阻塞 2 | `kun/interface/llm/router.py:572-574` |
| F093 | medium | fix | ⬜ | V6 Control Plane 全内存 + 本地 JSON 文件持久化：无租户隔离、无跨进程一致性，daemon  | `kun/api/control_plane.py:176-214,363-372;` |
| F094 | medium | fix | ⬜ | Automation 层 Action.timeout_sec / max_retries 契约未实现；Shopif | `kun/interface/automation/api_base.py:25-96;` |
| F095 | medium | fix | ⬜ | Event.build 生成的 NATS subject 域名段重复，与文档约定不符且已被测试固化 | `kun/datamodel/events.py:104-105` |
| F096 | medium | fix | ⬜ | TaskMeta 的 L1 字段 complexity / priority_profile / estimated | `kun/datamodel/task.py:57-62` |
| F097 | medium | fix | ⬜ | 5 个 ADR-016 指标定义后从未被更新，其中含安全告警指标 tenant_cross_access_attem | `kun/core/metrics.py:19-92` |
| F098 | medium | architecture | ⬜ | ImportanceScorer(中央重要度打分器)无任何生产调用方，packer 另起炉灶用重复的词法打分 | `kun/context/importance.py:43` |
| F099 | medium | fix | ⬜ | L6 行业评测套件是孤儿且度量很浅,无法支撑真实在度量 | `kun/evaluation/industry_suite.py:164` |
| F100 | medium | fix | ⬜ | RCDH 诊断、PromotionTimeoutSweeper、promotion advance 均无生产接线 — | `kun/governance/rcdh.py:303` |
| F101 | medium | fix | ⬜ | ResourceQuota / ExplorationPenalty 从未注入生产 Strategist, 且为单进 | `kun/governance/resource_quota.py:86` |
| F102 | medium | fix | ⬜ | 红队套件只打 mock, 从未对准真实系统 — security/ 子系统整体是测试夹具 | `kun/cli.py:1876` |
| F103 | medium | fix | ⬜ | proactive_dispatch 的 Layer 1a 强制工具分支只标记 seen 从不真正 dispatch | `kun/engineering/proactive_tools.py:301` |
| F104 | medium | fix | ⬜ | PromptABService 直接调用 Strategist 私有方法 _emit_and_adjust，且整个模 | `kun/integration/prompt_ab.py:305` |
| F105 | medium | fix | ⬜ | 主工作区 WS 协议覆盖不全: 长任务双线交互的 5 种服务端消息类型被静默丢弃 (后端对应链路同样未接通) | `frontend/src/app/page.tsx:68-99` |
| F106 | medium | fix | ⬜ | 导航包含两个 404 死链 (/billing、/account), 用对象字面量 href 绕过 typedRou | `frontend/src/app/layout.tsx:34-42` |
| F107 | medium | fix | ⬜ | cockpit 与 control-plane 页面无请求取消/竞态防护, 每个输入框 keystroke 触发整组 | `frontend/src/app/cockpit/page.tsx:206-229` |
| F108 | medium | fix | ⬜ | 主工作区 WS 无重连机制, 消息数组无上限增长; 后端重启后 UI 永久停在 '未连接' | `frontend/src/app/page.tsx:51-66` |
| F109 | medium | fix | ⬜ | shell-exec/python-exec 沙箱仅为 cwd 目录边界且可被调用方 cwd 放大，无进程隔离 | `kun/skills/sandbox.py:43-63` |
| F110 | medium | fix | ⬜ | curl|bash 一键部署无完整性校验并安装常驻 daemon | `scripts/one_click_deploy.sh:5` |
| F111 | medium | fix | ⬜ | integration marker 覆盖率 6/26，marker 隔离机制形同虚设 | `tests/integration/` |
| F112 | medium | fix | ⬜ | 六组循环依赖靠延迟 import/TYPE_CHECKING 压制，engineering↔skills 为模块级硬 | `/Users/petrarain/鲲/kun/skills/calibration.py:39` |
| F113 | medium | fix | ⬜ | daemon.py 6,927 行：12 个类 5 类职责堆在单文件，属真实复杂度但缺模块边界 | `/Users/petrarain/鲲/kun/control_plane/daemon.py:1` |
| F114 | medium | fix | ⬜ | evaluation/ 446 行 L6 评测框架生产调用方为零，仅测试文件引用 | `/Users/petrarain/鲲/kun/evaluation/__init__.py:1` |
| F115 | medium | fix | ⬜ | External Supervisor（ADR-023）：service 真实但'独立进程'模式是占位，Mode A | `kun/external_supervisor/runner.py:45` |
| F116 | medium | fix | ⬜ | e2e_rsi_demo.py 是半剧本：链路中段与验证证据写死；fixture_only 强制标注未实装 | `scripts/e2e_rsi_demo.py:195` |
| F117 | medium | fix | ⬜ | RCDH 诊断引擎与 heavy-drift 后续动作未接线：rsi_trigger 只是落库字符串 | `kun/governance/rcdh.py:303` |
| F118 | medium | fix | ⬜ | 锁定的运行时依赖含 7 个已知 CVE(starlette/urllib3/idna/mako) | `uv.lock:1` |
| F119 | medium | fix | ⬜ | .env.example 与代码实际读取的环境变量严重脱节(约 50 个未文档化) | `.env.example:1` |
| F120 | medium | fix | ⬜ | Dockerfile: --frozen 失败时静默回退到非锁定安装 + 镜像缺 seeds/ 目录 | `Dockerfile:19` |
| F121 | medium | fix | ⬜ | 定价表过时: Opus 4.7 高估 3 倍、Haiku 4.5 低估 4 倍, cache 写入未计费 | `kun/interface/llm/anthropic_provider.py:42-58` |
| F122 | medium | fix | ⬜ | temperature 修复用硬编码子串黑名单, opus-4-8/后续模型不覆盖且静默丢参 | `kun/interface/llm/anthropic_provider.py:151-154` |
| F123 | medium | fix | ⬜ | CodexMcpProvider 工具调用示例与解析器格式矛盾 → bad_json 静默丢工具调用 | `kun/interface/llm/codex_mcp_provider.py:462-465` |
| F124 | medium | fix | ⬜ | finish_reason 把 refusal/上下文超限折叠为 "stop", 失败被当成功完成 | `kun/interface/llm/anthropic_provider.py:201-203` |
| F125 | medium | fix | ⬜ | [文档漂移] ADR-023 Mode A 'NATS 订阅 + 独立进程持续监管' 实为 stub runner  | `kun/external_supervisor/runner.py:44` |
| F126 | medium | fix | ⬜ | [文档漂移] ADR-019 '生产模式不允许 query 参数租户' 为假——WS 闸门仅是 opt-in 环境变 | `kun/api/ws.py:88` |
| F127 | medium | fix | ⬜ | [文档漂移] RCDH 诊断链路（含 narrow_scope 护栏）在生产中无人调用——DiagnosticRun | `kun/agents/supervisor/service.py:104` |
| F128 | medium | fix | ⬜ | [文档漂移] ADR-022 宣称新加的 7 个事件类型一个 producer 都没有 | `decisions.md:644` |
| F129 | medium | fix | ⬜ | [文档漂移] ADR-020 '7 个 agent 角色' 已变成 11 个实目录，新增 4 个角色无任何 ADR  | `decisions.md:338` |
| F130 | medium | fix | ⬜ | [文档漂移] ADR-020 'control_plane / brain / engineering/orches | `decisions.md:380` |
| F131 | medium | fix | ⬜ | [文档漂移] PROGRESS.md 自 2026-05-27 停更，落后 78 个 commit，进度叙事双向失真 | `PROGRESS.md:234` |
| F132 | medium | fix | ⬜ | [文档漂移] ADR-024 '新建 kun/governance/rsi_loop.py 编排 10 步' 已删除 | `decisions.md:956` |
| F133 | medium | fix | ⬜ | unit-tests 无覆盖率门槛:测试可空心化而流水线仍绿 | `.github/workflows/ci.yml:47` |
| F134 | medium | fix | ⬜ | 无 branch protection / required checks 配置、无 CODEOWNERS:门禁强制 | `.github/workflows/ci.yml:13-124` |
| F135 | medium | fix | ⬜ | TaskMeta.complexity / priority_profile / estimated_steps 写 | `kun/core/orm.py:75` |
| F136 | medium | fix | ⬜ | ValidatorKind 枚举不含运行时产出的 'ensemble' | `kun/agents/tester/validation.py:343` |
| F137 | medium | architecture | ⬜ | CI 从不跑 alembic check，且无 DB 时无法运行 → 漂移长期无人发现 | `alembic/env.py:26` |
| F138 | medium | fix | ⬜ | /metrics 端点导出默认 registry，未 emit 的 series 在抓取时不出现 | `kun/api/main.py:261` |
| F139 | medium | fix | ⬜ | v7_xb_smoke.py：用 StubProvider + 内存 fake session 跑一遍即 print | `scripts/v7_xb_smoke.py:296-372` |
| F140 | medium | fix | ⬜ | dogfood_v10：lifecycle/auditor 表增长依赖手喂的'必过'GateService 输入；M | `scripts/dogfood_v10_trigger_xb_tables.py:104-131` |
| F141 | medium | fix | ⬜ | spark_world_run.py 未提交改动坦承 claude CLI OAuth 路径曾'silent stu | `scripts/spark_world_run.py:49-60` |
| G10 | medium | fix | ⬜ | CI unit-tests 加覆盖率门槛(--cov-fail-under) | `.github/workflows/ci.yml,` |
| F142 | low | architecture | ⬜ | 双运行时并存：control_plane 栈与 agents 7 角色栈模块级零耦合，Supervisor/Miss | `/Users/petrarain/鲲/kun/control_plane/kun_runtime` |
| F143 | low | fix | ⬜ | record_plan_change 绕过状态机校验直接改 mission.status | `kun/control_plane/runtime.py:1203-1213` |
| F144 | low | fix | ⬜ | frontier50_external：默认 workdir 硬编码他人机器绝对路径，can_run 用 "ab"  | `kun/control_plane/frontier50_external.py:34` |
| F145 | low | fix | ⬜ | cockpit._current_plan 用字典序比较计划版本，v10 < v9，多次改版后驾驶舱显示错误计划 | `kun/control_plane/cockpit.py:322` |
| F146 | low | fix | ⬜ | Strategist 配额与探索惩罚硬编码 tenant_id='default'，多租户限流/惩罚失效 | `/Users/petrarain/鲲/kun/agents/strategist/service` |
| F147 | low | fix | ⬜ | SupervisorService.observe 全程持单把全局锁跨 await DB/通知调用，监督线吞吐被串行 | `/Users/petrarain/鲲/kun/agents/supervisor/service` |
| F148 | low | fix | ⬜ | Cockpit 端点用 query 参数 tenant_id（默认 "default"）绕过租户中间件，discip | `kun/api/cockpit.py:282-296,328-339;` |
| F149 | low | fix | ⬜ | 平台 browser adapter 的读操作仅导航即返回 status=ok，假成功污染 AdapterRoute | `kun/interface/automation/shopify/browser.py:161-` |
| F150 | low | fix | ⬜ | EntityType 枚举与 capability_cards DB CHECK 约束双向不一致(company v | `kun/datamodel/capability.py:22-28` |
| F151 | low | fix | ⬜ | External Supervisor 自身可被提示词注入 + 解析兜底 fail-open + LLM 自由文本可 | `kun/external_supervisor/service.py:207` |
| F152 | low | fix | ⬜ | 技能注册表与 watchtower 规则用 cwd 相对默认路径，非 repo-root 启动时静默不加载 | `kun/skills/loader.py:162` |
| F153 | low | fix | ⬜ | /cockpit 页面的 API 路径未配置 rewrite, 同源部署下整页必然 404; dev 端口 3001 | `frontend/next.config.mjs:7-11` |
| F154 | low | fix | ⬜ | 单测非密闭: mission_director 单测真连 localhost:55432，错误被静默吞掉 | `kun/control_plane/mission_director.py:135-148` |
| F155 | low | fix | ⬜ | 5 个有生产调用方的模块在 unit+integration 双套件下覆盖率为 0% | `kun/governance/evidence_ledger.py:1` |
| F156 | low | fix | ⬜ | spark_world_run.py 注释与代码互相矛盾, env 操作埋下回切陷阱 | `scripts/spark_world_run.py:49-60` |
| F157 | low | fix | ⬜ | bug_root_cause_cases 唯一性约束：ORM 用 UniqueConstraint，迁移用 uniq | `kun/core/orm.py:708` |
| F158 | low | fix | ⬜ | events 子系统 emit 在生产 outbox worker 路径（确认为真产数，非测试） | `kun/core/events.py:160` |
| F159 | low | fix | ⬜ | watchtower 与 task 生命周期 emit 在生产路径（确认为真产数） | `kun/engineering/orchestrator.py:683` |
| F160 | low | fix | ⬜ | 依赖真 PG/LLM 的脚本在 Docker 停机时无优雅降级，且无任何脚本对'是否真跑了真实依赖'留下可审计的运行 | `scripts/dogfood_v9.py:133-167` |
