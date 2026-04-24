# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright © 2026 KUN Project
---
name: os-shell
description: Execute vetted, non-destructive shell commands in the task sandbox
version: 0.1.0
license: LicenseRef-Proprietary
curated_by: KUN
maturity: cold_start
allowed_commands:
  - ls
  - cat
  - echo
  - wc
  - head
  - tail
  - grep
  - find
  - python3
  - pytest
  - node
denied_patterns:
  - "rm"
  - "sudo"
  - "curl"
  - "wget"
  - "ssh"
  - ">/"
  - "&&"
input_schema:
  type: object
  required: [command]
  properties:
    command:
      type: string
      description: The shell command to run. Must start with an allowed command.
    timeout_sec:
      type: number
      default: 10
---

# os-shell

Run a vetted shell command inside the sandbox. Destructive commands
are blocked by the `denied_patterns` list.

**Scope**: read-only or safe idempotent operations. Anything that
writes outside the working copy or calls the network should use a
more specific skill (e.g., `research-web-fetch`).

## Example

```json
{
  "command": "wc -l **/*.py",
  "timeout_sec": 5
}
```
