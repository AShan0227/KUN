# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Anthropic (derived from open skills; curated_by: KUN)
---
name: data-csv-query
description: Run SQL queries over a CSV file using DuckDB
version: 0.1.0
license: Apache-2.0
curated_by: KUN
source: https://github.com/anthropics/skills
maturity: cold_start
input_schema:
  type: object
  required: [csv_path, query]
  properties:
    csv_path:
      type: string
      description: Path to the CSV file (within sandbox)
    query:
      type: string
      description: SQL query, referencing the CSV as table `t`
    max_rows:
      type: integer
      default: 100
# 主动用工具 layer 3 — 看到 prompt 里有 .csv 文件路径就自己声明该被触发.
# pattern 命中 → orchestrator 用 extract 提参数, dispatch 该 skill.
# 字段格式跟 rules/proactive/triggers.yaml 完全一致.
auto_trigger_when:
  - pattern: '\S*\.csv\b'
    extract:
      kind: match_group_0
      param_name: csv_path
      extra_params:
        query: "SELECT * FROM t LIMIT 10"
---

# data-csv-query

Run a SQL query over a CSV file. Uses DuckDB for in-process execution. The
CSV is referenced as table `t` in the query.

## Example

```sql
SELECT region, SUM(amount) AS total
FROM t
GROUP BY region
ORDER BY total DESC
LIMIT 5;
```
