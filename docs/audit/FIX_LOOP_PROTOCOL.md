# 鲲 审查问题修复 Loop · 执行协议（每次迭代必读）

来源：全盘审查 `wf_b8ef4518-5bd`（160 条去重发现 + 8 元缺口）。
追踪源：`docs/audit/findings.json`（唯一真相，含 status/commit/notes）。
目标分支：`鲲V1.1-dev`，**直接 push**（用户授权，最高自主度）。

## 安全网现状（决定了所有铁律）
- CI 关着、本机无 Docker → **唯一验证手段是 `uv run pytest`（单测，约 1949 个，当前全绿）**。
- 因此：每条修复 **push 前必须跑单测全绿**；红了就回滚那一条，**绝不推红的提交**。

## 每次迭代做什么（处理 1 个 item，保持提交可 review、loop 可断点续跑）

1. **读** `docs/audit/findings.json`，按下面优先级选第一个 `status=pending` 的 item：
   1. `G*`（CI/流程元缺口）— **最优先**，先把安全网（CI）打开。
   2. `critical`
   3. `high` 且 `class=fix`
   4. `medium` 且 `class=fix`
   5. `low` 且 `class=fix`
   6. `class=architecture`（control_plane 拆分、RSI 接线、V6/V7 双轨裁决）— **最后**。
2. **读**该 item 引用的 `file` 及相关调用方/被调方，理解真实上下文（审查的 detail 可能有偏差，以代码为准）。
3. **改**：做最小正确修复。若改了行为，**补/扩一个真断言的测试**（禁止 assert True 凑数）。
4. **验**：`uv run pytest -q`。可先跑相关子集快速反馈，但 **commit 前必须跑完整单测套件**。
   - 集成测试（需 PG/NATS）本机跑不了 → 不作为门禁，但**不得让单测变红**。
5. **落地**：
   - 绿 → `git add -A` → `git commit -m "fix(audit/<id>): <简述>"`（结尾加 Co-Authored-By 行）→ `git push origin 鲲V1.1-dev`。把 item 置 `status=done`、记 `commit` sha。
   - 红且短时修不好 → `git checkout -- .`（回滚）→ 置 `status=blocked`，`notes` 写原因。换下一条。
6. **架构类（class=architecture）专门规则**：**禁止大爆炸重构**。要么走一个能单测变绿的**小增量步**，要么在 `docs/audit/proposals/<id>.md` 写设计方案并置 `status=needs-design`（不写代码）。一次只推进一小步。
7. **记账**：更新 `findings.json`（status/commit/notes）+ 追加一行到 `docs/audit/FIX_LOG.md`。追踪文件可与本次修复同一个 commit。

## 铁律（违反任何一条 = 停止并报告）
- ❌ 绝不为了变绿而删除/弱化/skip 任何已有测试。
- ❌ 绝不 push 红的提交；绝不 `--force` / 改写历史。
- ❌ 危险操作（鉴权、`shutil.rmtree`、删代码）修完必须全套单测仍绿。
- ❌ 若 `pytest` 本身跑不起来（环境坏）→ **立即停止**，报告，不 push。
- ✅ 一次一条，提交粒度小、信息清楚、可回滚。

## 停止条件
- 没有 `pending` item 了 → loop 结束，**不再续期**，输出总结。
- 连续 3 条都 blocked → 暂停，回报需要人介入的卡点。
