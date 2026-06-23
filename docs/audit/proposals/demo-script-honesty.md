# 演示脚本诚实化 · 合并方案（F056/F057/F058/F059/F060）

> 状态：needs-design（"真验证"依赖 RSI 主链接通——见 rsi-mainline-wiring.md；本方案管"别再用假证据冒充真验证"）

## 0. 根因

dogfood / e2e / smoke 脚本是项目对外宣称「RSI 闭环已达成 / L5-L6 已实现」的**主要证据来源**，
但它们普遍用 `StubProvider` / `_StubLLM`、写死 anomaly 数据、手喂"必过"Gate 输入，PASS 判据只看
浅层 flag——**自证、且无人在 CI 持续校验**。这与 rsi-mainline-wiring 揭示的「RSI 环生产未接线」
是同一事实的两面：因为真链路没通，演示只能靠 fixture 摆拍。

## 1. 逐脚本 theater 点（已核实）

| ID | 脚本 | theater 点 |
|----|------|-----------|
| **F056** | 全部 dogfood/e2e/smoke (`scripts/*`) | 都在 CI 之外、无人持续跑，却是 RSI/L5-L6 叙事主要证据。CI(`.github/workflows/ci.yml`)不跑它们。 |
| **F057** | `scripts/e2e_rsi_demo.py` | 链路中段 anomaly 数据写死、Gate 输入手喂使其必过、验证证据写死；脚本头已自承 FIXTURE-ONLY(半诚实)。 |
| **F058** | `scripts/dogfood_v14_rsi_closed_loop_demo.py` / `dogfood_v15_all_8_mechanisms_on.py` | 用 `_StubLLM` 跑 orchestrator，把"RSI 闭环已达成"坐实为 PASS——stub 下的闭环不是真闭环。 |
| **F059** | `scripts/dogfood_v12_real_trifecta_checkpoint_collab.py` / `dogfood_v13_orchestrator_trifecta_real_llm.py` | 调真 Haiku，但 PASS 判据只看 `TrifectaState==OK`，而 OK 仅= hook 跑过、未校验产出质量。 |
| **F060** | `scripts/multi_dim_test.py` | 10 维能力"打分"本质是 grep 文件是否存在 + git log 计数 + 读 /tmp 临时日志——不是行为验证。 |

## 2. 处理方向

1. **诚实标注（已随本批做）**：给每个脚本头加横幅注释——「FIXTURE/DEMO：用 stub/写死输入，
   PASS 不代表真实 RSI 闭环验证，详见本方案」。不删脚本、不改逻辑(它们对手动 wiring 调试仍有价值)。
2. **PASS 判据基于真实证据**：F059 的 `TrifectaState==OK` 应附加产出质量断言；F058 的闭环 PASS 必须
   在**非 stub** provider 下跑；F060 的"能力分"要换成真实行为验证(真跑一次 + 断言产出)，而非文件存在性。
3. **接 CI 或显式隔离（F056）**：要么把**真**端到端用例(用真 provider、真 DB)纳入 CI(F052 已开 integration 作业)
   并作为 L5-L6 的权威证据；要么把纯 fixture 脚本明确归类为"手动调试工具、非验证"，从对外叙事里移除。
4. **真验证的前提是 RSI 主链接通**：在 rsi-mainline-wiring 的 1→9 步接通前，任何"RSI 闭环已达成"的
   声明都应保持 F047/F049/F050 那样的如实标注；演示脚本不能替代真链路验证。

## 1b. 第二批 theater 点（F139/F140/F141，已核实）

审计 round-2 又点出三处同根的"假证据冒充真验证"，处理同上(诚实化措辞 / 真证据判据)：

| ID | 脚本 | theater 点 | 修法 |
|----|------|-----------|------|
| **F139** | `scripts/v7_xb_smoke.py:296-372` | 脚本头**已**诚实标注 SCOPE(stub provider + in-memory fake session)——这点优于他者；但结论措辞越界：6 段全过后 `print('全部 6 块 X.B 真在 runtime 走通. 软件层完成度 100%')`(L21,360-363)，且 Segment6 用两个 StubProvider 调 ensemble、断言只看 `EnsembleCallRow` 被 add + `n_providers_total==2`——把"代码能调通"偷换成"功能完成 100%"。 | 把"软件层完成度 100%"改为"6 段代码路径在 stub 下可调通(非功能完成、非真 LLM/PG 验证)"；ensemble 段加"stub provider，不构成 ensemble 行为验证"。一行措辞级真修复。 |
| **F140** | `scripts/dogfood_v10_trigger_xb_tables.py:104-131,287-289` | row-delta 框架本身真实(真 SELECT count 前后快照)，但被削弱：(1) Step1 `GateService.admit` 输入 `pass_rate=0.97/evidence_quality=0.92` 是手填**必过**值→ +1 行是"喂必过参数触发桥"而非真实裁决；(2) Step2 MissionDirector 桥是 fire-and-forget 线程 + `await sleep(3.0)` 探测落库→时序竞态，慢机可能漏 row、PARTIAL 仍可能退出码 0。 | 注明 Step1 是"桥连通性"非"裁决质量"验证；Step2 改为对落库的确定性等待(轮询/事件)而非固定 sleep，PARTIAL 必须非零退出。 |
| **F141** | `scripts/spark_world_run.py:49-60` | 工作树对该 runner 的**未提交**改动注释直书历史事故：claude CLI OAuth subprocess `hung 180s ×3 → RetryError → silent stub fallback that echoed the prompt (a no-op masquerading as success)`——"调 stub 却当成功"反模式的当事人书面确认；同一改动把 `max_budget_usd` 提到 1000.0。 | 这是 F045(stub 生产 fail-closed)的真实事故印证：runner 的 provider 失败必须 fail-closed(报错/退出非零)，**绝不**静默回退 echo-prompt stub；未提交改动应作为线索纳入 F045 接线验证，不要把 stub 回退当兜底。 |

> 这三条与 F056-F060 同处理：F139 是一行措辞级真修复(可随手做)；F140/F141 涉及判据/控制流与 F045 接线，随 RSI 主链 + F045 落地。

## 3. 排期
- 诚实横幅：本批即做(低风险)。
- PASS 判据加固 + 入 CI：依赖 RSI 主链接通(rsi-mainline-wiring epic)与 F008/F016/F017(去硬编码门禁分数)，同排期。

## 4. 覆盖 findings
F056, F057, F058, F059, F060, F139, F140, F141（标 needs-design 指向本文件；关联 rsi-mainline-wiring.md、F045）。F139/F140/F141 见 §1b。
