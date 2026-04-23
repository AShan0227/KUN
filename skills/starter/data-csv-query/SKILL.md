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
