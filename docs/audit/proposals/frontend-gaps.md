# 前端缺口 · 合并方案（F066/F067/F105/F106/F107/F108）

> 状态：needs-design（鉴权依赖 ADR-019 真 auth；WS 5 消息类型对应的后端长任务双线链路本身部分未接通——见 rsi-mainline-wiring 第 7 步 F025）

## 0. 根因

前端是"半成品 + 与未接通的后端链路对齐"：硬编码身份、未定义的样式类、覆盖不全且不健壮的 WS、
死链导航。多数不是孤立前端 bug，而是**后端能力没通**(auth、长任务双线、计费)在前端的投影。

## 1. 逐条现状（已核实）

| ID | 现状 |
|----|------|
| **F066** | `frontend/src/app/page.tsx:40` WS_URL 写死 `?tenant_id=u-sylvan&user_id=sylvan`，零鉴权；审批/操作按钮任意访问者可点并触发真实副作用。与后端 F126(WS query 租户 opt-in)、F007a(生产 auth fail-closed)同源。 |
| **F067** | `kun-nav-link` 等 `kun-*` 组件样式类在 `layout.tsx`/`page.tsx`/`nuo/page.tsx`/`control-plane/page.tsx` 共 4 处使用，但 `globals.css` 中 **0 处定义**(grep 确认)——这些界面实际无样式渲染。 |
| **F105** | `page.tsx` `dispatchIncoming` 的 `switch(type)` 覆盖一组消息，但长任务双线交互的若干服务端消息类型落到 `default` 被静默丢弃；后端对应链路同样未接通(WS task_state 死代码，见 rsi-mainline F025)。 |
| **F106** | `layout.tsx` 导航含 `/billing`、`/account` 两个链接，但 `frontend/src/app/` 下**无** billing/account 路由目录(ls 确认)→ 必 404；且用对象字面量 `href={{pathname:...}}` 绕过 Next typedRoutes 检查。 |
| **F107** | `cockpit/page.tsx`(及 control-plane)的 fetch 由 `onChange`/`useEffect` 触发，无 `AbortController`/请求取消、无竞态防护——每次 keystroke 触发整组请求，旧响应可覆盖新响应。 |
| **F108** | `page.tsx` WS `onclose` 仅 `setConnected(false)`，**无重连**；`messages`/`side` 数组 `setMessages([...m, new])` **无上限增长**。后端重启后 UI 永久停在"未连接"。 |
| **F153** | ~~`cockpit/page.tsx` fetch `/cockpit/*` 无 rewrite → 同源 404~~ **已 land(Loop-2 #10)**：`frontend/next.config.mjs` rewrites 加了 `{ source: "/cockpit/:path*", destination: ${apiOrigin}/cockpit/:path* }`(与既有 /api、/nuo 同型，`node --check` 语法通过)。dev CORS：**不改后端 config 运行时默认**，在 `.env.example` CORS 注释说明 dev 多端口时设 `KUN_API_CORS_ORIGINS=http://localhost:3000,http://localhost:3001`。 | ✅ rewrite + CORS 文档已落地。**残留(需人)**：在跑起来的 Next dev + 后端栈里目视确认 /cockpit 页面请求被代理、不再 404(前端 pytest 不覆盖，改动是与现有可用 rewrite 同型的一行，高把握但未端到端跑过)。 |

## 2. 处理方向

- **F066 鉴权**：接 ADR-019 真 auth——登录后用会话令牌建立 WS(从 cookie/header 取身份)，移除 URL 里写死的
  `tenant_id/user_id`；副作用按钮按角色/登录态门控。依赖后端 auth 落地(F007a 已让生产 auth-off fail-closed)。
- **F067 样式**：要么在 `globals.css`/Tailwind 层定义 `kun-*` 组件类，要么把模板里的 `kun-*` 换成实际存在的类；
  加一个构建期/测试期守卫(检测模板引用的类是否有定义)防再漂移。
- **F105 WS 协议**：把长任务双线的全部服务端消息类型纳入 `dispatchIncoming`(显式处理而非落 default 丢弃)，
  `default` 分支改为可见告警而非静默；与后端 F025(填充 WS task_state、handle_long_task_input 6-bucket 路由)同排期。
- **F106 死链**：补 `/billing`、`/account` 路由页，或从导航移除；恢复 typedRoutes 检查(别用对象字面量 href 绕过)。
- **F107 竞态**：每个请求挂 `AbortController`，输入加 debounce，旧请求在新输入时 abort；按 request-id/序号丢弃过期响应。
- **F108 WS 健壮性**：加指数退避重连 + 在线状态恢复；`messages`/`side` 加上限(环形缓冲/截断老消息)。

## 3. 排期 / 耦合
- F066/F105 与后端耦合(auth、长任务双线链路)——后端没通前，前端只能做到"诚实显示未接通"，不能假装可用。
- F067/F106/F107/F108 是相对独立的前端工程改进，可先做(不依赖后端)。
- 建议作为「前端可用化」一个小 epic，F067/F106/F107/F108 先行，F066/F105 跟随后端 auth + RSI 长任务链路。

## 4. 覆盖 findings
F066, F067, F105, F106, F107, F108, F153（标 needs-design 指向本文件）；F066 关联 F007a/F126，F105 关联 rsi-mainline-wiring F025，F153 是 near-trivial 一行 rewrite 修复(F067/F106/F107/F108/F153 可先行)。
