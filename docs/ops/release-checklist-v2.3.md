# KUN v2.3.0 release checklist

> 目的: 打 `v2.3.0` tag 前, 用一张表把风险关掉. 这份清单只准备流程, 不自动打 tag.

## 1. PR 收口

- [x] BATCH13 PR #73 merged (commit 3a41096).
- [x] Claude C64+C68 (Pheromone decay step + dogfood_v23.sh) merged (commit 08e3ade).
- [x] Claude C71+C72+C73+qi CLI merged (commit e2ebe04).
- [ ] `gh pr list --base feat/v2.1-foundation --state open` 只剩明确推迟到 V2.4 的 PR.

## 2. 测试和静态检查

- [x] 本地跑过:

```bash
uv run --extra dev ruff check .                    # ✅ All checks passed!
uv run --extra dev ruff format --check kun tests alembic   # ✅
uv run --extra dev mypy kun                        # ✅ 195 source files, 0 error
uv run --extra dev pytest tests/unit               # ✅ 1453 passed
```

- [x] 测试数量达到 V2.3 基线: 1453+ (V2.2 是 1302).
- [ ] GitHub Actions 五项全绿: ruff / mypy / unit / integration / starter skill license scan.

## 3. Dogfood 验证

- [ ] 跑过 V2.3 dogfood script (需真 LLM API key):

```bash
./scripts/dogfood_v23.sh
```

- [ ] 看 logs 有以下 event 出现:
  - `protocol.applied` (orchestrator 真消费协议成功)
  - `gaming.detected` (AntiGaming hit, 偶尔可能没)
  - `pheromone_decay` (idle_batch step 跑完)
  - `pc_error` 或 PC hook 相关 (Predictive Coding hook 触发)
- [ ] V2.3 spec 默认 OFF 的功能, 用户能 opt-in 启用:
  - KUN_QI_RUNTIME_ENABLED=1 + KUN_QI_ENABLED=1 → 启 runtime 装上
  - KUN_PROTOCOL_CONSUME_ENABLED=1 → orchestrator 真消费协议
  - KUN_ANTI_GAMING_ENABLED=1 → 反作弊真接

## 4. Grafana dashboard

- [ ] V2.3 dashboard 正常: `KUN / V2.3 启 (Qi)`
- [ ] 9 panel 都有数据 (跑 dogfood 后):
  - 启窗口 active gauge
  - 启今日花费 USD
  - protocol match rate
  - protocol lifecycle 升级
  - PC error p50/p95
  - Pheromone 总强度
  - Pheromone decay step rate
  - AntiGaming 套路命中
  - CapabilityCardCache hit rate

## 5. Changelog

- [x] CHANGELOG-v2.3.md 写好 (本仓库根目录)
- [ ] `uv run kun release notes --range v2.2.0..HEAD --output CHANGELOG-v2.3-autogen.md` 跑一遍验证

## 6. Migration 验证

- [x] alembic single head: 0016_pheromone
- [ ] 在 staging DB 跑 `uv run alembic upgrade head` 无报错
- [ ] downgrade 一下确认双向工作: `uv run alembic downgrade -1` → upgrade head

## 7. 最终 tag

- [ ] git tag v2.3.0
- [ ] git push origin v2.3.0
- [ ] gh release create v2.3.0 -F CHANGELOG-v2.3.md

## 已知 V2.4 follow-up

- Predictive Coding 自动训练 cron (启窗口)
- Darwin Gödel 自动 strategy generation (LLM 生)
- AntiGaming 自学新套路 (基于用户 👎 反馈)
- Verification 模板自动生成 (基于 dogfood 数据)
- 协议自动 promote (基于 win_rate 阈值)
- 多窗口 + 自动选窗口

详见 `docs/v2/KUN-V2.4-spec.md`.
