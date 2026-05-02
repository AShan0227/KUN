from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load_legal_guard() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_legal_guard.py"
    spec = importlib.util.spec_from_file_location("kun_check_legal_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_clean_public_repo(root: Path) -> None:
    (root / "docs" / "legal").mkdir(parents=True)
    (root / "LICENSE").write_text(
        "PROPRIETARY SOURCE-AVAILABLE\nAll rights reserved.\n"
        "No rights are granted to copy model assets.\n",
        encoding="utf-8",
    )
    (root / "NOTICE").write_text("KUN notice.\n", encoding="utf-8")
    (root / "COMMERCIAL_USE.md").write_text("Commercial use needs permission.\n", encoding="utf-8")
    (root / "CONTRIBUTING.md").write_text("No secrets, no customer data.\n", encoding="utf-8")
    (root / "SECURITY.md").write_text("Report security issues privately.\n", encoding="utf-8")
    (root / "docs" / "legal" / "IP_POLICY.md").write_text(
        "Do not publish private key material or customer data.\n",
        encoding="utf-8",
    )
    (root / "docs" / "legal" / "PUBLIC_REPO_RISK.md").write_text(
        "Public repo risk policy.\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "This public repository is not open source. See COMMERCIAL_USE.md.\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        'license = { text = "Proprietary" }\n',
        encoding="utf-8",
    )


@pytest.mark.unit
def test_legal_guard_accepts_clean_public_repo(tmp_path: Path) -> None:
    legal_guard = _load_legal_guard()
    _write_clean_public_repo(tmp_path)

    assert legal_guard.check_legal_guard(tmp_path) == []


@pytest.mark.unit
def test_legal_guard_blocks_missing_required_file(tmp_path: Path) -> None:
    legal_guard = _load_legal_guard()
    _write_clean_public_repo(tmp_path)
    (tmp_path / "NOTICE").unlink()

    errors = legal_guard.check_legal_guard(tmp_path)

    assert "missing required legal file: NOTICE" in errors


@pytest.mark.unit
def test_legal_guard_blocks_env_files(tmp_path: Path) -> None:
    legal_guard = _load_legal_guard()
    _write_clean_public_repo(tmp_path)
    (tmp_path / ".env").write_text("KUN_AUTH_SECRET=secret\n", encoding="utf-8")

    errors = legal_guard.check_legal_guard(tmp_path)

    assert any("forbidden sensitive file in public repo: .env" in error for error in errors)


@pytest.mark.unit
def test_legal_guard_blocks_private_key_text(tmp_path: Path) -> None:
    legal_guard = _load_legal_guard()
    _write_clean_public_repo(tmp_path)
    private_key_marker = "BEGIN " + "PRIVATE KEY"
    (tmp_path / "notes.md").write_text(
        f"-----{private_key_marker}-----\nabc\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )

    errors = legal_guard.check_legal_guard(tmp_path)

    assert any("contains forbidden private key material" in error for error in errors)


@pytest.mark.unit
def test_legal_guard_blocks_missing_required_readme_phrase(tmp_path: Path) -> None:
    legal_guard = _load_legal_guard()
    _write_clean_public_repo(tmp_path)
    (tmp_path / "README.md").write_text("KUN project.\n", encoding="utf-8")

    errors = legal_guard.check_legal_guard(tmp_path)

    assert any("README.md missing required phrase" in error for error in errors)
