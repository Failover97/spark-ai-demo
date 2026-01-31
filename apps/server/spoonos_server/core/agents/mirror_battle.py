import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from spoonos_server.core.schemas import SubAgentSpec
from spoonos_server.core.agents.judge_agent import build_judge_subagent_spec
from spoonos_server.core.agents.opp_agent import build_opp_subagent_spec
from spoonos_server.core.agents.sub_agents import SubAgentTool, create_subagents
from spoonos_server.core.config import AppConfig


DIMENSIONS = ["Why", "When", "HowMuch", "WhatIf", "Exit"]
LOGGER = logging.getLogger("mirror_battle")


@dataclass
class BattleState:
    selected_dimensions: List[str] = field(default_factory=list)
    current_dimension_index: int = 0
    round_in_dimension: int = 0
    blood_user: int = 50
    blood_mirror: int = 50
    conversation_history_by_dimension: Dict[str, List[Dict[str, Any]]] = field(
        default_factory=dict
    )
    last_opp_output: Optional[Dict[str, Any]] = None
    last_judge_output: Optional[Dict[str, Any]] = None
    dimension_results: List[Dict[str, Any]] = field(default_factory=list)
    awaiting_dimension_choice: bool = True


def _normalize_dimension(token: str) -> Optional[str]:
    t = (token or "").strip().lower()
    if t in {"why", "为何", "为什么"}:
        return "Why"
    if t in {"when", "时机", "什么时候", "何时"}:
        return "When"
    if t in {"howmuch", "how much", "仓位", "投入", "投多少"}:
        return "HowMuch"
    if t in {"whatif", "what if", "止损", "错了怎么办", "风险"}:
        return "WhatIf"
    if t in {"exit", "卖出", "退出", "止盈"}:
        return "Exit"
    return None


def parse_dimensions_from_text(text: str) -> List[str]:
    if not text:
        return []
    tokens = []
    lowered = text.lower()
    for key in ["why", "when", "how much", "howmuch", "what if", "whatif", "exit"]:
        if key in lowered:
            tokens.append(key)
    for key in ["为什么", "时机", "仓位", "止损", "退出", "卖出", "投多少"]:
        if key in text:
            tokens.append(key)
    dims = []
    for token in tokens:
        dim = _normalize_dimension(token)
        if dim and dim not in dims:
            dims.append(dim)
    return dims


def normalize_dimensions(dimensions: Optional[List[str]]) -> List[str]:
    if not dimensions:
        return []
    dims: List[str] = []
    for item in dimensions:
        dim = _normalize_dimension(str(item))
        if dim and dim not in dims:
            dims.append(dim)
    return dims


def build_dimension_choice_prompt() -> str:
    return (
        "请选择本次 Mirror Battle 的维度（可多选）：Why / When / HowMuch / WhatIf / Exit。\n"
        "回复示例：Why, WhatIf"
    )


def _safe_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(text)
    except Exception:
        return None


def _opp_schema_hint() -> str:
    return (
        "必须只输出JSON，字段包含："
        "stance, counterpoints(>=3, 每条含point与required_evidence), "
        "cross_questions(>=3), risk_flags(按严重程度排序)。"
    )


def _judge_schema_hint() -> str:
    return (
        "必须只输出JSON，字段包含：dimension, should_terminate, termination_type, "
        "winner, scores{argument_strength,evidence_quality,fit_to_user}, "
        "blood_change, new_blood{user,mirror}, reason, round_summary。"
    )


def _clamp_blood(value: int) -> int:
    return max(0, min(100, value))


def _compute_new_blood(
    blood_user: int, blood_change: int
) -> Tuple[int, int]:
    new_user = _clamp_blood(blood_user + int(blood_change))
    new_mirror = _clamp_blood(100 - new_user)
    return new_user, new_mirror


def _summarize_dimension_result(
    dimension: str, state: BattleState, judge_payload: Dict[str, Any]
) -> Dict[str, Any]:
    return {
        "dimension": dimension,
        "winner": judge_payload.get("winner", "tie"),
        "blood_user": state.blood_user,
        "blood_mirror": state.blood_mirror,
        "reason": judge_payload.get("reason"),
        "round_summary": judge_payload.get("round_summary"),
    }


def _overall_outcome(blood_user: int) -> str:
    if blood_user >= 60:
        return "win"
    if 50 <= blood_user <= 59:
        return "tie"
    if 40 <= blood_user <= 49:
        return "loss"
    return "crush_loss"


def _looks_like_concede(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        token in lowered
        for token in ("你说得对", "我认了", "我认输", "我考虑一下", "先不做了", "放弃")
    )


def _format_report(state: BattleState) -> str:
    outcome = _overall_outcome(state.blood_user)
    lines = [
        "[报告]",
        "",
        "▼",
        "→ 盲点分析",
        "→ 行动建议",
        "",
        f"总体结果：{outcome}（user={state.blood_user} / mirror={state.blood_mirror}）",
    ]
    for item in state.dimension_results:
        lines.append(
            f"- {item.get('dimension')}: winner={item.get('winner')} "
            f"(user={item.get('blood_user')} / mirror={item.get('blood_mirror')})"
        )
    lines.append("盲点分析：请检查是否存在过度自信、忽视止损、仓位过重等问题。")
    lines.append("行动建议：基于你提供的信息补齐证据、限定风险边界、明确退出条件。")
    return "\n".join(lines)


async def run_mirror_battle_turn(
    config: AppConfig,
    state: BattleState,
    user_message: str,
) -> Tuple[str, BattleState, bool]:
    if state.awaiting_dimension_choice:
        selected = parse_dimensions_from_text(user_message)
        if not selected:
            return build_dimension_choice_prompt(), state, False
        state.selected_dimensions = selected
        state.current_dimension_index = 0
        state.round_in_dimension = 0
        state.awaiting_dimension_choice = False

    if state.current_dimension_index >= len(state.selected_dimensions):
        report = _format_report(state)
        return report, state, True

    dimension = state.selected_dimensions[state.current_dimension_index]
    state.round_in_dimension += 1
    history = state.conversation_history_by_dimension.setdefault(dimension, [])

    specs: List[SubAgentSpec] = [
        build_opp_subagent_spec(),
        build_judge_subagent_spec(),
    ]
    subagents = create_subagents(specs, config)
    subagent_tool = SubAgentTool(subagents) if SubAgentTool else None
    if "opp" not in subagents or "judge" not in subagents:
        return "子 agent 未就绪，请检查配置。", state, False

    opp_request = json.dumps(
        {
            "dimension": dimension,
            "user_message": user_message,
            "blood": {"user": state.blood_user, "mirror": state.blood_mirror},
            "history": history[-3:],
            "format": _opp_schema_hint(),
        },
        ensure_ascii=False,
    )
    LOGGER.info("CALL_OPP start dimension=%s round=%s", dimension, state.round_in_dimension)
    if subagent_tool:
        opp_payload = await subagent_tool.execute(
            name="opp",
            message=opp_request,
            description="Mirror battle: generate opposition response",
        )
    else:
        opp_payload = await subagents["opp"].run(request=opp_request)
    LOGGER.info(
        "CALL_OPP end len=%s preview=%s",
        len(str(opp_payload)),
        str(opp_payload)[:120],
    )
    opp_json = _safe_json(
        opp_payload if isinstance(opp_payload, str) else str(opp_payload)
    )
    if not isinstance(opp_json, dict):
        retry_request = json.dumps(
            {
                "dimension": dimension,
                "user_message": user_message,
                "blood": {"user": state.blood_user, "mirror": state.blood_mirror},
                "history": history[-3:],
                "format": _opp_schema_hint(),
                "strict": "JSON_ONLY",
            },
            ensure_ascii=False,
        )
        LOGGER.info("CALL_OPP retry start")
        if subagent_tool:
            opp_payload = await subagent_tool.execute(
                name="opp",
                message=retry_request,
                description="Mirror battle: retry opp JSON",
            )
        else:
            opp_payload = await subagents["opp"].run(request=retry_request)
        LOGGER.info(
            "CALL_OPP retry end len=%s preview=%s",
            len(str(opp_payload)),
            str(opp_payload)[:120],
        )
        opp_json = _safe_json(
            opp_payload if isinstance(opp_payload, str) else str(opp_payload)
        )
    if not isinstance(opp_json, dict):
        opp_json = {
            "stance": "观点不足以成立。",
            "counterpoints": ["缺少证据。", "风险未被量化。", "假设过于乐观。"],
            "cross_questions": ["你依据的具体数据是什么？", "失败的边界条件是什么？", "你的止损计划在哪里？"],
            "risk_flags": ["信息不足", "风险未量化"],
        }

    judge_request = json.dumps(
        {
            "dimension": dimension,
            "round": state.round_in_dimension,
            "user_message": user_message,
            "opp_output": opp_json,
            "blood": {"user": state.blood_user, "mirror": state.blood_mirror},
            "history": history[-3:],
            "format": _judge_schema_hint(),
        },
        ensure_ascii=False,
    )
    LOGGER.info("CALL_JUDGE start dimension=%s round=%s", dimension, state.round_in_dimension)
    if subagent_tool:
        judge_payload = await subagent_tool.execute(
            name="judge",
            message=judge_request,
            description="Mirror battle: judge round decision",
        )
    else:
        judge_payload = await subagents["judge"].run(request=judge_request)
    LOGGER.info(
        "CALL_JUDGE end len=%s preview=%s",
        len(str(judge_payload)),
        str(judge_payload)[:200],
    )
    judge_json = _safe_json(
        judge_payload if isinstance(judge_payload, str) else str(judge_payload)
    )
    if not isinstance(judge_json, dict):
        retry_request = json.dumps(
            {
                "dimension": dimension,
                "round": state.round_in_dimension,
                "user_message": user_message,
                "opp_output": opp_json,
                "blood": {"user": state.blood_user, "mirror": state.blood_mirror},
                "history": history[-3:],
                "format": _judge_schema_hint(),
                "strict": "JSON_ONLY",
            },
            ensure_ascii=False,
        )
        LOGGER.info("CALL_JUDGE retry start")
        if subagent_tool:
            judge_payload = await subagent_tool.execute(
                name="judge",
                message=retry_request,
                description="Mirror battle: retry judge JSON",
            )
        else:
            judge_payload = await subagents["judge"].run(request=retry_request)
        LOGGER.info(
            "CALL_JUDGE retry end len=%s preview=%s",
            len(str(judge_payload)),
            str(judge_payload)[:200],
        )
        judge_json = _safe_json(
            judge_payload if isinstance(judge_payload, str) else str(judge_payload)
        )
    if not isinstance(judge_json, dict):
        judge_json = {}

    if _looks_like_concede(user_message):
        judge_json.setdefault("should_terminate", True)
        judge_json.setdefault("termination_type", "user_concede")
        judge_json.setdefault("winner", "mirror")
        judge_json.setdefault("blood_change", -10)
        judge_json.setdefault("reason", "用户表达退让/认同，镜像获胜。")
        judge_json.setdefault("round_summary", "用户让步，本轮判定镜像占优。")

    blood_change = int(judge_json.get("blood_change", 0) or 0)
    new_blood = judge_json.get("new_blood")
    if isinstance(new_blood, dict):
        state.blood_user = _clamp_blood(int(new_blood.get("user", state.blood_user)))
        state.blood_mirror = _clamp_blood(
            int(new_blood.get("mirror", state.blood_mirror))
        )
    else:
        state.blood_user, state.blood_mirror = _compute_new_blood(
            state.blood_user, blood_change
        )

    state.last_opp_output = opp_json
    state.last_judge_output = judge_json

    history.append(
        {
            "round": state.round_in_dimension,
            "user": user_message,
            "opp": opp_json,
            "judge": judge_json,
            "blood_user": state.blood_user,
            "blood_mirror": state.blood_mirror,
        }
    )

    should_terminate = bool(judge_json.get("should_terminate"))
    if state.round_in_dimension >= 6:
        should_terminate = True
        judge_json["termination_type"] = "max_rounds"

    if should_terminate:
        state.dimension_results.append(
            _summarize_dimension_result(dimension, state, judge_json)
        )
        state.current_dimension_index += 1
        state.round_in_dimension = 0

    opp_questions = opp_json.get("cross_questions") or []
    next_question = opp_questions[0] if opp_questions else "请补充更明确的数据或边界条件。"
    counterpoints = opp_json.get("counterpoints", [])
    counterpoints_text = "; ".join(counterpoints) if counterpoints else "暂无可用反证。"
    response = (
        f"【维度】{dimension}\n"
        f"【血条】user={state.blood_user} / mirror={state.blood_mirror}\n"
        f"【镜像立场】{opp_json.get('stance')}\n"
        f"【反证要点】{counterpoints_text}\n"
        f"【下一问】{next_question}"
    )

    if should_terminate and state.current_dimension_index >= len(state.selected_dimensions):
        report = _format_report(state)
        response = f"{response}\n\n{report}"
        return response, state, True

    return response, state, False
