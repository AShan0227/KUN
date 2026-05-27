# Ollama 本地模型启动指南

> KUN 的 External Supervisor (ADR-023) 默认走本地推理引擎 ollama, 避免主线
> token 预算被监督线吃掉。本文档说明如何启动 + 切换模型规模。

---

## 一、起 ollama service

ollama 在 `docker-compose.dev.yml` 里走 `external-supervisor` profile, 默认不拉起。

```bash
# 拉镜像 + 起 service
docker compose -f docker-compose.dev.yml --profile external-supervisor up -d ollama

# 检查
docker compose -f docker-compose.dev.yml ps ollama
curl -s http://localhost:11434/api/version
```

---

## 二、选模型 (按 RAM + 用途分)

| 模型 | 大小 | RAM 需求 | 用途 | 拉取命令 |
|------|------|---------|------|---------|
| `qwen2.5:0.5b` | ~400MB | 2GB | 烟测连通 / CI smoke | `docker exec kun-dev-ollama-1 ollama pull qwen2.5:0.5b` |
| `qwen2.5:1.5b` | ~1.5GB | 4GB | 轻量自嗨检测 | `docker exec kun-dev-ollama-1 ollama pull qwen2.5:1.5b` |
| `qwen2.5:7b` | ~5GB | 8GB | 一般 External Supervisor | `docker exec kun-dev-ollama-1 ollama pull qwen2.5:7b` |
| `qwen2.5:14b` | ~9GB | 16GB | 平衡推理质量 + 资源 | `docker exec kun-dev-ollama-1 ollama pull qwen2.5:14b` |
| **`qwen2.5:32b` (生产推荐)** | ~20GB | 32GB | ADR-023 默认 | `docker exec kun-dev-ollama-1 ollama pull qwen2.5:32b` |

> **推荐**: 开发期用 `qwen2.5:1.5b` 验证流程 (1 分钟下载完); 生产切 `qwen2.5:32b`.

---

## 三、配置 KUN 用哪个模型

修改 `.env` 或 export 环境变量:

```bash
# 启用 External Supervisor + 指定模型
export KUN_EXTERNAL_SUPERVISOR_ENABLED=true
export KUN_EXTERNAL_SUPERVISOR_MODEL_ID=qwen2.5:32b   # 或更小的
export KUN_EXTERNAL_SUPERVISOR_BASE_URL=http://localhost:11434/v1
export KUN_EXTERNAL_SUPERVISOR_MAX_CONCURRENT=2       # ollama 并发数
```

---

## 四、烟测脚本 (验证真接通)

```bash
uv run python -c "
import asyncio
from kun.interface.llm.local_provider import LocalLLMProvider
from kun.interface.llm.base import LLMRequest, LLMMessage

async def main():
    p = LocalLLMProvider(model_id='qwen2.5:0.5b')  # 用最小模型烟测
    ok = await p.health_check()
    print(f'health_check: {ok}')
    if ok:
        resp = await p.invoke(LLMRequest(
            messages=[LLMMessage(role='user', content='Say hello in one word.')],
            max_tokens=20,
        ))
        print(f'response: {resp.content[:200]}')
        print(f'latency_ms: {resp.latency_ms:.0f}')
        print(f'tokens: in={resp.usage.input_tokens} out={resp.usage.output_tokens}')

asyncio.run(main())
"
```

期望输出:
```
health_check: True
response: Hello
latency_ms: 1500
tokens: in=15 out=2
```

---

## 五、跑一次真 External Supervisor

```bash
uv run python -c "
import asyncio
import json
from kun.interface.llm.local_provider import LocalLLMProvider
from kun.external_supervisor.service import ExternalSupervisorService
from kun.external_supervisor.modes import mode_a_gate_review

async def main():
    provider = LocalLLMProvider(model_id='qwen2.5:0.5b')
    service = ExternalSupervisorService(llm_provider=provider, max_concurrent=1)

    advisory = await mode_a_gate_review(
        service,
        anchor={'goal_statement': 'Ship auth middleware safely'},
        gate_evidence={
            'test_report': {'passed': 18, 'failed': 2},
            'change_summary': 'Added JWT validation; coverage 92%',
        },
    )
    print(f'verdict: {advisory.verdict}')
    print(f'rationale: {advisory.rationale[:200]}')
    print(f'recommended_action: {advisory.recommended_action}')
    print(f'model_used: {advisory.underlying.model_used}')

asyncio.run(main())
"
```

期望输出 (小模型可能稳定性不高, 但应该能给出 verdict):
```
verdict: approve | escalate | block
rationale: <一段 LLM 输出 — 小模型可能短>
recommended_action: continue | pause | human_review | <LLM 提供的具体动作>
model_used: qwen2.5:0.5b
```

---

## 六、生产部署 (qwen2.5:32b)

详见 [`docs/PROD_DEPLOY_DESIGN.md`](./PROD_DEPLOY_DESIGN.md) §3.3.

要点:
- ollama 调到 GPU 节点 (NVIDIA A10G+ 即可, 24GB VRAM 跑 32b)
- 模型存储用 PVC (50GB+)
- External Supervisor concurrency=2 (推理慢, 不能让 caller queue 爆)
- 主线 fallback: ollama 不可用时 `concerning` verdict 兜底 (服务降级不挂)

---

## 七、已知问题

### macOS Metal 兼容 (2026-05 起)

如果跑模型时遇到 `static_assert failed ... __is_same_v<half, bfloat>` 错误,
说明 ollama 版本与你 macOS Metal API 不兼容:

```bash
# 升级 ollama
brew upgrade ollama

# 或者强制跑 CPU 模式 (慢, 但能跑)
OLLAMA_NUM_GPU=0 OLLAMA_LLM_LIBRARY=cpu ollama serve
```

实测 2026-05-27 在 macOS 15+ 上 ollama 0.20.7 必须升级到 0.21+
才能用 Metal. KUN `LocalLLMProvider` 已通过单测验证 (`tests/unit/test_local_provider.py`),
此问题仅影响真实启动, 不影响 KUN 代码正确性.

### 内存不足

macOS 8GB 跑 qwen2.5:0.5b 应该够; qwen2.5:32b 需要 32GB+ 物理 RAM. 不够会触发 OOM.

### Docker / colima 拉镜像超时

如果走 docker compose, colima VM 镜像拉取可能很慢. 推荐**本地 brew 装 ollama**:
```bash
brew install ollama
brew services start ollama
# 之后 KUN 走 http://localhost:11434 不需要 docker
```

---

## 八、停止 ollama

```bash
docker compose -f docker-compose.dev.yml --profile external-supervisor stop ollama

# 彻底删 (包括镜像数据, 慎用!)
docker compose -f docker-compose.dev.yml --profile external-supervisor down -v
```

注意: `down -v` 会删 `ollama_data` volume → 下次起要重新 pull 模型. 平时用 `stop`.

---

*最后更新：2026-05-27*
