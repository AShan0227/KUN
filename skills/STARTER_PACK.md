# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 KUN Project

# KUN Starter Pack

> 从 Anthropic 140+ 开源 skill 精选, 覆盖 coding / writing / research / data / os 五大类.
> 每个 skill 保留原作者 + 原 LICENSE, 详见 `SKILL.md` 头部 frontmatter.
> 新增一行 `curated_by: KUN` (ADR-014).

## 挑选原则 (ADR-014)

1. 社区高频使用 / star 数排前
2. 覆盖 coding / writing / research / data / os 五大类
3. 不依赖特定外部服务 (能独立跑的优先)
4. 许可证可商用 (MIT / Apache-2.0 / BSD-3-Clause)

## 包含 skill 清单 (首批最小集, 后续按需增删)

| Skill | Source | License | Purpose |
|-------|--------|---------|---------|
| `coding-pytest` | Anthropic open skills | Apache-2.0 | 运行 Python 测试 |
| `coding-docx` | Anthropic open skills | Apache-2.0 | 读 / 写 docx |
| `coding-xlsx` | Anthropic open skills | Apache-2.0 | 读 / 写 spreadsheet |
| `writing-markdown` | Anthropic open skills | Apache-2.0 | 格式化 markdown |
| `research-web-fetch` | Internal | LicenseRef-Proprietary | 抓取 URL 并解析 |
| `data-csv-query` | Anthropic open skills | Apache-2.0 | 在 CSV 上做 SQL |
| `os-shell` | Internal | LicenseRef-Proprietary | 受限 shell 命令 |

后续在 `skills/<skill-name>/SKILL.md` 添加完整 skill 定义.

## 合规 (ADR-014)

- 每次 CI run 跑 `reuse lint` 扫描许可证 (已接入 `.pre-commit-config.yaml`)
- 任何新 skill 接入前需出现在本清单
- 非 MIT / Apache / BSD 许可证直接拒绝合并
