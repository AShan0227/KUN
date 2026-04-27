# KUN-Lab Grafana Dashboard

KUN-Lab 的 Grafana 面板现在由 Docker Compose 自动挂载，不需要手动 import。

## 启动

```bash
docker compose -f docker-compose.dev.yml up -d prometheus grafana
```

打开：

- Grafana: http://localhost:3011
- 默认账号: `admin`
- 默认密码: `admin`

进入左侧 **Dashboards**，打开 `KUN / KUN-Lab`。

## 自动挂载了什么

- datasource provision: `kun/infra/grafana-datasources.yml`
- dashboard provision: `kun/infra/grafana-dashboards-provision.yaml`
- dashboard JSON: `kun/infra/grafana-dashboard-kun-lab.json`

容器内路径：

- `/etc/grafana/provisioning/dashboards/kun-lab.yml`
- `/var/lib/grafana/dashboards/kun-lab.json`

## 面板看什么

这张 dashboard 主要看 KUN-Lab 周边健康度：

- 实验吞吐
- 累积成本
- path 成功/失败
- cost-cap 触发
- recipe 推升
- registry size

这些指标来自 `kun/core/metrics.py` 的 `kun_lab_*` 系列 Prometheus metrics。

## 常见问题

如果 dashboard 没出现，先确认：

```bash
docker compose -f docker-compose.dev.yml logs grafana
```

重点看 provisioning dashboard 是否读取了 `/var/lib/grafana/dashboards/kun-lab.json`。
