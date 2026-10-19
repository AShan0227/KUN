# KUN V4 release checklist

> 目标: 打 `v4.x.y` 正式 tag 前，把能在本地验证的风险先关掉。此清单不自动打 tag；自动门禁走 `kun ops release-check`。

## 0. 本地 release gate

```bash
uv run kun ops release-check --version v4.0.0
uv run kun ops release-check --version v4.0.0 --json
```

必须确认:

- [ ] `tag_name` 通过: tag 形如 `v4.0.0`、`v4.0.0-rc.1` 或 `v4.0.1-hotfix.1`。
- [ ] `git_worktree_clean` 通过: 正式打 tag 前没有未提交/未跟踪变更。
- [ ] `git_tag_available` 通过: 本地不存在同名 tag，禁止覆盖 tag。
- [ ] `alembic_local_single_head` 和 preflight 的 Alembic 检查都通过。
- [ ] `production_preflight` 通过: 配置、迁移、备份脚本、能力边界诚实性没有 blocker。
- [ ] `delivery_status_validation` 通过: 没有伪 ready。
- [ ] `legal_guard` 通过: LICENSE / NOTICE / public repo 风险文件还在。
- [ ] `backup_restore_prerequisites` 通过: 备份、restore smoke、本地演练脚本都存在。

本地预演可以加 `--allow-dirty`，但正式 release 禁止。

## 1. PR 收口

- [ ] PR #78 及依赖 PR 已合入目标 release 分支。
- [ ] `gh pr list --base main --state open` 只剩明确延期项。
- [ ] `uv run kun ops delivery-status --json` 没有 validation issue。
- [ ] `uv run kun ops delivery-status --fail-on-not-ready` 的失败项都被产品/ops 接受为 V4 后续，不被文案包装成 ready。

## 2. 测试和静态检查

```bash
uv run ruff check kun tests scripts alembic
uv run ruff format --check kun tests scripts alembic
uv run mypy kun
uv run pytest tests/unit -q
uv run kun ops release-check --version v4.0.0 --json
```

- [ ] GitHub Actions `lint` 通过。
- [ ] GitHub Actions `unit-tests` 通过。
- [ ] GitHub Actions `delivery-honesty` 通过。
- [ ] GitHub Actions `legal-guard` 通过。
- [ ] `mypy` 软失败项已人工 review，不影响 release 风险判断。

## 3. Backup / restore 前置检查

```bash
uv run python scripts/backup_restore_drill.py create --output-dir backups
uv run python scripts/backup_restore_drill.py restore-dry-run backups/kun-backup-drill-*.manifest.json \
  --restore-root /tmp/kun-restore-dry-run
scripts/backup_postgres.sh backups
KUN_RESTORE_TEST_DSN=postgresql://... scripts/restore_postgres_smoke.sh backups/kun-postgres-*.dump
```

- [ ] 本地备份演练 manifest 可校验。
- [ ] restore dry-run 没有覆盖风险。
- [ ] staging/production-like Postgres dump 可生成。
- [ ] restore smoke 在独立测试库成功。
- [ ] rollback 所需的上一个稳定 tag、镜像 digest、DB dump 路径已写入 release 记录。

## 4. Tag 流程

```bash
git switch main
git pull --ff-only origin main
uv run kun ops release-check --version v4.0.0
git tag -a v4.0.0 -m "KUN v4.0.0"
git push origin v4.0.0
gh release create v4.0.0 -F CHANGELOG-v4.0.md
```

- [ ] tag 只从干净 worktree 创建。
- [ ] tag message 写清版本号。
- [ ] GitHub release 使用已 review 的 changelog。
- [ ] 发布后记录 commit SHA、tag、镜像 digest、迁移 head、备份 manifest。

## 5. Rollback 流程

触发条件:

- 新版本启动失败或 readiness blocker 无法快速修复。
- 新版本引入数据写坏风险。
- 外部动作、鉴权、租户隔离、计费相关出现高风险回归。

操作:

```bash
git fetch --tags origin
git checkout v4.0.0
# 或部署平台选择上一版镜像 digest
uv run kun ops preflight --json
uv run kun ops release-check --version v4.0.0 --skip-alembic --json
```

- [ ] 先切回上一版应用镜像或上一稳定 tag。
- [ ] 如 migration 已写入不可逆数据，按 release 记录选择 restore smoke 通过的 dump。
- [ ] 回滚后跑 `uv run kun ops readiness --skip-alembic --json`。
- [ ] 记录 rollback reason、影响租户、恢复时间、是否需要 hotfix。

## 6. Hotfix 流程

命名:

- 分支: `hotfix/v4.0.1-<short-name>`
- tag: `v4.0.1-hotfix.1` 或正式补丁 `v4.0.1`

门禁:

```bash
uv run ruff check kun tests scripts alembic
uv run mypy kun
uv run pytest tests/unit -q
uv run kun ops release-check --version v4.0.1-hotfix.1
```

- [ ] hotfix 只包含修复所需最小改动。
- [ ] 新增或更新回归测试。
- [ ] rollback notes 写清是否需要 DB restore。
- [ ] tag 前 worktree 仍然必须干净。

## 7. 发布后观察

- [ ] `/health/ready` 正常。
- [ ] `uv run kun ops readiness --json` 无新增 blocker。
- [ ] NUO health report 没有新 critical/error。
- [ ] Grafana 看 API、outbox、DB、NATS、WorldGateway、Mission reaper 指标。
- [ ] 观察窗口内没有新增 rollback 触发条件。
