from __future__ import annotations

from kun.lab.ensemble_executor import EnsembleExecutor, EnsemblePathResult


def _path(idx: int, score: float) -> EnsemblePathResult:
    return EnsemblePathResult(
        path_idx=idx,
        config={"strategy": f"s{idx}"},
        output=f"out-{idx}",
        score=score,
    )


def test_ensemble_exploration_default_keeps_best() -> None:
    winner, reason = EnsembleExecutor._select_winner_with_exploration(
        [_path(0, 0.9), _path(1, 0.4)],
        "best_score",
        exploration_rate=0.0,
        seed="x",
    )
    assert winner == 0
    assert reason == "best_score:0.90"


def test_ensemble_exploration_can_choose_non_best_when_forced() -> None:
    winner, reason = EnsembleExecutor._select_winner_with_exploration(
        [_path(0, 0.9), _path(1, 0.8), _path(2, 0.7)],
        "best_score",
        exploration_rate=1.0,
        seed="x",
    )
    assert winner in {1, 2}
    assert "non_best_exploration" in reason
