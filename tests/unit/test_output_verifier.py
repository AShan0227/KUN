"""OutputVerifier 单元测试。"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from kun.security.output_verifier import OutputVerifier


def _completed(
    stdout: str, stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["pytest"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_hash_artifact_returns_sha256_for_relative_path(tmp_path: Path) -> None:
    artifact = tmp_path / "result.txt"
    artifact.write_text("real output\n", encoding="utf-8")

    digest = OutputVerifier(cwd=tmp_path).hash_artifact("result.txt")

    assert digest == hashlib.sha256(b"real output\n").hexdigest()
    assert len(digest) == 64


def test_hash_artifact_raises_for_missing_file(tmp_path: Path) -> None:
    verifier = OutputVerifier(cwd=tmp_path)

    with pytest.raises(FileNotFoundError):
        verifier.hash_artifact("missing.txt")


def test_hash_artifact_rejects_path_traversal(tmp_path: Path) -> None:
    verifier = OutputVerifier(cwd=tmp_path)

    with pytest.raises(ValueError, match="must not contain"):
        verifier.hash_artifact("../secret.txt")


def test_hash_artifact_rejects_absolute_path_outside_cwd(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-output-verifier.txt"
    outside.write_text("secret\n", encoding="utf-8")
    verifier = OutputVerifier(cwd=tmp_path)

    with pytest.raises(ValueError, match="inside cwd"):
        verifier.hash_artifact(str(outside))


def test_verify_diff_accepts_claimed_added_line() -> None:
    verifier = OutputVerifier()

    assert verifier.verify_diff(
        "alpha\n",
        "alpha\nbeta\n",
        ["+beta"],
    )


def test_verify_diff_accepts_claimed_removed_line() -> None:
    verifier = OutputVerifier()

    assert verifier.verify_diff(
        "alpha\nbeta\n",
        "alpha\n",
        ["-beta"],
    )


def test_verify_diff_rejects_claim_not_in_real_diff() -> None:
    verifier = OutputVerifier()

    assert (
        verifier.verify_diff(
            "alpha\n",
            "alpha\nbeta\n",
            ["+gamma"],
        )
        is False
    )


def test_verify_diff_rejects_expected_change_when_files_equal() -> None:
    verifier = OutputVerifier()

    assert verifier.verify_diff("alpha\n", "alpha\n", ["+beta"]) is False


def test_verify_diff_accepts_no_expected_changes_when_no_diff() -> None:
    verifier = OutputVerifier()

    assert verifier.verify_diff("alpha\n", "alpha\n", []) is True


def test_check_pytest_output_accepts_matching_summary(tmp_path: Path) -> None:
    def runner(
        test_file_path: str,
        cwd: Path,
        timeout_sec: float,
    ) -> subprocess.CompletedProcess[str]:
        assert test_file_path == "tests/unit/test_x.py"
        assert cwd == tmp_path
        assert timeout_sec == 60.0
        return _completed(".. 2 passed in 0.01s\n")

    verifier = OutputVerifier(cwd=tmp_path, pytest_runner=runner)

    assert verifier.check_pytest_output(".. 2 passed in 0.02s", "tests/unit/test_x.py")


def test_check_pytest_output_rejects_fabricated_success(tmp_path: Path) -> None:
    def runner(
        _test_file_path: str,
        _cwd: Path,
        _timeout_sec: float,
    ) -> subprocess.CompletedProcess[str]:
        return _completed("F 1 failed in 0.01s\n", returncode=1)

    verifier = OutputVerifier(cwd=tmp_path, pytest_runner=runner)

    assert verifier.check_pytest_output("1 passed in 0.02s", "tests/unit/test_x.py") is False


def test_check_pytest_output_rejects_output_without_summary(tmp_path: Path) -> None:
    verifier = OutputVerifier(cwd=tmp_path, pytest_runner=lambda *_args: _completed("1 passed"))

    assert verifier.check_pytest_output("looks good to me", "tests/unit/test_x.py") is False


def test_check_pytest_output_rejects_when_actual_run_has_no_summary(tmp_path: Path) -> None:
    verifier = OutputVerifier(cwd=tmp_path, pytest_runner=lambda *_args: _completed("oops"))

    assert verifier.check_pytest_output("1 passed in 0.02s", "tests/unit/test_x.py") is False


def test_detect_git_log_answer_leak_by_commit_hash() -> None:
    git_log = "abc1234 add billing transparency\n"
    agent_output = "我参考了 abc1234 的实现。"

    assert OutputVerifier().detect_git_log_answer_leak(agent_output, git_log)


def test_detect_git_log_answer_leak_by_subject_line() -> None:
    git_log = "abc1234 implement output verifier with pytest replay\n"
    agent_output = "这次方案是 implement output verifier with pytest replay。"

    assert OutputVerifier().detect_git_log_answer_leak(agent_output, git_log)


def test_detect_git_log_answer_leak_catches_short_key_phrase() -> None:
    git_log = "abc1234 secret token\n"
    agent_output = "最终答案直接复用了 secret token。"

    assert OutputVerifier().detect_git_log_answer_leak(agent_output, git_log)


def test_detect_git_log_answer_leak_handles_full_git_log_message() -> None:
    git_log = """commit abc1234567890
Author: Dev <dev@example.com>
Date:   Sun Apr 26 10:00:00 2026 +0800

    add deterministic audit hashing for artifacts
"""
    agent_output = "我做了 add deterministic audit hashing for artifacts。"

    assert OutputVerifier().detect_git_log_answer_leak(agent_output, git_log)


def test_detect_git_log_answer_leak_ignores_generic_overlap() -> None:
    git_log = "abc1234 fix bug\n"
    agent_output = "我修了 bug，但没有复制历史提交。"

    assert OutputVerifier().detect_git_log_answer_leak(agent_output, git_log) is False
