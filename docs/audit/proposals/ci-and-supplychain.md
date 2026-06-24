# CI 门禁强制力 + 供应链 · 合并方案（F110/F111/F119/F120/F133/F134/G10）

> 状态：needs-design（多为 CI/Docker/GitHub-设置/部署脚本改动，需在 CI 或构建环境验证，不在离线 unit-loop 内一次改绿）
> 同根 F118(CVE 依赖升级)已作为**真修复**单独落地(uv.lock，全套件绿)，不在本文件待办。

## 0. 根因

与全仓"orphan / 漂移"同源的治理面：**有 job 不等于有门禁，有引擎不等于有强制力**。
CI 跑了但不阻断、marker 定义了但没打、覆盖率/类型/分支保护无硬门槛——债无人拦就持续累积。
与 typecheck-debt(G09)同属「CI 门禁强制力」一个 epic。

## 1. 逐条现状（已核实，含已做对的部分）

| ID | 现状 | 修法 |
|----|------|------|
| **F133 / G10** | `ci.yml:47` unit job 仅 `pytest tests/unit -q`，**无** `--cov`/`--cov-fail-under`(grep cov 0 命中)→ 测试可空心化仍绿。基线覆盖率 82%。 | unit job 加 `--cov=kun --cov-fail-under=80`(留 2pt 缓冲)；与 G09 类型门禁、F134 一起翻硬门禁。 |
| **F134** | ~~`.github/` 下只有 `workflows/ci.yml`~~ **已部分落地(Loop-2 #7)**：(a) CI 触发**已含**活跃开发分支(`ci.yml on: push/pull_request branches:[main,"鲲V1.1-dev"]`，G01 已加)；(b) **已加** `.github/CODEOWNERS`(F134，单维护者 → @AShan0227，关键路径 kun/core、alembic、.github、docs/audit 分别声明 owner)。**残留(GitHub 设置项，代码不可核实，需人操作)**：在仓库 Settings 开启 branch protection + required status checks 清单 **lint / unit-tests(含 cov) / typecheck / integration-tests**，并启用 "require review from Code Owners"(CODEOWNERS 现已就绪可被它引用)。 | 残留项需人在 GitHub Settings 配置 required checks（上列 4 个 job）+ 启用 CODEOWNERS review。CI 触发与 CODEOWNERS 已 land。 |
| **F111** | `pyproject.toml` 定义了 integration/e2e marker + 开了 `--strict-markers`，但 `tests/integration/` 26 文件中 **20 个没打** `pytest.mark.integration`(含真依赖 PG 的 test_v7_xb_pg_check_constraints 等)。实测 `pytest -m "not integration and not e2e"` 仍选中 2152/2172——marker 隔离形同虚设(离线想跑"纯单测子集"做不到)。**注**：CI 本身用**目录**切分(unit job 跑 tests/unit、integration job 跑 tests/integration + PG/Redis/NATS + alembic check)是有效的，所以 CI 分层 OK；坏的是 marker 选择。 | 给 20 个 integration 文件加模块级 `pytestmark = pytest.mark.integration`；加一个守卫测试断言 `tests/integration/**` 每个文件都带该 marker(防再漂移)。机械但涉 ~20 文件，建议独立 PR。 |
| **F119** | `.env.example` 与代码实读的 KUN_ 变量严重脱节：代码读取但无文档的约 50 个，含 (a) 整个 **V7 特性开关族 ~23 个**(KUN_V7_TRIFECTA_*/ENSEMBLE_*/METHODOLOGY_*/AUDITOR_*)——RSI 闭环核心开关，新部署者无从知默认开关；(b) `KUN_AUTH_ENABLED/JWT_SECRET/TOKEN_TTL`(config.py:106-108)——安全关键却缺文档(直接关联 F007a/F066)。本审计已核实 13 个 config 字段(auth 三件+external_supervisor 六件+budget/task 调优四件)未出现在 .env.example。 | 从 `kun/core/config.py` 字段 + `grep -roE 'KUN_[A-Z_]+'` 重新生成 .env.example，逐项带注释与安全默认；auth 段显著标注"生产必开"。属真修复(纯示例文档、不碰运行时)，但需逐项核对默认值，建议独立 PR。 |
| **F120** | Dockerfile **已做对**：多阶段(builder/runtime)、非 root(`USER kun` uid1001)、HEALTHCHECK、.dockerignore、最小 apt。**残留两点**：(1) `:19` `uv sync --frozen ... \|\| uv sync ...` 的 fallback 在 uv.lock 与 pyproject 不一致时**静默重解析**→ 镜像失可重现性，且恰在锁过期时掩盖；(2) 运行时镜像只 COPY kun/rules/skills/alembic，**缺 `seeds/`**——`kun/context/seeds.py:49` 默认种子路径 `<repo>/seeds/context_assets.yaml` 缺失时只记 info 日志，静默降级。 | (1) 去掉 fallback,让锁不一致**硬失败**；(2) `COPY --chown=kun:kun seeds /app/seeds`。基础镜像 `python:3.13-slim` 可选 digest pin。需 docker build 验证,故归 needs-design。 |
| **F110** | `one_click_deploy.sh` README/脚本头建议 `curl ... \| bash`；脚本内 `curl -LsSf https://astral.sh/uv/install.sh \| sh`(:37) 无校验和;clone/pull 走 https 但**不固定 commit**(:45-48);随后 launchctl/systemctl **立即拉起常驻 daemon**(:59-95)。**注**:daemon 装机已被 F043 的 provider 预检兜住(无 provider 不装)。残留是供应链信任面。 | uv 安装改 pin 版本 + 校验和(或 vendored installer)；clone 固定到 release tag/commit；脚本头显著声明信任假设(只在信任 GitHub+astral 的前提下用)。或提供"下载-审阅-再执行"的两步式。 |

## 2. 排期 / epic

1. **「CI 门禁强制力」epic**（与 G09 typecheck-debt 同）：F133/G10 覆盖率门槛 + G09 类型门禁 + F134 分支保护/CODEOWNERS/开发分支 CI，一起从"软跑"翻成"硬门禁"。一次性把 lint/unit+cov/typecheck/integration 设为 required。
2. **F111 marker 修复**：独立小 PR（~20 文件加 marker + 守卫测试），解锁离线"纯单测子集"。
3. **F119 .env.example 重生成**：独立小 PR（真修复，纯文档）。
4. **F120 / F110 供应链加固**：去 Dockerfile fallback + 补 seeds/ + 部署脚本 pin/校验和，需 docker/CI 验证。

## 3. 覆盖 findings
F110, F111, F119, F120, F133, F134, G10（标 needs-design 指向本文件）。
F118（CVE 依赖升级）已作为真修复落地（uv.lock，全 unit 套件绿），见 FIX_LOG；与本文件同属供应链线但已闭环。
关联：G09(typecheck-debt，同 epic)、F007a/F066(F119 auth 文档)、F043(F110 daemon 预检)、F045(stub fail-closed)。
