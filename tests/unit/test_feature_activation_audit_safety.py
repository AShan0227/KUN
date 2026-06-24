"""Output-dir safety for the feature-activation audit (audit F018).

_prepare_audit_output_dir must refuse to wipe unsafe / unknown targets and only
clear directories it owns (empty, or carrying the audit marker).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kun.control_plane.feature_activation_audit import (
    _AUDIT_MARKER,
    _prepare_audit_output_dir,
)


@pytest.mark.unit
def test_creates_fresh_dir(tmp_path: Path) -> None:
    root = (tmp_path / "audit-out").resolve()
    _prepare_audit_output_dir(root)
    assert root.is_dir()
    assert list(root.iterdir()) == []


@pytest.mark.unit
def test_wipes_prior_audit_dir(tmp_path: Path) -> None:
    root = (tmp_path / "audit-out").resolve()
    root.mkdir()
    (root / _AUDIT_MARKER).write_text("{}", encoding="utf-8")
    (root / "old-case.json").write_text("stale", encoding="utf-8")
    _prepare_audit_output_dir(root)
    # prior audit dir is reset to a clean slate
    assert root.is_dir()
    assert list(root.iterdir()) == []


@pytest.mark.unit
def test_empty_existing_dir_is_ok(tmp_path: Path) -> None:
    root = (tmp_path / "audit-out").resolve()
    root.mkdir()
    _prepare_audit_output_dir(root)
    assert root.is_dir()


@pytest.mark.unit
def test_refuses_non_audit_nonempty_dir_and_keeps_data(tmp_path: Path) -> None:
    root = (tmp_path / "precious").resolve()
    root.mkdir()
    keep = root / "keep.txt"
    keep.write_text("do not delete me", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty non-audit"):
        _prepare_audit_output_dir(root)
    # the user's data must survive
    assert keep.exists()
    assert keep.read_text(encoding="utf-8") == "do not delete me"


@pytest.mark.unit
def test_refuses_home(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        _prepare_audit_output_dir(Path.home().resolve())


@pytest.mark.unit
def test_refuses_cwd_and_ancestors() -> None:
    cwd = Path.cwd().resolve()
    with pytest.raises(ValueError, match="unsafe"):
        _prepare_audit_output_dir(cwd)
    with pytest.raises(ValueError, match="unsafe"):
        _prepare_audit_output_dir(cwd.parent)


@pytest.mark.unit
def test_refuses_shallow_path() -> None:
    with pytest.raises(ValueError, match=r"shallow|unsafe"):
        _prepare_audit_output_dir(Path("/x"))
