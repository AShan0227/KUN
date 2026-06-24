"""V7 Phase A — 验证 kun.agents.qi 和 kun.agents.nuo re-export 包.

V7 命名约定 (§5.1 / §5.2): 启 (Qi) 和 傩 (Nuo) 是对外角色名, 内部实现保留
StrategistService / SupervisorService. 新代码应该 import qi/nuo, 旧代码继续
可用 strategist/supervisor — 渐进迁移。

本测试验证:
1. kun.agents.qi 包能 import, 关键符号可用
2. kun.agents.nuo 包能 import, 关键符号可用
3. Qi / Nuo 别名指向正确的服务类
4. 新旧路径 re-export 同一对象 (identity check)
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_qi_module_imports() -> None:
    """启 (Qi) re-export 包能 import."""
    from kun.agents import qi

    assert hasattr(qi, "StrategistService")
    assert hasattr(qi, "Qi")  # public alias
    assert hasattr(qi, "StrategyExperiment")
    assert hasattr(qi, "ExplorerMode")
    assert hasattr(qi, "ExplorerPoolConfig")


@pytest.mark.unit
def test_qi_alias_is_strategist_service() -> None:
    """Qi 别名必须指向 StrategistService 类 (V7 §5.1 命名约定)."""
    from kun.agents.qi import Qi, StrategistService

    assert Qi is StrategistService


@pytest.mark.unit
def test_qi_reexports_same_object_as_strategist() -> None:
    """kun.agents.qi.StrategistService 与 kun.agents.strategist.StrategistService 同一对象."""
    from kun.agents import qi, strategist

    assert qi.StrategistService is strategist.StrategistService


@pytest.mark.unit
def test_nuo_module_imports() -> None:
    """傩 (Nuo) re-export 包能 import."""
    from kun.agents import nuo

    assert hasattr(nuo, "SupervisorService")
    assert hasattr(nuo, "Nuo")  # public alias
    assert hasattr(nuo, "PlanReviewService")
    assert hasattr(nuo, "PlanReviewHeartbeat")


@pytest.mark.unit
def test_nuo_alias_is_supervisor_service() -> None:
    """Nuo 别名必须指向 SupervisorService 类 (V7 §5.2 命名约定)."""
    from kun.agents.nuo import Nuo, SupervisorService

    assert Nuo is SupervisorService


@pytest.mark.unit
def test_nuo_reexports_same_object_as_supervisor() -> None:
    """kun.agents.nuo.SupervisorService 与 kun.agents.supervisor.SupervisorService 同一对象."""
    from kun.agents import nuo, supervisor

    assert nuo.SupervisorService is supervisor.SupervisorService


@pytest.mark.unit
def test_qi_nuo_independent_modules() -> None:
    """启傩独立 module path, 互不引用对方内部实现 (V7 §5.4 独立可发布预留)."""
    import kun.agents.nuo
    import kun.agents.qi

    # Both modules import OK and have distinct names
    assert kun.agents.qi.__name__ == "kun.agents.qi"
    assert kun.agents.nuo.__name__ == "kun.agents.nuo"
