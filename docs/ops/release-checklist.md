# KUN v2.2.0 release checklist

> 目的: 打 `v2.2.0` tag 前, 用一张表把风险关掉. 这份清单只准备流程, 不自动打 tag.

## 1. PR 收口

- [ ] BATCH9/10/11 全部 V2.2 PR 已 merge 到 `feat/v2.1-foundation`.
- [ ] 6 个 rebase 阻塞项已处理: #62 / #68 / #67 / #53 / #54 / #55.
- [ ] stacked PR 已按依赖顺序合并, 没有悬空 base branch.
- [ ] `gh pr list --base feat/v2.1-foundation --state open` 只剩明确推迟到 V2.3 的 PR.

## 2. 测试和静态检查

- [ ] GitHub Actions 五项全绿: ruff / mypy / unit / integration / starter skill license scan.
- [ ] 本地跑过:

```bash
uv run --extra dev ruff check .
uv run --extra dev ruff format --check kun tests alembic
uv run --extra dev mypy kun
uv run --extra dev pytest
```

- [ ] 测试数量达到当前基线: 1272+.

## 3. Dogfood 验证

- [ ] 跑过 dogfood script:

```bash
scripts/dogfood_v22.sh
```

- [ ] 跑过 CLI demo:

```bash
KUN_LAB_MODE=1 uv run kun lab dogfood --enable --report-path /tmp/kun-v22-dogfood.json
```

- [ ] 检查 `docs/ops/dogfood-checklist.md` 里的 V2.2 §20/§21/§22/§26/§27 验证项.
- [ ] dogfood 输出里没有新的 blocker, 只保留已知 V2.3 follow-up.

## 4. Changelog

- [ ] 生成 release notes:

```bash
uv run kun release notes --range v2.1.0..HEAD --output CHANGELOG-v2.2.md
```

- [ ] 人工 review `CHANGELOG-v2.2.md`: 分组准确, 没有把 WIP / revert / 内部噪声写成用户可见能力.
- [ ] 如需同步 PROMISES:

```bash
uv run kun promises generate --range v2.1.0..HEAD --title "v2.2.0 release sync" --write
```

## 5. Alembic 和数据迁移

- [ ] 比对 v2.1.0 到当前 head 的 migration 数量:

```bash
git diff --name-only v2.1.0..HEAD -- alembic/versions
```

- [ ] 确认只有一个 alembic head:

```bash
uv run alembic heads
```

- [ ] 本地或 staging 跑过:

```bash
uv run alembic upgrade head
```

- [ ] 新表的 RLS / tenant 过滤 / 索引都在 migration 和测试里覆盖.

## 6. 环境开关

- [ ] V2.2 wire 默认值已确认:
  - `KUN_VALUE_GATE_ENABLED`
  - `KUN_HERMES_ENABLED`
  - `KUN_LAB_BRIDGE_ENABLED`
  - `KUN_LAB_MODE`
  - `KUN_INPUT_TRANSLATOR_ENABLED`
- [ ] 生产默认不打开高成本实验流量, ENSEMBLE / lab / dogfood 有明确预算和 kill switch.
- [ ] LLM provider failover / billing / GitHub Actions quota 正常.

## 7. Tag 操作模板

```bash
git checkout feat/v2.1-foundation
git pull --ff-only origin feat/v2.1-foundation
uv run --extra dev ruff check .
uv run --extra dev ruff format --check kun tests alembic
uv run --extra dev mypy kun
uv run --extra dev pytest
uv run kun release notes --range v2.1.0..HEAD --output CHANGELOG-v2.2.md
git add CHANGELOG-v2.2.md docs/ops/release-checklist.md docs/v2/KUN-V2.2-revisions.md
git commit -m "docs(release): prepare v2.2.0 release"
git tag -a v2.2.0 -m "KUN v2.2.0"
git push origin feat/v2.1-foundation
git push origin v2.2.0
```

## 8. Tag 后确认

- [ ] GitHub release 页面有 `CHANGELOG-v2.2.md` 摘要.
- [ ] `docs/v2/KUN-V2.2-revisions.md` 已带实装状态.
- [ ] `docs/v2/V2.2-implementation-audit.md` 与 tag 内容一致.
- [ ] V2.3 讨论从 dogfood 数据出发, 不再从 spec 想象出发.
