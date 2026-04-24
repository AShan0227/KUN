#!/usr/bin/env bash
# Push .github/workflows/ci.yml after granting 'workflow' OAuth scope.
#
# Why this exists:
#   The initial push to GitHub stripped the workflow file because the gh CLI
#   OAuth token didn't have 'workflow' scope. The file is restored locally
#   but uncommitted. Running this script:
#     1. Refreshes gh auth to add 'workflow' scope (requires browser click)
#     2. Stages + commits the workflow
#     3. Pushes

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -f .github/workflows/ci.yml ]]; then
    echo "error: .github/workflows/ci.yml not found" >&2
    exit 1
fi

echo "==> Refreshing gh auth with workflow scope (browser will open; click 'authorize')"
gh auth refresh -h github.com -s workflow

echo "==> Committing workflow"
git add .github/workflows/ci.yml
git commit -m "ci: add GitHub Actions workflow (lint / test / integration / license-scan)" || {
    echo "(nothing to commit?)"
}

echo "==> Pushing"
git push origin main

echo "==> Done. View at: https://github.com/AShan0227/KUN/actions"
