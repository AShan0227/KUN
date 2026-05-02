# KUN V2.3 启 (Qi) Grafana Dashboard

V2.3 启 (Qi) 子模式监控面板, Docker Compose 自动挂载, 无需手动 import.

## 启动

```bash
docker compose -f docker-compose.dev.yml up -d prometheus grafana
```

打开:
- Grafana: http://localhost:3011
- 默认账号 / 密码: `admin` / `admin`

进入左侧 **Dashboards** → 打开 `KUN / V2.3 启 (Qi)`.

## 自动挂载

- datasource provision: `kun/infra/grafana-datasources.yml`
- dashboard provision: `kun/infra/grafana-dashboards-provision.yaml`
- dashboard JSON: `kun/infra/grafana-dashboard-kun-v23.json`

## 面板看什么

V2.3 dashboard 看启 (Qi) + 协议涌现 + Predictive Coding + Pheromone + AntiGaming 健康度:

### 上行 (启状态)
- **启窗口当前活跃** (`kun_qi_window_active` gauge) — 0/1 per tenant
- **启今日花费** (`kun_qi_daily_spent_usd` gauge) — USD per tenant

### 中行 (协议涌现)
- **协议匹配命中率** (`kun_protocol_match_total{hit="true"}` rate) — 5min 窗口
- **协议 lifecycle 升级** (`kun_protocol_promotion_total{from_status, to_status}` rate) — experimental → shadow → canary → stable

### 中下行 (Predictive Coding)
- **PC error p50/p95** (`kun_predictive_coding_error_bucket` histogram) — per task_type
- **Pheromone 总强度** (`kun_pheromone_total_strength` gauge) — per tenant

### 下行 (反作弊 + cache)
- **Pheromone decay step 跑次** (`kun_pheromone_decay_step_total` rate)
- **AntiGaming 套路命中** (`kun_anti_gaming_detection_total{pattern}` rate) — 7 个 pattern + alert (>10/5min spike)
- **CapabilityCardCache hit rate** (`kun_capability_card_cache_hit_rate` gauge)

## 启用 metrics 上报

V2.3 metrics 默认会被 Prometheus 抓 (走 /metrics endpoint). 但需要先打开:

```bash
# 启 V2.3 runtime (装 ProtocolRegistry / Pheromone / QiBudget / CapabilityCardCache)
export KUN_QI_RUNTIME_ENABLED=1
# 真消费协议 + 反作弊
export KUN_PROTOCOL_CONSUME_ENABLED=1
export KUN_ANTI_GAMING_ENABLED=1
# Predictive Coding hook (默认开)
export KUN_PREDICTIVE_CODING_ENABLED=1
# Pheromone decay cron (默认开)
export KUN_PHEROMONE_DECAY_ENABLED=1
```

跑几个真任务后, dashboard 上的曲线就有数据.
