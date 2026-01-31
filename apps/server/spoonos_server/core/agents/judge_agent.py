from typing import List, Optional

from spoonos_server.core.schemas import SubAgentSpec


JUDGE_AGENT_NAME = "judge"


def build_judge_system_prompt() -> str:
    return (
        "你是中立法官，只负责裁决与评分，不做角色扮演。"
        "输出必须为严格 JSON，禁止任何额外文本。"
        "字段必须包括："
        "dimension (Why/When/HowMuch/WhatIf/Exit)"
        "should_terminate (true/false)"
        "termination_type (user_concede|user_persist|exhausted|user_collapse|mirror_collapse|consensus|max_rounds|continue)"
        "winner (user|mirror|tie)"
        "scores: {argument_strength:0-10, evidence_quality:0-10, fit_to_user:0-10}"
        "blood_change (int, -20..+20; >0 表示用户血条增加，<0 表示镜像增加)"
        "new_blood: {user:int 0-100, mirror:int 0-100}"
        "reason (简述本轮变化原因)"
        "round_summary (本轮一句话总结)"
    )


def build_judge_subagent_spec() -> SubAgentSpec:
    return SubAgentSpec(
        name=JUDGE_AGENT_NAME,
        system_prompt=build_judge_system_prompt(),
        toolkits=["crypto"],
    )


def ensure_judge_subagent_specs(
    sub_agents: Optional[List[SubAgentSpec]],
) -> List[SubAgentSpec]:
    specs = list(sub_agents) if sub_agents else []
    if not any(spec.name == JUDGE_AGENT_NAME for spec in specs):
        specs.append(build_judge_subagent_spec())
    return specs
