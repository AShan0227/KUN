# KUN V4 Release Checklist

这份清单只做一件事：防止“能不能发版”靠感觉。正式打 tag 前，先跑机器检查，再人工确认。

## 1. Release Gate

```bash
uv run kun ops release-check --tag v4.0.0 --require-ready
```

内部测试版如果仍有 partial 能力，可以先不加 `--require-ready`，但对外说明必须继续诚实。

## 2. Backup / Restore

打 tag 前必须完成：

- `uv run kun ops preflight`
- `scripts/backup_postgres.sh` 在目标环境可用
- `scripts/restore_postgres_smoke.sh` 在目标环境可用
- `uv run python scripts/backup_restore_drill.py --dry-run`

没有真实数据库/S3 restore 演练时，只能发内部测试版，不能宣称生产级完成。

## 2.5 Auth / Signup

如果要开放邀请码注册，必须确认：

- `KUN_SELF_SIGNUP_ENABLED=true`
- `KUN_SELF_SIGNUP_INVITE_CODE` 已设置，且不写进仓库
- `KUN_AUTH_SECRET` 或 `KUN_AUTH_SECRETS` 已设置 32+ 字符真实密钥
- 注册入口只承诺“邀请码注册 + refresh session”，不能宣传成密码登录 / OAuth / 设备风控

不开放注册时，保持 `KUN_SELF_SIGNUP_ENABLED=false`。

## 2.6 Secret Store

本地测试可用 file-backed secret store：

```bash
export KUN_SECRET_STORE_FILE=.kun/secrets.json
uv run kun ops secret-store-set --tenant <tenant> --name KUN_WORLD_SMTP_HOST --value <smtp-host>
```

这个工具只允许写 `KUN_WORLD_*`，并且不会回显密钥值。它不是云 KMS；生产环境仍要补托管 Secret Manager、轮换和审计。

发布前建议额外跑：

```bash
uv run kun ops dogfood --tenant <tenant> --include-db-account
```

这会验证账号账本、token 使用账本、refresh session、成员邀请、一次性接受 token
和接受邀请写库链路；它仍不会发送邀请邮件。

## 3. Tag

```bash
git status --short
uv run kun ops release-check --tag v4.0.0 --require-ready
git tag -a v4.0.0 -m "KUN v4.0.0"
git push origin v4.0.0
```

## 4. Rollback

发现 blocker 后：

```bash
git checkout <last-good-tag>
uv run kun ops preflight --no-fail-on-blocker
uv run kun ops readiness --no-fail-on-blocker
```

如果涉及数据库迁移，先确认 downgrade 路径和备份，再回滚应用。不能在没有备份的情况下直接降库。

## 5. Hotfix

热修流程：

```bash
git checkout -b hotfix/<short-name> <last-good-tag>
# 修复 + 测试
uv run kun ops release-check --tag v4.0.1 --require-ready
git tag -a v4.0.1 -m "KUN v4.0.1 hotfix"
git push origin hotfix/<short-name>
git push origin v4.0.1
```

## 6. Legal / IP

公开仓库发版前必须通过：

```bash
uv run python scripts/check_legal_guard.py
```

不要把未公开商业方案、客户信息、密钥、投资材料、内部 GTM 细节放进 public repo。
