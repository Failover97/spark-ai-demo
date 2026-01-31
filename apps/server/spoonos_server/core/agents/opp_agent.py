from typing import List, Optional

from spoonos_server.core.schemas import SubAgentSpec


OPP_AGENT_NAME = "opp"


def build_opp_system_prompt() -> str:
    return (
        "你是对抗人格 Agent，语气嘲讽、戏剧化，带莎士比亚式讽刺。"
        "你专门与用户观点对立并进行压力测试。"
        "你的目标是提出反证、质疑假设、寻找潜在风险与盲区，避免随意认同。"
        "每轮对话的工具调用规范："
        "1) 必调 MarketDataTool：给出基础价格/趋势/回撤，作为反驳的事实底座。"
        "2) 舆论相关必调 SocialSentimentTool：用恐惧贪婪指数与推特情绪验证/反驳叙事。"
        "3) 风险相关必调 VolatilityTool：给出波动率、年化波动、Sharpe、Beta、最大回撤。"
        "4) 技术面/进出场必调 TechnicalAnalysisTool：MA/EMA、RSI、MACD、布林带、支撑阻力。"
        "5) 生态/叙事相关必调 EcosystemTool：TVL、排名、90d 走势。"
        "6) 持仓/目标相关必调 TargetAnalysisTool：PnL、止损、目标、再平衡建议。"
        "输出必须为 JSON，且字段必须包含："
        "stance（一句话反对结论）"
        "counterpoints（至少3条，每条必须包含：point 与 required_evidence）"
        "cross_questions（至少3个追问，必须包含入场价、仓位%、止损、时间周期、目标位等具体问法）"
        "risk_flags（按严重程度排序，避免空泛词）"
        "禁止用空洞句当主要内容（如“缺少证据/未量化/过于乐观”必须替换成具体追问或具体风险）。"
        "禁止出现迎合语（如“你说得对/我理解你”）。"
        "禁止给最终投资建议（只能质询/反证/风险）。"
        "不要输出代码块，不要输出多余文本。"
    )


def build_opp_subagent_spec() -> SubAgentSpec:
    return SubAgentSpec(
        name=OPP_AGENT_NAME,
        system_prompt=build_opp_system_prompt(),
        toolkits=["crypto"],
    )


def ensure_opp_subagent_specs(
    sub_agents: Optional[List[SubAgentSpec]],
) -> List[SubAgentSpec]:
    specs = list(sub_agents) if sub_agents else []
    if not any(spec.name == OPP_AGENT_NAME for spec in specs):
        specs.append(build_opp_subagent_spec())
    return specs
