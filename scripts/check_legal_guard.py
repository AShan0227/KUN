"""Fail CI if public-repo legal guardrails are missing."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    "LICENSE",
    "NOTICE",
    "COMMERCIAL_USE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/legal/IP_POLICY.md",
    "docs/legal/PUBLIC_REPO_RISK.md",
]

REQUIRED_TEXT = {
    "LICENSE": [
        "PROPRIETARY SOURCE-AVAILABLE",
        "All rights reserved",
        "No rights are granted",
        "model",
    ],
    "README.md": [
        "public",
        "not open source",
        "COMMERCIAL_USE.md",
    ],
    "pyproject.toml": [
        'license = { text = "Proprietary" }',
    ],
}


def main() -> int:
    errors: list[str] = []
    for rel in REQUIRED_FILES:
        path = ROOT / rel
        if not path.exists():
            errors.append(f"missing required legal file: {rel}")

    for rel, needles in REQUIRED_TEXT.items():
        path = ROOT / rel
        if not path.exists():
            errors.append(f"missing required legal text source: {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        for needle in needles:
            if needle.lower() not in lower:
                errors.append(f"{rel} missing required phrase: {needle}")

    if errors:
        for error in errors:
            print(f"LEGAL_GUARD: {error}")
        return 1
    print("LEGAL_GUARD: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
