# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 KUN Project (derived from Anthropic open skills, curated_by: KUN)
---
name: writing-markdown
description: Produce well-formatted Markdown (headings, lists, tables, code blocks)
version: 0.1.0
license: Apache-2.0
curated_by: KUN
source: https://github.com/anthropics/skills
maturity: cold_start
input_schema:
  type: object
  required: [intent]
  properties:
    intent:
      type: string
    style:
      type: string
      enum: [casual, formal, technical]
      default: technical
    max_words:
      type: integer
      default: 400
---

# writing-markdown

Format writing output as Markdown. Ensures:
- Consistent heading levels (H1 → H2 → ...)
- Bullet lists use `-` not `*`
- Code blocks have language tag
- Tables are pipe-delimited + aligned
- No trailing whitespace

Pair with any writing task that produces long-form output.
