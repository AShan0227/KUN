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

## 3. 排期
- 诚实横幅：本批即做(低风险)。
- PASS 判据加固 + 入 CI：依赖 RSI 主链接通(rsi-mainline-wiring epic)与 F008/F016/F017(去硬编码门禁分数)，同排期。

## 4. 覆盖 findings
F056, F057, F058, F059, F060（标 needs-design 指向本文件；关联 rsi-mainline-wiring.md）。
