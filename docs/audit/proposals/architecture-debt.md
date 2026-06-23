# 架构债 · 合并方案（F012/F036/F037/F038/F053）

> 状态：needs-design（拆分/裁决/补 ADR 都是需要人拍板的架构决策，不在 loop 内自动改）

## 0. 根因

`kun/control_plane/` 在 ADR-020「钦定它应消失」之后反而翻倍成全包 ~50%，把通用平台引擎和
两个具体产品（Scribblenauts 风格游戏 / RainFlow 广告）的硬编码剧本焊在一起；同时 V6/V7 两代
架构没有任何 ADR 背书，权威文档链事实断裂。5 条 finding 是「平台层与产品层未分离 + 治理链失修」
的同一根因。

## 1. 现状（已核实）

| ID | 事实 |
|----|------|
| **F036** | `kun/control_plane/` 45,313 行 / 41 文件，占 `kun/` 包 ~50%；ADR-020(accepted) 明文要求它在 L1 重组后**消失**、功能散到五层目录。`agents/`(7 角色栈, 9.5k 行) 与 `control_plane/` **模块级 import 为零**，只靠 2 处函数内延迟 import 桥接——平台同时养着两套互不相认的运行时(V6 control_plane vs ADR-020 agents)。 |
| **F012** | `kun/control_plane/daemon.py` 6,927 行 god-module：12 个类、~20 个治理 pass、worker 池、watchtower 桥堆在单文件，且内嵌游戏/广告两个产品的硬编码剧本 + 中英文关键词启发式。 |
| **F037** | `kun/control_plane/game_production.py` 7,907 行，约 60% 是单一游戏的内嵌 TS/React 源码 + SVG/CSS + 17 个按版本号命名的 `_patch_app_for_product_gamefeel_v32~v55` 字面 string-replace 补丁——平台层焊死了一条 mission 的重放日志。 |
| **F038** | `decisions.md` 实际止于 ADR-026(grep `^## ADR-` 确认)，其后两周的 V7-PHASE-X 全部工作无 ADR；V7 现行规范是 `docs/v7/KUN-V7.md`，但 README 权威文档清单与 PROGRESS 均无一字提及；V6/V7(Mission Director、trifecta、Control Plane 复活)无任何 ADR 留痕，权威链断裂。`PROGRESS.md` 停更落后约 78 commit(已部分由 F049/F050 注记修正)。 |
| **F053** | 迁移 0011/0012 为 8 张 RSI/checkpoint 表创建了 ~20 条 DB CHECK 约束(promotion_state/rollout_mode/status/sampling_rate/target_level/priority/triggered_by/sequence… 见 `0011_rsi_data_spine.py`、`0012_task_checkpoints.py`)，但对应 ORM Row 类(`orm.py:486-657`)**未声明**这些 CHECK——ORM↔迁移系统性漂移。 |

## 1b. game_production 实现质量 + 分层/模块化债（F070/F071/F072/F082/F084/F086/F112/F113）

同一根因的另一面——平台/产品未分离带来的实现质量与模块化债（已核实）：

| ID | 现状（已核实） | 修法 |
|----|------|------|
| **F070** | `game_production.py:712/791/832` 等用 `_patch_app_for_*` 脆弱**字符串 replace** 改 TS/React 源码（与 F037 的 v32~v55 补丁同处），源码一变补丁即失配 | 改 AST 变换 / 模板渲染；产品源码作为资产模板而非平台代码内嵌字符串。随 F037 域化一并迁出。 |
| **F071** | `game_production.py:2331` `_hash_path(spec.project_path)` 对整个项目目录算 SHA，含 `node_modules` 等易变目录 → 缓存恒失效或误判 | hash 时排除 `node_modules`/构建产物/`.git`，只对源码 + lockfile 算指纹。 |
| **F072** | npm 安装/测试命令散落多处（`:1259` install、`:3730` test 串、`:7592` 内嵌脚本）复制粘贴，易漂移 | 收敛为单一 `_run_npm(cmd, cwd)` 帮助函数，命令集中定义。 |
| **F082** | 领域/策略逻辑落在 `kun/core/`（如 `scoring.py`、`quota_tracker.py` 等含业务规则），违反 ADR-020「core 只放跨层通用基建」 | 把带产品/策略语义的逻辑上移到对应层（governance / domains），core 仅留 config/db/ids/logging/metrics/tenancy 这类通用件。需人裁分层归属。 |
| **F084** | `workspace_snapshot.py:74/113` `create_workspace_snapshot` 用 `shutil.copy2` **全量拷贝**、无增量、无 ignore 规则（会连 node_modules/构建产物一起拷） | 加 ignore 规则（复用 F071 的排除集）+ 增量/硬链接快照；与 F074 artifact 膨胀同源。 |
| **F086** | `game_production.py` 顶层 30 条 import，模块顶层有重副作用 import，拖慢启动且难测 | 重副作用 import 改惰性（函数内 / lazy），顶层只留纯定义。 |
| **F112** | 出现循环依赖迹象：`kun/core/events.py:28`、`nats_subscriber.py:26` 用 `TYPE_CHECKING` 守卫 + 函数内延迟 import 规避 import cycle（与 F036 agents↔control_plane 仅靠函数内延迟 import 桥接同构） | 理清依赖方向（core 不应反向依赖上层）；拆出共享类型模块打破环，而非靠延迟 import 掩盖。 |
| **F113** | **同 F012**：`daemon.py` 6,930 行 god-module（本轮复核行数），多职责（引擎/治理/worker/产品剧本）堆一处 | 见 F012 处理方向——按「引擎 / 治理插件 / 产品 playbook」三层拆分。此处仅作 F012 的再确认条目。 |

> F070/F071/F072/F084/F086 随 F037 game_production 域化迁出一并落地；F082/F112 随 F036 分层裁决落地；F113 即 F012。落地前不应宣称 game_production 是"通用平台能力"。

## 2. 处理方向

### F036 + F012 + F037 · 平台/产品分离（核心）
- **裁决先行**：补一份 ADR 明确 control_plane 的去向——是按 ADR-020 拆散到五层，还是正式承认 V6
  control_plane 为一等公民并更新 ADR-020。**先有裁决，再动代码**(否则又是双轨)。
- **迁出业务 runner**：`docs/CONTROL_PLANE_DOMAINS.md` 已有 `kun/domains/` 方案。把 game_production、
  rainflow_ad_mission 等**产品 runner + 内嵌源码/补丁**迁出平台层到 `kun/domains/<product>/`；平台层只保留
  通用 runner 协议 + 调度。game_production 的 v32~v55 补丁应折叠成产品仓内的资产，不再是平台代码。
- **拆 daemon**：按「引擎 / 治理插件 / 产品 playbook」三层拆 `daemon.py`，删被遮蔽的重复函数(F011 修复时已点到)。
- **接线 agents↔control_plane** 或明确二选一：终结「两套运行时」——要么让 V6 control_plane 调 ADR-020 agents，
  要么文档明确 control_plane 是 agents 的上层编排，消除「零 import」的架构割裂。
- 与 F001/F008/F009(game_production 门禁/RSI 信号)强耦合，建议作为「control_plane 域化」一个 epic 同排期。

### F038 · 修治理链
- 给 V6/V7 关键架构变化(Control Plane 复活、Mission Director、trifecta、Qi/Nuo agent 化)**补 ADR**(追溯式，标注 accepted 日期)。
- README 权威文档清单补上 `docs/v7/KUN-V7.md`；把 PROGRESS/decisions/PROGRESS_VS_PLAN/PROMISES 四套口径**对账或归档**(后两者已失效)。
- 制度化：把 X.Q 的 production-entry diff check 扩展为「每个新组件强制声明生产入口 + 对应 ADR」，防止再次漂移(呼应全仓反复出现的 orphan 模式)。

#### 具体漂移实例（审计核实，需追溯式 ADR / 对账）
这些是 F038「治理链断裂」的可枚举实例，建议在补 ADR 时逐条交代：

| ID | 漂移 | 现状（已核实） | 处理 |
|----|------|---------|------|
| **F129** | ADR-020 称「7 个 agent 角色」 | `kun/agents/` 实有 11 个角色目录(director/executor/external_supervisor/gate/mission_director/nuo/qi/strategist/supervisor/tester/trifecta)。原文带「（按需扩展）」软化，但 mission_director/external_supervisor/trifecta 等新角色无任何 ADR 留痕。 | 追溯 ADR 交代新增角色 + 更新 ADR-020 角色清单(或改为「≥7，见 agents/ 目录」)。 |
| **F130** | ADR-020 模块路径(control_plane/brain/engineering/orchestrator) | 五层目录规划与实际包结构漂移(control_plane 反而膨胀，见 F036)。 | 与 control_plane 去留裁决 ADR 一并更新模块路径表。 |
| **F128** | ADR-022 新增的事件类型 producer 缺失 | ADR-022 声称的若干 anti-drift 事件类型在生产无 producer(emit 端缺失)，属「类型已声明、信号零流动」。与 RSI 主链 orphan 同构(rsi-mainline-wiring)。 | 接通 anti-drift 信号(rsi-mainline 第 6 步防漂移消费端)时补 producer，或在 ADR-022 注记「类型预留、producer 待接」。 |

> 注：F126(ADR-019 WS query 租户表述)、F132(ADR-024 rsi_loop.py 已删)已直接在 `decisions.md` 就地做诚实订正(opt-in flag / 文件不存在)，不在本表——它们是单点可订正的事实，无需追溯 ADR。

### F053 · ORM↔迁移 CHECK 对齐（机械但需逐表核对 + 全套件验证）
- **可机械修，但不在本 loop 一次盲改**：把 0011/0012 的每条 CHECK 逐条镜像进对应 ORM Row 的 `__table_args__`
  (条件字符串与迁移完全一致)，加一个像 F054 那样的漂移守卫测试(ORM CHECK 集合 == 迁移 CHECK 集合)。
- **风险**：若某个 create_all-based 单测插入了违反新 CHECK 的数据，会变红——所以必须以「全 unit 套件绿」为门禁，
  逐表加、跑全套件、红则回滚该表。建议作为一个专注 PR(8 表)，而非顺手改。
- 与 F052(已加 `alembic check`)、F054(已对齐 EntityType)同属 schema-faithfulness 线。

## 3. 排期建议
1. 先做 **F038 的裁决 ADR**(control_plane 留/拆) + 补 V6/V7 ADR —— 解锁后续一切。
2. 再做 **F053**(独立 PR，低风险高确定性，全套件门禁)。
3. 最后做 **F036/F012/F037 的域化拆分**(大 epic，与 F001/F008/F009 一起)。

## 4. 覆盖 findings
F012, F036, F037, F038, F053, F070, F071, F072, F082, F084, F086, F112, F113, F128, F129, F130（标 needs-design 指向本文件）。
F070/F071/F072/F082/F084/F086/F112/F113 见 §1b（随 F036/F037 域化与分层裁决一并落地；F113 即 F012）。
F126、F132 已在 decisions.md 就地诚实订正(done)，此处仅备注关联。
