# KUN 生产部署设计 v0.1

> 本文档是 KUN 从 docker-compose 本地开发 到 production 部署的架构设计。
> 假设规模：**Phase 2 初期单地域 1-100 租户 / 日 ≤ 10k 任务**。

---

## 一、运行时拓扑

```
                ┌──────────────────┐
                │  CDN / Edge      │  (CloudFlare / Fastly, 缓存静态资源)
                └────────┬─────────┘
                         │
                ┌────────▼─────────┐
                │   ALB / Ingress  │  (TLS 终止, sticky session for WS)
                └────────┬─────────┘
                         │
       ┌─────────────────┼──────────────────┐
       │                 │                  │
┌──────▼──────┐  ┌──────▼──────┐    ┌──────▼──────┐
│ KUN API     │  │ KUN API     │ ...│ KUN API     │  (FastAPI + uvicorn, 3-N replicas)
│ replica 1   │  │ replica 2   │    │ replica N   │   - api/main.py 主入口
│             │  │             │    │             │   - WS for 对话协议 (ADR-010)
│ Director    │  │ Director    │    │ Director    │
│ Executor    │  │ Executor    │    │ Executor    │
│ Tester      │  │ Tester      │    │ Tester      │
│ Gate        │  │ Gate        │    │ Gate        │
│ Supervisor  │  │ Supervisor  │    │ Supervisor  │  (Supervisor 是常驻 instance per replica)
│ Strategist  │  │ Strategist  │    │ Strategist  │  (Strategist on-demand 实例化)
└──────┬──────┘  └──────┬──────┘    └──────┬──────┘
       │                 │                  │
       └────────┬────────┴──────────────────┘
                │
       ┌────────┼────────┬────────────┬──────────────┐
       │        │        │            │              │
       │   ┌────▼───┐ ┌─▼─────┐ ┌────▼─────┐ ┌─────▼──────┐
       │   │ Postgres│ │ Redis │ │ NATS JS  │ │ S3/MinIO   │
       │   │ + RLS   │ │       │ │          │ │            │
       │   │ (managed│ │(cache+│ │(events + │ │(artifacts +│
       │   │  pri+RR)│ │ rate)│ │ outbox) │ │  reports)  │
       │   └─────────┘ └───────┘ └──────────┘ └────────────┘
       │
       └──── (separate deployment) ────┐
                                       │
                              ┌────────▼─────────┐
                              │ External Super   │   (单独 deployment, 本地推理)
                              │ visor (Pool)     │
                              │ + ollama         │   - qwen2.5:32b on GPU node
                              │   (GPU node)     │   - 或 CPU node + 量化模型
                              └──────────────────┘
```

---

## 二、组件清单 + Managed/Self-hosted 决策

| 组件 | Managed 选项 | Self-hosted 选项 | 推荐起步 |
|------|--------------|------------------|---------|
| Postgres + RLS | AWS RDS / GCP Cloud SQL / Aliyun PolarDB | EKS + CloudNativePG | **Managed** (备份 + RLS 不易出错) |
| Redis | AWS ElastiCache / MemoryDB | EKS + redis-operator | **Managed** (state 不复杂) |
| NATS JetStream | Synadia Cloud | k8s + nats-operator | Self-hosted (Synadia 贵 / 数据敏感) |
| S3 / Object Storage | AWS S3 / GCS / Aliyun OSS | MinIO on k8s | **Managed** (lifecycle + cost) |
| KUN API | — | k8s Deployment (3+ replicas) | Self-hosted on k8s |
| External Supervisor | — | k8s Deployment + GPU node | Self-hosted (本地模型) |
| ollama | — | k8s Pod (GPU node) | Self-hosted |
| Prometheus + Grafana | AWS Managed Prometheus / Grafana Cloud | EKS + kube-prometheus-stack | Grafana Cloud (起步省事) |
| Loki | Grafana Cloud Logs | EKS + loki-distributed | **Managed** (起步) |
| Jaeger | Grafana Tempo | EKS + jaeger-operator | **Managed Tempo** (起步) |
| Secrets | AWS Secrets Manager / Vault | k8s sealed-secrets | **AWS Secrets Manager** |

---

## 三、Kubernetes 部署 (推荐)

### 3.1 Namespace 切分

```
kun-prod/        # 生产 KUN service
kun-supervisor/  # External Supervisor (单独, 隔离故障域)
kun-data/        # 仅 NATS / 缓存自托管时用 (Postgres/Redis 走 managed 不进 k8s)
kun-monitoring/  # Prometheus / Grafana / Loki (如果自托管)
```

### 3.2 KUN API Deployment 关键配置

```yaml
# k8s/api-deployment.yaml (sketch)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kun-api
  namespace: kun-prod
spec:
  replicas: 3                          # 起步 3, HPA target CPU 60%
  strategy:
    type: RollingUpdate                # 滚动更新, maxSurge=1, maxUnavailable=0
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    spec:
      containers:
        - name: kun-api
          image: ghcr.io/yourorg/kun:v1.0.0
          ports:
            - containerPort: 8000
          env:
            - name: KUN_ENV
              value: production
            - name: KUN_PG_DSN
              valueFrom:
                secretKeyRef:
                  name: kun-secrets
                  key: pg_dsn          # 走 RDS 私网 endpoint
            - name: KUN_REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: kun-secrets
                  key: redis_url
            - name: KUN_NATS_URL
              value: nats://nats.kun-data.svc.cluster.local:4222
            - name: KUN_S3_BUCKET
              value: kun-prod-artifacts
            # ⚠️ 生产 KUN_PG_ADMIN_DSN 切实账号 (非 dev default 'kun:kun@...')
            # 配置不当会触发 InsecureProductionConfigError 启动失败
          resources:
            requests:
              cpu: 500m
              memory: 1Gi
            limits:
              cpu: 2
              memory: 4Gi
          livenessProbe:
            httpGet:
              path: /health/live
              port: 8000
            initialDelaySeconds: 30
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 5
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: kun-api-hpa
  namespace: kun-prod
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: kun-api
  minReplicas: 3
  maxReplicas: 12
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
    - type: Pods
      pods:
        metric:
          name: kun_websocket_connections    # custom metric: WS 连接数 per pod
        target:
          type: AverageValue
          averageValue: 200
```

### 3.3 External Supervisor + ollama (单独 namespace)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: external-supervisor
  namespace: kun-supervisor
spec:
  replicas: 1
  template:
    spec:
      nodeSelector:
        node-type: gpu               # 调到 GPU 节点 (本地推理用)
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
      containers:
        - name: ollama
          image: ollama/ollama:latest
          resources:
            limits:
              nvidia.com/gpu: 1
              memory: 32Gi
          volumeMounts:
            - name: ollama-models
              mountPath: /root/.ollama
        - name: external-supervisor
          image: ghcr.io/yourorg/kun:v1.0.0  # 复用 KUN image, command 不同
          command: ["python", "-m", "kun.external_supervisor"]
          env:
            - name: KUN_EXTERNAL_SUPERVISOR_ENABLED
              value: "true"
            - name: KUN_EXTERNAL_SUPERVISOR_MODEL_ID
              value: "qwen2.5:32b"
            - name: KUN_EXTERNAL_SUPERVISOR_BASE_URL
              value: "http://localhost:11434/v1"
      volumes:
        - name: ollama-models
          persistentVolumeClaim:
            claimName: ollama-models-pvc   # 50GB PVC, qwen2.5:32b ~ 20GB
```

### 3.4 Postgres (RLS-aware)

**关键**: 生产 KUN service 必须用 `kun_app` 账号 (非 superuser), 否则 RLS 被绕过.

- managed RDS 创建两个账号:
  - `kun_admin` — alembic migrations 用 (拥有 schema)
  - `kun_app` — runtime 用 (RLS-bound, 不能 bypass)
- secrets 分两份: `kun-secrets/pg_admin_dsn` 与 `kun-secrets/pg_dsn`
- alembic CronJob 每次 deploy 跑一次 migration (用 admin)
- API runtime 用 `pg_dsn` (走 `kun_app`)

---

## 四、Observability 接入

| 维度 | Stack | 关键指标 / 日志 |
|------|-------|----------------|
| Metrics | Prometheus | `llm_request_total{provider,model}`, `llm_latency_seconds`, `llm_cost_usd`, `kun_anomaly_emitted_total`, `kun_capability_promoted_total`, `kun_supervisor_cluster_total` |
| Logs | Loki | structlog JSON; correlate via `trace_id` |
| Traces | Jaeger / Tempo | OTLP → otel-collector → Jaeger; 关键 span: `Director.intent` / `Executor.invoke` / `Supervisor.observe` / `Strategist.propose` / `Gate.admit` |
| Cost | Grafana Dashboard | per-tenant `cost_usd_actual` / `cost_usd_equivalent` (ADR-008) |
| Alerting | Grafana Alerts → Slack/PagerDuty | `llm_fallback_rate > 0.5 for 10m` / `runtime_capabilities_state="rolled_back" rate > 0.2 / hour` |

---

## 五、Secrets + Auth (ADR-019 升级路径)

### 5.1 Phase 2 中期 (推荐先做这档)

- API 入口走 JWT (HS256/RS256), 短时 token + refresh token
- 多租户隔离: token 含 `tenant_id` claim, KUN API 中间件把它写入 PG session 变量 (`SET app.tenant_id = ...`)
- KUN_PG_DSN 用 `kun_app` 账号, RLS policy 比对 `app.tenant_id`
- Secrets 用 AWS Secrets Manager + IAM IRSA (k8s service account 链接)
- Webhook / external 调用走签名 (HMAC)

### 5.2 Phase 3 长期

- OAuth 2.0 / SSO (Auth0 / Okta / 飞书 SSO)
- 多租户 SaaS 化: per-tenant subscription + billing 集成
- 数据本地化: 不同地域 RDS, KMS per-region 加密

---

## 六、CI/CD 流程

```
Push to main
    ↓
GitHub Actions:
  1. pytest tests/unit (全绿才继续)
  2. ruff check
  3. mypy / pyright (类型检查)
  4. docker build → ghcr push (tagged v$semver-$sha)
  5. trigger ArgoCD sync
    ↓
ArgoCD (GitOps):
  - 监控 k8s manifest repo
  - rolling update KUN API
  - 跑 alembic migration job (kun_admin)
  - smoke test (curl /health/ready)
  - rollback on failure
```

**Migration 安全**:
- alembic upgrade 跑在单独 Job (非 API container 启动时跑)
- Job 失败 → API 不滚动 → 老版本继续服务
- Schema 变动按 ADR 走 (expand → migrate data → contract 三步法)

---

## 七、成本估算 (起步规模)

| 项 | 月度估算 (USD) | 说明 |
|---|---------------|------|
| EKS 控制平面 | $73 | AWS managed |
| 3 × API pod (1 vCPU, 2GB) | $90 | t3.medium spot-friendly |
| 1 × External Supervisor + GPU (g5.xlarge) | $700 | NVIDIA A10G, 24/7 |
| RDS Postgres (db.r6g.large + Multi-AZ) | $350 | + storage |
| ElastiCache Redis (cache.r6g.large) | $150 | |
| NATS (k8s self-host on existing node) | $0 | |
| S3 (100GB + 100k PUT/月) | $5 | |
| Grafana Cloud (free tier OK 起步) | $0 | 上规模后约 $200 |
| **LLM 调用** | $200-2000 | 取决于流量, 走 capability_router cost gate (ADR-008) |
| **合计起步** | **~$1600-3500/月** | 不含 LLM, GPU 是大头 |

**优化点** (上规模后):
- External Supervisor 不需要 24/7 GPU — 转 spot / pre-empt + 任务批 (本地推理慢, 可异步)
- 多租户共享 ollama pool (Pool config 已经支持)
- canary 流量先在低成本 instance, ready 状态切高 cost (capability_router 的 cost gate 反向约束)

---

## 八、上线 Checklist

### Phase 1 → Phase 2 切换前

- [ ] `KUN_PG_ADMIN_DSN` / `KUN_S3_*` 凭证全切真值 (dev 默认会触发 `InsecureProductionConfigError`)
- [ ] `KUN_ENV=production` 启动检查通过
- [ ] alembic upgrade 到 0011 (head)
- [ ] RLS policy 真实生效 (用 `kun_app` 账号 SELECT 看是否被 filter)
- [ ] WebSocket auth 真接 (ADR-019 中期 JWT)
- [ ] OpenTelemetry 真上报 → otel-collector → Jaeger
- [ ] Grafana Dashboard 关键指标可视化
- [ ] Alerting rule 配齐 (fallback / rollback / cost)
- [ ] 备份策略: RDS daily backup + 7-day retention
- [ ] DR 演练: kill 一个 API pod / 一个 AZ, 看 service 自愈
- [ ] Load test: 100 RPS sustained, 看 capability_router cold-start damping 是否真起作用

### L6 Phase 2 商业化前

- [ ] 选定垂直行业 (电商 / 投放 / 内容分发 / CRM)
- [ ] 业务能力卡冷启动校准任务集 ≥ 6 个 (ADR-011)
- [ ] 接入层 adapter (Browser-first hybrid) 至少 1 个目标 SaaS 跑通
- [ ] Billing 集成 (Stripe / 阿里云开发者) — 计费走 ADR-008 等效价格 → 真 API 价
- [ ] Onboarding flow + 用户文档
- [ ] 客服 / 反馈通道

---

## 九、风险 + 缓解

| 风险 | 缓解 |
|------|------|
| External Supervisor GPU 故障 → 监督线挂 | Pool 多副本 + 主路径 fallback 到 `concerning` verdict (保守) |
| Postgres RLS 配置错 → 跨租户数据泄露 | Phase 2 上线前用 integration test 跨 tenant 调 API 验证 |
| LLM cost 失控 (capability_router cold-start) | ADR-008 budget gate + 日预算 hard cap + Slack alert at 80% |
| Strategist 自指改 governance → 系统死锁 | L3.5 自指限制强化 + Gate enable_capability `human_approval_token` 必校 |
| ollama 模型推理慢 (qwen2.5:32b ~ 5-10s) | External Supervisor concurrency=2 + asyncio.Semaphore; fallback 不阻塞主线 |

---

*最后更新：2026-05-27*  
*下次更新触发：选定 L6 垂直行业后, 加 industry-specific deployment section*
