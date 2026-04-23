# SPDX-License-Identifier: Proprietary
# Copyright © 2026 KUN Project
---
name: research-web-fetch
description: Fetch a URL and extract clean text (no scripts/ads) for downstream analysis
version: 0.1.0
license: Proprietary
curated_by: KUN
maturity: cold_start
input_schema:
  type: object
  required: [url]
  properties:
    url:
      type: string
      format: uri
    selector:
      type: string
      description: Optional CSS selector to focus extraction
    max_chars:
      type: integer
      default: 8000
denied_domains:
  - example-banned.com
---

# research-web-fetch

Fetch a URL via HTTPS, parse HTML, strip scripts/ads, return clean text. Honors
`robots.txt`. Rate-limited per-tenant.
