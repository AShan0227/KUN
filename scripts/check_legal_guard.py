"""Fail CI if public-repo legal guardrails are missing."""

from __future__ import annotations

import shutil
import subprocess
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

SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
FORBIDDEN_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_dsa",
    "id_ed25519",
}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
FORBIDDEN_TEXT = {
    "BEGIN " + "PRIVATE KEY": "private key material",
    "BEGIN " + "RSA PRIVATE KEY": "private key material",
    "AKIA": "possible AWS access key",
    "客户数据": "customer data marker",
    "customer data": "customer data marker",
    "investor deck": "investor material marker",
    "内部 GTM": "internal GTM marker",
    "internal gtm": "internal GTM marker",
}
FORBIDDEN_TEXT_SCAN_ALLOWLIST = {
    "CONTRIBUTING.md",
    "docs/legal/IP_POLICY.md",
    "docs/legal/PUBLIC_REPO_RISK.md",
    "docs/ops/release-checklist-v4.md",
    "scripts/check_legal_guard.py",
}
TEXT_SCAN_SUFFIXES = {
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".env",
    ".example",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
}


def check_legal_guard(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for rel in REQUIRED_FILES:
        path = root / rel
        if not path.exists():
            errors.append(f"missing required legal file: {rel}")

    for rel, needles in REQUIRED_TEXT.items():
        path = root / rel
        if not path.exists():
            errors.append(f"missing required legal text source: {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        for needle in needles:
            if needle.lower() not in lower:
                errors.append(f"{rel} missing required phrase: {needle}")
    errors.extend(_scan_for_public_repo_leaks(root))
    return errors


def _scan_for_public_repo_leaks(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _iter_public_repo_files(root):
        rel_path = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel_path.parts):
            continue
        rel = rel_path.as_posix()
        if path.name in FORBIDDEN_FILE_NAMES:
            errors.append(f"forbidden sensitive file in public repo: {rel}")
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden sensitive file suffix in public repo: {rel}")
            continue
        if path.suffix.lower() not in TEXT_SCAN_SUFFIXES and path.name not in REQUIRED_FILES:
            continue
        if rel in FORBIDDEN_TEXT_SCAN_ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            errors.append(f"could not scan file for public repo leaks: {rel}: {exc!r}")
            continue
        lower = text.lower()
        for needle, label in FORBIDDEN_TEXT.items():
            if needle.lower() in lower:
                errors.append(f"{rel} contains forbidden {label}: {needle}")
                break
    return errors


def _iter_public_repo_files(root: Path) -> list[Path]:
    git = root / ".git"
    if git.exists():
        git_bin = shutil.which("git")
        if git_bin is None:
            return [path for path in root.rglob("*") if path.is_file()]
        try:
            proc = subprocess.run(
                [git_bin, "ls-files", "-z"],
                cwd=root,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            files = [root / item.decode("utf-8") for item in proc.stdout.split(b"\0") if item]
            return [path for path in files if path.is_file()]
    return [path for path in root.rglob("*") if path.is_file()]


def main() -> int:
    errors = check_legal_guard(ROOT)
    if errors:
        for error in errors:
            print(f"LEGAL_GUARD: {error}")
        return 1
    print("LEGAL_GUARD: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
