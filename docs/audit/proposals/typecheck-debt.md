# 类型检查债 · 方案（G09，关联 G10/F133/F134）

> 状态：needs-design（145 个 mypy 错分布在 42 文件，无法在 fix-loop 内一次安全清完；本方案给分批清理顺序 + 落地后把 CI typecheck 设硬门禁）

## 0. 现状（`uv run mypy kun`，2026-06-24 实测）

**145 errors / 42 files（checked 265）**。CI(`.github/workflows/ci.yml`)目前 typecheck 为软跑(不阻断)，
所以类型错误持续累积、无门禁。审计 G09 原计为 152，当前 145（部分随本轮修复消解）。

### 按 error-code 分布
| 数量 | code | 性质 |
|----|------|------|
| 31 | `arg-type` | 实参类型不匹配 —— **可能藏真 bug**，需逐个看 |
| 21 | `attr-defined` | 访问不存在属性 —— 可能真 bug 或缺 stub |
| 18 | `no-untyped-def` | 函数缺注解 —— 机械补 |
| 17 | `type-arg` | 泛型缺参数(`dict` → `dict[str, X]`) —— 机械补 |
| 12 | `unused-ignore` | 过时 `# type: ignore` —— **删即可，零风险** |
| 10 | `no-any-return` | 返回 Any —— 收紧返回注解 |
| 8 | `dict-item` | dict 字面量值类型不符 |
| 7 | `misc` / 5 `call-overload` / 4 `prop-decorator` / 4 `assignment` / 2 `union-attr` / 2 `comparison-overlap` / 1 `type-var` / 1 `no-untyped-call` | 杂项，逐个看 |

### 热点文件（前几名占比高）
| errs | 文件 |
|----|------|
| 27 | `kun/control_plane/game_production.py` |
| 27 | `kun/cli.py` |
| 10 | `kun/governance/engineering_discipline.py` |
| 9 | `kun/control_plane/feature_activation_audit.py` |
| 7 | `kun/control_plane/daemon.py` |
| 5 | `kun/control_plane/mission_director.py` |

> 两个热点(game_production + cli)占 54/145 ≈ 37%。

## 1. 分批清理顺序（每批独立 PR、跑全套件 + mypy 增量校验）

1. **批 1 · 零风险机械（~30 项）**：删 12 个 `unused-ignore` + 补 17 个 `type-arg` 泛型参数。
   纯机械、不改运行时行为；先清掉噪声，让后续真问题更显眼。
2. **批 2 · 补注解（~28 项）**：`no-untyped-def`(18) + `no-any-return`(10)。给函数/返回值加注解，
   过程中可能逼出隐藏的类型不一致——逐个确认不是真 bug。
3. **批 3 · 热点专项**：集中清 `cli.py` 与 `game_production.py`(各 27)。这两个文件占 37%，单独 PR 收益最大。
4. **批 4 · 语义类（需谨慎，可能是真 bug）**：`arg-type`(31) + `attr-defined`(21) + `call-overload`(5) +
   `union-attr`(2) + `comparison-overlap`(2)。这些往往不是"加注解"能消的——是实际调用/属性访问不符，
   **修这些时优先怀疑真 bug**，配单测验证，不要用 `# type: ignore` 掩盖。
5. **批 5 · 收口剩余** + 全项目 `uv run mypy kun` 归零。

## 2. 落地后：CI 硬门禁
- 批 1-5 清零后，把 `ci.yml` 的 typecheck 从软跑改为**硬门禁**(mypy 非 0 退出即 fail)，防回潮。
- 与 G10/F133(unit 覆盖率 `--cov-fail-under`)、F134(branch protection / required checks)一起，
  作为「CI 门禁强制力」一个 epic 落地——光有 job 不阻断＝没有门禁。

## 3. 风险 / 排期
- **不要用 `# type: ignore` 批量压**：那只是把债换个地方藏；批 4 的语义错尤其要当 bug 排查。
- 建议每批一个 PR、CI 增量跑 mypy(只校验改动文件不回潮)，全清后再翻硬门禁。
- 与全仓「orphan / 漂移」根因一致：缺门禁 → 债无人拦。X.Q production-entry check 可扩展为"类型 + 覆盖率 + 入口"三合一门禁。

## 4. 覆盖 findings
G09（标 needs-design 指向本文件）；关联 G10/F133（覆盖率门槛）、F134（branch protection）。
