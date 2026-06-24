# Executable-skill sandbox — real isolation plan (DRAFT)

> Status: **needs-review** (Loop-2 TIER-2 #5). Operationalizes the F109 row in [security-posture.md](../security-posture.md) and the isolation half of F035a. No code here.
> Author: audit fix-loop · Date: 2026-06-24

## 1. Current state + threat model
- `kun/skills/sandbox.py:resolve_execution_cwd` only confines the **cwd** to configured roots (`KUN_SKILL_EXEC_ROOTS`, default `/tmp/kun-skill-exec`). Its own docstring: *"This is not a container."*
- `kun/skills/command_policy.py:check_shell_command` adds a **denylist** (catastrophic one-liners) + an optional **env allowlist mode** (`KUN_SHELL_EXEC_ALLOW`, fail-closed when set). This is the *real* guard (the dead *per-manifest* `allowed_commands` field was removed in F035a — the honesty half).
- **Threat**: prompt-injected code executed by shell-exec / python-exec runs as the KUN process with **no process isolation** — within the cwd boundary it can read/write any path the process can, open network sockets, and spawn subprocesses. The cwd boundary stops *accidental* host writes, not a *malicious* payload.

So F035a's "phantom allowlist" honesty debt is paid; the **real isolation** (F109) is the remaining work and is infrastructure-level.

## 2. Isolation options (increasing strength / complexity)
| tier | mechanism | strength | cost / portability |
|---|---|---|---|
| (a) **subprocess hardening** | run exec in a child with dropped privileges, `rlimit` (CPU/mem/fsize/nofile), `unshare` net namespace (no network), `umask`, time limit | blocks network + resource exhaustion + accidental escalation; same-kernel | cheap; Linux-only for unshare; macOS dev degrades to limits-only |
| (b) **seccomp-bpf syscall allowlist** | filter syscalls to a safe set | blocks dangerous syscalls (ptrace, mount, raw sockets) | Linux-only; needs a curated syscall profile per runtime |
| (c) **lightweight jail** (nsjail / bubblewrap) | namespaces + bind-mounts + seccomp, read-only rootfs, tmpfs workdir | strong fs/net/pid isolation without a full container runtime | Linux; extra binary dep; the pragmatic prod target |
| (d) **full sandbox** (gVisor / Firecracker / container) | kernel-level or microVM isolation | strongest | heaviest ops; for untrusted multi-tenant code execution |

## 3. Recommendation (phased)
- **Dev (macOS / no namespaces)**: keep the cwd boundary + command_policy, add subprocess `rlimit` + time limit where the platform allows, and **clearly log** that strong isolation is not active (don't pretend — same honesty principle as F035a). Optionally allowlist-mode (`KUN_SHELL_EXEC_ALLOW`) on by default in dev.
- **Prod (Linux)**: tier (c) **nsjail/bubblewrap** as the baseline — read-only rootfs, tmpfs/bind-mounted workdir under the exec root, **no network namespace** by default, cgroup CPU/mem/pids limits, seccomp profile. Escalate to (d) gVisor only if running genuinely untrusted third-party code.
- Selected by `KUN_SKILL_EXEC_ISOLATION = none|limits|jail` (env), defaulting to `jail` in production (fail-closed: if the jail binary/namespaces are unavailable in prod, **refuse to exec**, don't silently downgrade).

## 4. Capability-declaration allowlist (default-minimal)
- A skill **declares** the capabilities it needs (fs paths, network egress, subprocess) and the executor grants only those; default = none (no net, workdir-only fs, no spawn).
- **Relation to F035a**: the dead per-manifest `allowed_commands`/`denied_patterns`/`denied_domains` fields were removed. If a per-skill capability declaration is reintroduced, it **must be actually enforced** by the executor (wired through command_policy + the jail profile) — never a typed field with no consumer again. This plan is where that enforcement would live.

## 5. Stepwise landing + per-step verification (escape tests)
| step | change | escape-test assertion |
|---|---|---|
| 1 | subprocess `rlimit` + time limit + (Linux) net-unshare | a payload that opens a socket / forks unbounded / runs forever → killed/blocked |
| 2 | `KUN_SKILL_EXEC_ISOLATION` selector + prod fail-closed | prod with jail unavailable → exec refused (not silently downgraded) |
| 3 | nsjail/bubblewrap profile: ro-rootfs, tmpfs workdir, no-net, cgroup | payload reads `/etc/passwd` or `~/.ssh` → denied; writes outside workdir → denied; network → denied |
| 4 | capability declaration + executor enforcement | a skill without `net` capability that attempts egress → denied; with declared paths only those readable |
| 5 | seccomp profile (optional hardening) | ptrace/mount/raw-socket syscalls → blocked |

A standing **escape-PoC suite** (read-outside-sandbox, network-egress, spawn-subprocess, resource-exhaust, path-traversal) is the regression gate — all must be blocked.

## 6. Risk / rollback
- **Risk**: a legitimate skill needs network or a path and the jail blocks it → capability declaration + per-skill grants; start jail in **audit/log-only** mode in staging to find false positives before enforcing.
- **Risk**: cross-platform — namespaces/seccomp are Linux-only; macOS dev can't run tier (c)/(d). Mitigation: tiered selector + honest logging when strong isolation is inactive (never claim "sandboxed" when it's only a cwd boundary).
- **Rollback**: `KUN_SKILL_EXEC_ISOLATION=limits|none` to step down (non-prod); jail profile is additive per step.

## 7. Acceptance
- Production refuses to exec a skill when the configured isolation tier is unavailable (fail-closed).
- The escape-PoC suite (read-outside / net / spawn / resource / traversal) is fully blocked under the prod jail tier.
- No code path claims "sandboxed/isolated" when only the cwd boundary is active (honesty, per F035a).

## 8. Covers / relates
F109 (sandbox isolation) + F035a isolation half. Independent infra epic (can land in parallel with the persistence/RSI epics). The F035a honesty half (dead fields + misleading description) is already landed (59567a6).
