from __future__ import annotations

import pytest
from kun.compiler import CompilerIntakeRequest, build_compiler_review_package
from kun.compiler.review_queue import compiler_review_package_to_problem_signal


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compiler_review_package_enters_qi_queue_as_review_only_signal() -> None:
    package = await build_compiler_review_package(
        CompilerIntakeRequest(
            tenant_id="tenant-compiler",
            source_type="raw_text",
            value="ok",
        )
    )

    signal = compiler_review_package_to_problem_signal(
        tenant_id="tenant-compiler",
        package=package,
    )

    assert signal.source == "compiler.intake_review.package"
    assert signal.task_type == "compiler:inline:plain"
    assert signal.severity == "warning"
    assert signal.evidence["queue_intent"] == "compiler_intake_review_only"
    assert signal.evidence["review_only"] is True
    assert signal.evidence["production_action"] is False
    assert signal.evidence["auto_ingest_allowed"] is False
