# 安全姿态 · 合并方案（F102/F109/F148/F151）

> 状态：needs-design（红队真打、沙箱真隔离、租户强绑定、监督抗注入都需要超出 unit-loop 的环境/架构改动）

## 0. 根因

安全机制存在"形" но缺"实"：红队套件只打 mock、沙箱只做路径边界、租户作用域可由调用方 query 参数指定、
外部监督自身可被提示注入且解析 fail-open。与全仓"引擎就绪但未真接线"同构——安全控制写了，但没有真正的对抗面。

## 1. 逐条现状（已核实）

| ID | 现状 | 修法 |
|----|------|------|
| **F102** | `kun/cli.py:1876` red-team 套件用 `_mock(case)` 作 `system_invoke`——返回写死字符串("APPROVED: ..." 或"拒绝：红队 mock 拦截")，`run_red_team_suite` 从不对准真实系统；`security/` 子系统整体是测试夹具。pass/fail 数字是 mock 自证。 | red-team 必须能对**真实 system_invoke**（真 agent 入口）跑，至少在 staging/CI 用真 provider；mock 仅限本地冒烟且报告里显著标注"mock，非真实对抗"。与 demo-script-honesty 同一"别用假证据"原则。 |
| **F109 / F035a(隔离半)** | `kun/skills/sandbox.py:43-63` `resolve_execution_cwd` 仅校验 cwd 落在 configured roots 内（docstring 自承"This is not a container"），无进程隔离、无 fs/network/syscall 限制；roots 由调用方 cwd 可放大。prompt-injected 代码在边界内可任意读写、联网、起子进程。**F035a 的 python-exec 进程/容器隔离诉求与此同根**，并入本行。 | 真隔离：容器 / seccomp / nsjail / 只读 rootfs + 无网络 namespace + 资源 cgroup；执行器接受能力声明白名单。属基础设施级改动。 |
| **F035a(死字段+误导描述半)** | `kun/skills/loader.py:56-58` `SkillManifest.allowed_commands/denied_patterns/denied_domains` 三字段**全仓零消费者**(grep `.allowed_commands` 0 命中)；而 `kun/skills/builtin/__init__.py:32` 仍宣称 shell-exec "受 allowlist 约束"、`skills/starter/os-shell/SKILL.md:10-20` 还填了真实 allowlist——实际唯一守卫是 `command_policy.py` 的 env denylist。是**安全诚实问题**(广告了不存在的 allowlist)。 | 二选一并修正描述：①真正用 `command_policy.check_shell_command` 强制 per-manifest allowlist；或②删三死字段 + 改 `builtin/__init__.py:32` 描述为"env denylist 约束" + 去 starter SKILL.md 的 allowlist 列。这半是**具体 fix**(非基础设施 epic)，可较早单做。 |
| **F148** | `kun/api/cockpit.py:282-296`(及 :328-339) `list_capabilities` 等端点 `tenant_id: str = Query("default")`——RLS 作用域由**调用方 query 参数**指定、默认 "default"，绕过租户中间件；`discipline_store.py` 兜底缓存无租户键。任意调用方可读任意/默认租户数据。 | 租户作用域必须来自**已认证身份**(中间件/JWT)，不接受 query 覆盖；cockpit 端点纳入与主 API 同一租户解析；兜底缓存按租户分键。直接关联 F007a(生产 auth fail-closed)/F066(前端身份)。 |
| **F151** | `kun/external_supervisor/service.py:207` 监督 prompt 由 `observation_payload` 直接格式化进模板（**可被被观察内容提示注入**）；（审计指出）解析兜底 fail-open + LLM 自由文本可覆盖确定性动作映射——被监督对象可借内容反向操纵监督裁决。 | 被观察内容做输入隔离/转义（明确区分"待审数据"与"指令"）；解析失败 **fail-closed**(默认最保守动作)；动作必须落**白名单枚举**，LLM 自由文本不得直接成为动作。并入 external-supervisor.md 的 enforce 模式设计。 |

## 2. 排期 / epic
- F148 跟随 auth epic(F007a/F066)——auth 落地时一并把所有端点租户作用域改为身份绑定、禁 query 覆盖。**优先级最高**(数据越权)。
- F151 并入 external-supervisor.md 的 fail-close + enforce 设计(F030/F031/F032/F115)。
- F102 跟随 demo-script-honesty/RSI 主链(有真入口可打才有意义)。
- F109 是独立基础设施 epic(真沙箱)，需求最大、可单列。

## 3. 覆盖 findings
F102, F109, F148, F151, F035a（标 needs-design 指向本文件）。F035a 隔离半并入 F109；死字段+误导描述半是独立具体 fix（见上表末行）。
关联：F007a/F066(auth，F148)、external-supervisor.md(F151)、demo-script-honesty.md(F102)、rsi-mainline-wiring.md。
