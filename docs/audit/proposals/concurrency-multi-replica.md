# 并发 / 多副本一致性 · 合并方案（F010/F011/F014/F015，关联 F019）

> 状态：needs-design（跨进程一致性模型决策；本机无 Docker 跑不了并发集成测试，需带集成验证落地）

## 0. 根因

控制面状态以「进程内 dict + 镜像到单 JSON 快照文件(FileControlPlaneStore)」存储，**没有跨进程协调原语**
(版本号/CAS/序列/租约续期)。单 daemon 单 worker 路径靠「碰巧不撞」工作；官方文档却宣称支持多副本部署。
一旦多副本(多 daemon / daemon+API)并发，就出现误判超时双执行、last-writer-wins 丢更新、审计事件静默丢弃、
API 覆盖 daemon 新状态。这 4 条 + F019(单文件全量重写/丢字段)是同一根因。

## 1. 逐条现状（已核实）

| ID | 断点 | 问题 |
|----|------|------|
| **F010** | `daemon.py` `_claim_work_item_lease`(~3232)：`timeout = now + self.resource_lock_ttl` 仅在**领取时**设一次 | 长任务执行期间不续期 lease/心跳/资源锁 TTL。任务跑得比 TTL 久 → 锁过期 → 另一副本重新领取 → **同一 work item 双执行**。 |
| **F011** | `daemon.py`(~1156) 治理 pass：tick 起点 `refresh_from_store` 取内存快照 → 改 → 无条件 `put` 写回 | 多 daemon 副本各自从自己 tick 起点的快照改，最后写的覆盖先写的 → **last-writer-wins 丢更新**(治理状态、retire/restore 等)。 |
| **F014** | `runtime.py` `_record_ledger_event`(~2535)：ledger 序列/id 由进程内状态分配 | 多写者(API 进程 / 多 daemon)并发 append → 序列冲突 → **审计事件静默丢弃**(ledger 不是唯一真理源)。 |
| **F015** | `api/control_plane.py`(~176)：API 持有 `InMemoryControlPlane` 副本，从不 `refresh_from_store` | API 读到陈旧状态；API 写回时**整记录覆盖** daemon 刚写的新状态 → 双向丢更新。 |
| (关联)**F019** | `file_store.py`：单条 put 全量重读+重写、丢未知字段 | 见 docs/audit/proposals/F019.md。 |

## 2. 一致性方案选项

按"改动面 vs 正确性"权衡，建议组合：

1. **乐观锁 / 版本号 CAS（F011/F015 核心）**：每条记录加 `version`(或 `updated_at` 单调)。写回用
   "compare-and-set"：`UPDATE ... WHERE id=? AND version=?`(PG) 或文件 store 写前比对快照版本，version 不符
   → 重读合并重试，而非无条件 put。消灭 last-writer-wins。API 与 daemon 都走同一 CAS 路径(F015 顺带解决：
   API 不再持陈旧副本，按需 refresh + CAS 写)。
2. **租约续期心跳（F010）**：runner 执行期间起一个后台续期任务，每 `ttl/3` 调 `claim_work_item_lease`
   续 `timeout`(持 lease token 才能续，CAS 语义)。任务结束/崩溃停续 → 锁自然过期可被接管。配合"领取时校验
   lease 仍属本副本"防双执行。
3. **ledger 用 DB 序列 / append-only（F014）**：序列号交给 DB(`BIGSERIAL` / 序列)或改 append-only 写
   (唯一约束 (mission_id, seq) + 失败重取 seq 重试)，杜绝进程内分配冲突导致的静默丢弃。审计写失败必须
   报错/重试，绝不静默吞。
4. **单写者 owner 模型（最简，先上）**：若短期不想做全面 CAS，明确"control-plane 状态只有 daemon 单写者，
   API 只读(经 store refresh)"——API 的写改为发命令给 daemon 而非直接写。代价是 API 写延迟，但消灭并发覆盖。
   多 daemon 副本则用 worker_pool 的 claim 机制保证单副本拥有某 mission。

## 3. 最小落地顺序 + 验证

1. **加 `version` 字段 + CAS 写回**(F011/F015)：先给 mission/work_item 等高频写记录加版本，写路径改 CAS。
   - 验证：并发不变量测试——两个"副本"对同一记录交替读改写，断言无丢更新(最终值含两次变更或第二次 CAS 失败重试后合并)。
2. **lease 续期心跳**(F010)：runner 包一层续期。
   - 验证：模拟任务时长 > TTL，断言 lease 未被他人抢走、无双执行。
3. **ledger 序列加固**(F014)：DB 序列 / append-only + 写失败重试。
   - 验证：并发 append N 条，断言 N 条全部落库、序列唯一、零静默丢弃。
4. **API 单写者/只读**(F015)：API 写改命令化或强制 refresh+CAS。
   - 验证：API 与 daemon 并发改同一 mission，断言无覆盖。
5. **持久层**(F019)：脏标记防抖 / 分片，承载上面的 CAS 与 append-only。
- **注意**：以上验证多为并发集成测试，需 PG/真 store(本机无 Docker 跑不了)——必须在 CI(F052 已开 integration 作业)里跑。

## 4. 风险 / 排期
- 这是「把 demo 级单写者状态机升级为生产级多副本一致存储」的系统工程，建议作为一个 epic，与 F019(持久层重设计)
  同排期——CAS/版本号需要持久层支持。
- 在落地前，**生产应限制为单 daemon 单写者部署**(文档明确)，避免现状下多副本数据打架；不要在多副本下跑。
- 与持久层(F019)、ledger 作为 RSI 证据源(rsi-mainline-wiring 第 5 环 evidence_ledger)耦合。

## 5. 覆盖 findings
F010, F011, F014, F015（标 needs-design 指向本文件）；F019 见其自身方案。
