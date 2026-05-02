"""V2.3 Seed Protocols — 默认 5 个 stable 协议.

V2.3 启始时 ProtocolRegistry 是空的, 用户跑 dogfood 之前看不到任何 protocol.
这里提供 5 个初始 stable 协议作为 "starter pack", 让 KUN 一启动就有协议给
orchestrator 消费.

5 个 protocol 覆盖最常见 task_type:
- writing.creative.short — 短文写作 (slogan/标题/朋友圈)
- writing.long_form — 长文写作 (报告/文档/邮件)
- coding.python.fastapi — FastAPI 编程
- decision.product — 产品决策 (A vs B)
- research.summarize — 研究/资料总结

这些是"经验值"协议, 不是涌现的. 启窗口跑探索后会涌现 experimental 协议
(自动 promote 后会替代/补充这些).
"""

from __future__ import annotations

from kun.qi.protocol import (
    Protocol,
    ProtocolExecution,
    ProtocolHermesTemplate,
    ProtocolRegistry,
    ProtocolSkillStep,
    ProtocolTrigger,
    ProtocolVerificationSpec,
)


def _writing_creative_short() -> Protocol:
    return Protocol(
        protocol_id="writing.creative.short",
        version="0.1.0-seed",
        tenant_id="u-sylvan",
        status="stable",
        trigger=ProtocolTrigger(
            task_type_pattern="writing.creative.*",
            complexity_score_min=0.0,
            complexity_score_max=0.5,
            risk_levels=["low", "medium"],
        ),
        execution=ProtocolExecution(
            mode="SMART",
            llm_strategy="tier_strong_mid_temp",
            max_steps=2,
            expected_cost_usd=0.02,
            expected_duration_sec=10.0,
        ),
        hermes_template=ProtocolHermesTemplate(
            system_prompt_addon=(
                "短文创作: 优先简洁有力. 控字数. 避免陈词滥调. "
                "输出前自检: 是否突出了卖点 / 是否符合调性."
            ),
            action_type_preference=["direct_llm"],
        ),
        verification=[
            ProtocolVerificationSpec(
                kind="exact_output",
                spec={"min_length_chars": 5, "max_length_chars": 200},
                required=True,
            ),
        ],
        created_by="seed",
        metadata={"description": "短文/slogan/标题创作 (≤200 字符)"},
    )


def _writing_long_form() -> Protocol:
    return Protocol(
        protocol_id="writing.long_form",
        version="0.1.0-seed",
        tenant_id="u-sylvan",
        status="stable",
        trigger=ProtocolTrigger(
            task_type_pattern="writing.long_form.*",
            complexity_score_min=0.3,
            complexity_score_max=1.0,
            risk_levels=["low", "medium", "high"],
        ),
        execution=ProtocolExecution(
            mode="MAX",
            llm_strategy="chain_of_thought",
            max_steps=5,
            expected_cost_usd=0.10,
            expected_duration_sec=60.0,
        ),
        hermes_template=ProtocolHermesTemplate(
            system_prompt_addon=(
                "长文写作: 先列大纲再展开. 每段开头要主题句. 结尾要有结论. 总长度按用户要求."
            ),
            action_type_preference=["direct_llm", "use_memory"],
        ),
        verification=[
            ProtocolVerificationSpec(
                kind="exact_output",
                spec={"min_length_chars": 500},
                required=True,
            ),
        ],
        created_by="seed",
        metadata={"description": "长文/报告/邮件写作 (500+ 字符)"},
    )


def _coding_python_fastapi() -> Protocol:
    return Protocol(
        protocol_id="coding.python.fastapi",
        version="0.1.0-seed",
        tenant_id="u-sylvan",
        status="stable",
        trigger=ProtocolTrigger(
            task_type_pattern="coding.python.*",
            complexity_score_min=0.2,
            complexity_score_max=1.0,
            risk_levels=["low", "medium", "high"],
        ),
        execution=ProtocolExecution(
            mode="MAX",
            llm_strategy="tier_top_low_temp",
            max_steps=8,
            expected_cost_usd=0.15,
            expected_duration_sec=120.0,
        ),
        skill_chain=[
            ProtocolSkillStep(skill="reader", when="task需读现有代码", timeout_sec=30),
            ProtocolSkillStep(skill="python_exec", when="需测试代码", timeout_sec=60),
        ],
        hermes_template=ProtocolHermesTemplate(
            system_prompt_addon=(
                "FastAPI 编程: 先 type hint, 再实现. 用 async/await. "
                "返 Pydantic models. 错误处理用 HTTPException. 写完跑 mypy/pytest."
            ),
            action_type_preference=["use_skill"],
        ),
        verification=[
            ProtocolVerificationSpec(kind="lint_pass", spec={"linter": "ruff"}, required=True),
            ProtocolVerificationSpec(kind="lint_pass", spec={"linter": "mypy"}, required=False),
        ],
        created_by="seed",
        metadata={"description": "FastAPI / Python 后端编程"},
    )


def _decision_product() -> Protocol:
    return Protocol(
        protocol_id="decision.product",
        version="0.1.0-seed",
        tenant_id="u-sylvan",
        status="stable",
        trigger=ProtocolTrigger(
            task_type_pattern="decision.*",
            complexity_score_min=0.1,
            complexity_score_max=0.9,
            risk_levels=["low", "medium", "high"],
        ),
        execution=ProtocolExecution(
            mode="ENSEMBLE",
            llm_strategy="multi_path",
            max_steps=4,
            expected_cost_usd=0.20,
            expected_duration_sec=60.0,
        ),
        hermes_template=ProtocolHermesTemplate(
            system_prompt_addon=(
                "决策类: 先列选项 + 维度. 每维度逐对比. "
                "结论给推荐 + 反对者意见 + 触发条件 (什么情况下改主意)."
            ),
            action_type_preference=["direct_llm", "use_memory"],
        ),
        verification=[
            ProtocolVerificationSpec(
                kind="exact_output",
                spec={"min_length_chars": 200, "must_contain_any": ["建议", "推荐", "结论"]},
                required=True,
            ),
        ],
        created_by="seed",
        metadata={"description": "产品/技术 A vs B 决策"},
    )


def _research_summarize() -> Protocol:
    return Protocol(
        protocol_id="research.summarize",
        version="0.1.0-seed",
        tenant_id="u-sylvan",
        status="stable",
        trigger=ProtocolTrigger(
            task_type_pattern="research.*",
            complexity_score_min=0.0,
            complexity_score_max=1.0,
            risk_levels=["low", "medium"],
        ),
        execution=ProtocolExecution(
            mode="SMART",
            llm_strategy="chain_of_thought",
            max_steps=3,
            expected_cost_usd=0.05,
            expected_duration_sec=30.0,
        ),
        skill_chain=[
            ProtocolSkillStep(skill="reader", when="需读外部资料", timeout_sec=20),
            ProtocolSkillStep(skill="web_search", when="资料不足", timeout_sec=30),
        ],
        hermes_template=ProtocolHermesTemplate(
            system_prompt_addon=(
                "研究/总结: 先 bullet 关键事实. 然后 narrative 结构化总结. "
                "标 source. 主观判断单独 ' >> 我的看法' 分块."
            ),
            action_type_preference=["use_skill", "use_memory"],
        ),
        verification=[
            ProtocolVerificationSpec(
                kind="exact_output",
                spec={"min_length_chars": 100},
                required=True,
            ),
        ],
        created_by="seed",
        metadata={"description": "资料/研究总结"},
    )


def get_seed_protocols() -> list[Protocol]:
    """5 个初始 stable 协议. ProtocolRegistry 启动时 seed 进去."""
    return [
        _writing_creative_short(),
        _writing_long_form(),
        _coding_python_fastapi(),
        _decision_product(),
        _research_summarize(),
    ]


async def seed_default_protocols(registry: ProtocolRegistry) -> int:
    """把 5 个 seed 协议 save 到 registry. 返成功数. 已存在 (按 protocol_id+version)
    则跳过, 不覆盖.
    """
    seeded = 0
    for proto in get_seed_protocols():
        existing = await registry.get(proto.tenant_id, proto.protocol_id, proto.version)
        if existing is not None:
            continue
        await registry.save(proto)
        seeded += 1
    return seeded


__all__ = ["get_seed_protocols", "seed_default_protocols"]
