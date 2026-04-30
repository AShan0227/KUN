# Public Repository Risk Notes

KUN is currently public. That is useful for review and collaboration, but it
creates real risk.

## Main Risks

1. People can see the code and product thinking.
2. Bad actors can copy files even if copying is not legally allowed.
3. Copyright notices help enforcement, but they do not stop technical copying.
4. Product ideas and high-level concepts are weaker than code under copyright.
5. The more strategy documents we publish, the less trade-secret protection we
   have for those documents.

## Recommended Split

For stronger protection, split the project into:

- public repo: product shell, public docs, selected demos, legal notices;
- private repo: core strategy, commercial playbooks, sensitive prompts,
  customer data, private benchmarks, go-to-market plans, and anything that
  should remain a trade secret.

## Current Mitigation Added

- proprietary `LICENSE`;
- `NOTICE`;
- `COMMERCIAL_USE.md`;
- `CONTRIBUTING.md`;
- `SECURITY.md`;
- legal guard script in CI;
- README warning that public visibility is not permission.
