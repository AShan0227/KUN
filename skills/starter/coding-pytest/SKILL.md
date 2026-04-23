# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Anthropic (derived from open skills; curated_by: KUN)
---
name: coding-pytest
description: Run Python tests with pytest and summarize results
version: 0.1.0
license: Apache-2.0
curated_by: KUN
source: https://github.com/anthropics/skills
maturity: cold_start
input_schema:
  type: object
  required: [target]
  properties:
    target:
      type: string
      description: pytest target (path or nodeid); e.g. tests/unit/test_foo.py
    markers:
      type: string
      default: ""
      description: pytest -m marker expression
    verbose:
      type: boolean
      default: false
---

# coding-pytest

Execute pytest with the given target. Surface:
- Number passed / failed / skipped
- Failing test nodeids with short tracebacks
- Coverage if `--cov` was enabled
