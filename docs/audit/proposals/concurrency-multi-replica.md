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
| **F085** | `work_item_governance.py` `RedisResourceLockStore.release_holder`(~668)：`get(key)`→比对 holder→`delete(key)` 非原子 | 锁过期后被新 holder 抢占，本陈旧 release 在 get/delete 间隙把**新 holder 的锁误删**。修法：Lua compare-and-delete（仅当 stored holder_id 仍等于本 holder 才 DEL），与 acquire 的 WATCH/MULTI 同族。**本机无 fakeredis/真 redis + 手写 FakeRedis 的 watch/multi 是 no-op，离线单测无法忠实复现 TOCTOU 窗口**，故并入本方案随 lease 续期(F010)一起带集成测试落地。 |
| **F062** | `daemon.py` `_run_prepared_work_items_in_parallel`：worker_pool>1 时 `ThreadPoolExecutor` 多线程对**同一个** `InMemoryControlPlane` 调 `start_work_item_run`/`finish_work_item_run`（写共享 dict），而别处 `_mission_work_items`(`runtime.py:2439` 等)正 `for item in self.work_items.values()` 迭代 | 进程内线程不安全：迭代中被改 → `RuntimeError: dict changed size during iteration`；更深是 read-modify-write 竞态丢更新。**单线程(worker_pool=1)默认路径无此问题**。修法二选一：①给 `InMemoryControlPlane` 状态访问加 `threading.RLock`；②把状态变更收回主线程、线程池只跑纯 `runner.run()`（单写者）。逐点 `list(...)` 快照只挡 size-change crash、挡不住数据竞态，且**离线无法忠实复现线程时序**——故并入本方案带并发测试落地。 |
| **F088** | `agents/executor/checkpoint.py` `TaskCheckpointService`：sequence 仅进程内单调 | 重启/多进程下产生重复 sequence，resume 取错 checkpoint。与 F014(ledger 序列)同族。修法：sequence 交 DB 序列，或 `(task_id, sequence)` 唯一约束 + 冲突重取。需真 DB 验证。 |
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
- 在落地前，**生产应限制为单 daemon 单写者 + worker_pool=1 部署**(文档明确)，避免现状下多副本/多线程数据打架；不要在多副本或 worker_pool>1 下跑。
- 与持久层(F019)、ledger 作为 RSI 证据源(rsi-mainline-wiring 第 5 环 evidence_ledger)耦合。
- F062 是**进程内**线程安全(单进程多线程共享一个 CP)，与 F010/F011/F015 的**跨进程**一致性是同族两面；建议同一 epic 内一并设计（进程内用 RLock/单写线程，跨进程用 CAS/版本号）。

## 4b. 资源膨胀 / 异常隔离 / 进程级一致性（F074/F075/F076/F077/F081/F093）

同一根因家族的另一面——单进程内存状态机在长跑/并发下的资源与隔离问题：

| ID | 断点（已核实） | 修法 |
|----|------|------|
| **F074** | `daemon.py` 每 tick 为每 mission 生成进度 artifact(~:1121)，**无清理**，叠加 file_store 全量 JSON 重写 → store 随时间无界膨胀 | artifact 加保留窗口/上限(只留最近 N 或按 TTL 清)；配合 F019 持久层防全量重写。 |
| **F075** | `daemon.py`(~:1373) 单任务批次路径 `finish_work_item_run` 抛异常会击穿整个守护循环；多任务批次有 try 隔离——两路异常隔离不一致 | 把多批次的 per-item try/except 隔离对齐到单批次路径，一个 work item 失败不杀守护循环。 |
| **F076** | `daemon.py` `claim_start`(:321，调用 :990) 抢占 daemon 槽位是 load→check→save 的 **TOCTOU**，无进程间锁 | 用原子 CAS（store 版本号，见本方案 §2.1）或文件锁/DB 唯一约束保证单副本拥有槽位。 |
| **F077** | `daemon.py`(~:4581) V7 Mission Director 周期 hook 每 tick 每 mission 起一个**未节流** `threading.Thread` + 独立 asyncpg engine | hook 用有界线程池(或 asyncio 任务)、复用单个 engine/连接池，按 mission 节流。 |
| **F081** | `runtime.py:2572` `ledger_refs` 每事件 `[*mission.ledger_refs, event.event_id]` **无界增长**，且每条 ledger 事件全 mission 重写 + 文件存储每 put 全文件读写 → 长任务 O(N²) | ledger_refs 不内联进 mission(改为按 mission_id 查 ledger 表)；append-only 写(见 §2.3 F014)；配合 F019。 |
| **F093** | `api/control_plane.py:176-183` V6 Control Plane 全内存 `InMemoryControlPlane` + 本地 JSON：无租户隔离、无跨进程一致性、daemon 状态可被任意覆写 | 落 DB(带 RLS 租户隔离) + 走 §2 的 CAS 写；与 F015(API 持陈旧副本)、F011 同一接线。 |
| **F147** | `kun/agents/supervisor/service.py:160-243` `SupervisorService.observe` 全程 `async with self._lock:`(:160) 跨多个 await(`_enrich_cluster_requests`:200、`_emitter`:209、`_safe_notify`:221——DB/通知 I/O)→ 监督线吞吐被**全局串行化**，一个慢 await 阻塞所有租户的观察处理。 | 缩小临界区：锁只护内存状态读改(`state.record`/计数)，await DB/通知**不持锁**；或按 tenant_id 用 per-key 锁，去掉单把全局锁。 |

> 这 7 条都随「demo 级内存状态机 → 生产级持久/一致存储」epic 一并解决；落地前生产仍应单 daemon + worker_pool=1。F147 的锁粒度可较早独立优化(不依赖持久层)。

## 5. 覆盖 findings
F010, F011, F014, F015, F062, F074, F075, F076, F077, F081, F085, F088, F093, F147（标 needs-design 指向本文件）；F019 见其自身方案。

> F085 落地说明：`release_holder` 改为 Lua compare-and-delete（atomic CAS-DEL）+ acquire 路径已有的 WATCH/MULTI 复用；与 F010 lease 续期同 PR，在 CI(真 redis) 下做并发释放不变量测试（断言陈旧 release 不删新 holder 的锁）。
