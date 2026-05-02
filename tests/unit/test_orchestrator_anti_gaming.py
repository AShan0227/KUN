"""V2.3 Wire 53 (C72): orchestrator post-step AntiGaming check."""

from __future__ import annotations

from kun.engineering.orchestrator import Orchestrator
from kun.security.anti_gaming import AntiGamingDetector, GamingFinding


def test_orchestrator_init_with_anti_gaming() -> None:
    det = AntiGamingDetector()
    orch = Orchestrator(anti_gaming_detector=det)
    assert orch.anti_gaming_detector is det


def test_anti_gaming_detector_check_copy_prompt() -> None:
    det = AntiGamingDetector()
    finding = det.check(prompt="What is 1+1?", answer="What is 1+1?")
    assert finding is not None
    assert finding.pattern == "copy_prompt"


def test_anti_gaming_detector_check_clean_passes() -> None:
    det = AntiGamingDetector(off_topic_threshold=0.05)
    finding = det.check(
        prompt="Calculate one plus one",
        answer="One plus one equals two by addition",
        planned_steps=2,
        actual_steps=2,
        has_assets=True,
    )
    assert finding is None


def test_anti_gaming_detector_check_skip_step() -> None:
    det = AntiGamingDetector(skip_step_threshold=0.4)
    finding = det.check(planned_steps=10, actual_steps=2)
    assert finding is not None
    assert finding.pattern == "skip_step"


def test_gaming_finding_severity_field() -> None:
    finding = GamingFinding(
        pattern="copy_prompt",
        confidence=0.95,
        reason="copied",
        severity="high",
        evidence={"sim": 0.95},
    )
    assert finding.pattern == "copy_prompt"
    assert finding.severity == "high"
    assert finding.evidence["sim"] == 0.95
