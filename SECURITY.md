# Security Policy

KUN handles agent orchestration, external actions, tenant boundaries, secrets,
memory, and long-running tasks. Please report security issues privately.

## Report Privately

Do not open a public GitHub issue for vulnerabilities.

Send a private report to the project owner with:

- affected file, endpoint, action type, or workflow;
- reproduction steps;
- impact;
- whether secrets, tenants, external actions, or user data are involved;
- suggested fix, if known.

## In Scope

- tenant isolation and RLS bypass;
- WorldGateway external action abuse;
- secret leakage or weak secret handling;
- prompt injection that causes unauthorized side effects;
- SSRF, path traversal, command execution, or browser automation abuse;
- event/outbox duplication that can trigger repeated external actions;
- memory poisoning, capability-card poisoning, or evaluation manipulation;
- misleading delivery status that marks unsafe or partial features as ready.

## Out of Scope

- social engineering;
- denial-of-service without a concrete security impact;
- findings requiring compromised developer machines;
- generic dependency reports without a working exploit path in KUN.

## Safe Harbor

Good-faith security research is welcome when it avoids data destruction,
privacy violations, service disruption, and unauthorized external actions.
Commercial use, redistribution, model training, or reuse of KUN materials is
still prohibited unless separately authorized in writing.
