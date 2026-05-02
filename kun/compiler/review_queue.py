"""Bridge compiler intake review packages into Qi / NUO review queues."""

from __future__ import annotations

from typing import Literal

from kun.compiler.intake_review import CompilerReviewPackage
from kun.qi.problem_queue import QiProblemSignal, persist_problem_signals


def compiler_review_package_to_problem_signal(
    *,
    tenant_id: str,
    package: CompilerReviewPackage,
) -> QiProblemSignal:
    """Turn one compiler review package into a review-only Qi signal."""

    ticket = package.as_review_ticket()
    ticket.update(
        {
            "review_only": True,
            "production_action": False,
            "promotion_allowed": False,
            "auto_ingest_allowed": False,
            "queue_intent": "compiler_intake_review_only",
        }
    )
    return QiProblemSignal.build(
        tenant_id=tenant_id,
        category=_signal_category(package),
        severity=_signal_severity(package),
        summary=_signal_summary(package),
        source="compiler.intake_review.package",
        task_type=f"compiler:{package.source.type}:{package.suggested_backend}",
        evidence=ticket,
    )


def compiler_review_packages_to_problem_signals(
    *,
    tenant_id: str,
    packages: list[CompilerReviewPackage],
) -> list[QiProblemSignal]:
    return [
        compiler_review_package_to_problem_signal(tenant_id=tenant_id, package=package)
        for package in packages
    ]


async def enqueue_compiler_review_packages(
    *,
    tenant_id: str,
    packages: list[CompilerReviewPackage],
) -> int:
    signals = compiler_review_packages_to_problem_signals(
        tenant_id=tenant_id,
        packages=packages,
    )
    return await persist_problem_signals(signals)


def _signal_category(package: CompilerReviewPackage) -> Literal["risk", "context"]:
    if package.risk_level == "high" or package.decision in {"blocked", "backend_unavailable"}:
        return "risk"
    return "context"


def _signal_severity(package: CompilerReviewPackage) -> str:
    if package.decision == "blocked":
        return "critical"
    if package.risk_level == "high":
        return "error"
    if package.needs_human_review or package.needs_recompile:
        return "warning"
    return "info"


def _signal_summary(package: CompilerReviewPackage) -> str:
    if package.decision == "compiled_to_asset":
        prefix = "Compiler intake compiled"
    elif package.decision == "blocked":
        prefix = "Compiler intake blocked"
    elif package.decision == "backend_unavailable":
        prefix = "Compiler backend unavailable"
    elif package.needs_recompile:
        prefix = "Compiler intake needs recompile"
    else:
        prefix = "Compiler intake needs review"
    return f"{prefix}: {package.source.uri}"


__all__ = [
    "compiler_review_package_to_problem_signal",
    "compiler_review_packages_to_problem_signals",
    "enqueue_compiler_review_packages",
]
