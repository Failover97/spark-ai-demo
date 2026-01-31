import asyncio
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from spoon_ai.agents.spoon_react import SpoonReactAI
from spoon_ai.chat import ChatBot
from spoon_ai.tools import ToolManager

from spoonos_server.core.config import AppConfig
from spoonos_server.core.prompt import system_prompt
from spoonos_server.core.mcp.loader import load_mcp_tools
from spoonos_server.core.schemas import SubAgentSpec
from spoonos_server.core.tools.toolkits import (
    available_toolkits,
    load_toolkits,
    resolve_toolkits,
)
from spoonos_server.core.tools.tool_call_wrapper import wrap_tools_for_calls
from spoonos_server.core.agents.sub_agents import SubAgentTool, create_subagents
from spoonos_server.core.agents.judge_agent import ensure_judge_subagent_specs
from spoonos_server.core.agents.opp_agent import ensure_opp_subagent_specs


DEFAULT_SYSTEM_PROMPT = system_prompt


def _build_tool_list(tool_manager: ToolManager) -> str:
    tool_map = getattr(tool_manager, "tool_map", None)
    if not tool_map:
        return "- (no tools loaded)"
    lines = []
    for tool in tool_map.values():
        desc = getattr(tool, "description", "") or ""
        lines.append(f"- {tool.name}: {desc}")
    return "\n".join(lines)


def create_react_agent(
    config: AppConfig,
    system_prompt: Optional[str],
    profile_prompt: Optional[str],
    session_id: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    toolkits: Optional[List[str]],
    mcp_enabled: Optional[bool],
    sub_agents: Optional[List[SubAgentSpec]],
) -> SpoonReactAI:
    selected_toolkits = resolve_toolkits(toolkits, config.toolkits.default_toolkits)
    tools = load_toolkits(selected_toolkits)

    if mcp_enabled is None:
        mcp_enabled = config.mcp.enabled
    if mcp_enabled:
        tools.extend(load_mcp_tools(config.mcp))

    sub_agents = ensure_judge_subagent_specs(sub_agents)
    sub_agents = ensure_opp_subagent_specs(sub_agents)
    subagent_map = create_subagents(sub_agents, config)
    subagent_tool = None
    if subagent_map and SubAgentTool:
        subagent_tool = SubAgentTool(subagent_map)
        tools.append(subagent_tool)

    tools, tool_wrappers = wrap_tools_for_calls(
        tools, context={"session_id": session_id} if session_id else None
    )

    tool_manager = ToolManager(tools)
    tool_list = _build_tool_list(tool_manager)
    toolkit_list = ", ".join(selected_toolkits) if selected_toolkits else "(none)"
    available_list = ", ".join(available_toolkits())
    mcp_status = "enabled" if mcp_enabled else "disabled"
    mcp_servers = (
        ", ".join(server.name for server in config.mcp.servers)
        if config.mcp.servers
        else "(none)"
    )

    prompt = DEFAULT_SYSTEM_PROMPT
    if system_prompt:
        prompt = f"{prompt}\n\nUser request prompt:\n{system_prompt.strip()}"
    if profile_prompt:
        prompt = f"{prompt}\n\nUser profile context:\n{profile_prompt.strip()}"
    prompt = (
        f"{prompt}\n\nEnabled toolkits: {toolkit_list}"
        f"\nAvailable toolkits: {available_list}"
        f"\nMCP status: {mcp_status} (servers: {mcp_servers})"
        f"\n\nAvailable tools:\n{tool_list}"
    )

    llm_kwargs = {
        "llm_provider": provider or config.llm.provider,
        "model_name": model or config.llm.model,
        "api_key": config.llm.api_key,
        "base_url": config.llm.base_url,
        "stream": True,
    }

    agent = SpoonReactAI(
        name="spoon_react_server",
        llm=ChatBot(**llm_kwargs),
        tools=tool_manager,
        max_steps=8,
    )
    agent.system_prompt = prompt
    async def emit(ai_message: Dict[str, Any]) -> None:
        await agent.output_queue.put(ai_message)

    for wrapper in tool_wrappers:
        wrapper.set_emit(emit)

    if subagent_tool:
        subagent_tool.set_emit(emit)
    return agent


def _looks_like_ai_message(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("id"), str)
        and isinstance(payload.get("role"), str)
        and isinstance(payload.get("parts"), list)
    )


def _serialize_chunk(chunk: Any) -> Dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk
    if isinstance(chunk, str):
        return {"delta": chunk}
    delta = getattr(chunk, "delta", None)
    if delta is not None:
        return {"delta": delta}
    content = getattr(chunk, "content", None)
    if content is not None:
        return {"delta": content}
    return {"delta": str(chunk)}


def _build_text_message(message_id: str, text: str, state: str) -> Dict[str, Any]:
    return {
        "id": message_id,
        "role": "assistant",
        "parts": [
            {"type": "text", "text": text, "state": state},
        ],
    }


def _build_judge_note_text(speaker: str, message: str) -> str:
    if not message:
        return "【法官旁注】未捕捉到有效内容，暂不作出理由。"
    lowered = message.lower()
    has_trade_intent = any(
        keyword in lowered
        for keyword in ("买", "卖", "抄底", "梭哈", "加仓", "减仓", "止损", "止盈")
    )
    has_uncertainty = any(
        keyword in lowered for keyword in ("可能", "不确定", "感觉", "猜", "赌", "也许")
    )
    has_meme = "meme" in lowered or "土狗" in message
    reason_parts = []
    if has_trade_intent:
        reason_parts.append("出现交易动作倾向，需要判断动机与风险")
    if has_uncertainty:
        reason_parts.append("表达含糊或不确定，结论稳定性不足")
    if has_meme:
        reason_parts.append("涉及高波动标的，风险优先级上调")
    if not reason_parts:
        reason_parts.append("信息量有限，优先要求补充依据与边界条件")
    reason = "；".join(reason_parts)
    return f"【法官旁注】{speaker}发言逻辑判断：{reason}。"


async def stream_agent_events(
    agent: SpoonReactAI, user_message: str, timeout: float
) -> AsyncIterator[Dict[str, Any]]:
    agent.clear()
    agent.task_done.clear()

    original_timeout = getattr(agent, "_default_timeout", timeout)
    agent._default_timeout = timeout

    async def run_and_signal() -> str:
        try:
            return await agent.run(request=user_message)
        finally:
            agent.task_done.set()

    async def stream_llm_tokens() -> None:
        llm = getattr(agent, "llm", None)
        if not llm or not hasattr(llm, "astream"):
            return
        try:
            async for chunk in llm.astream(
                messages=[{"role": "user", "content": user_message}],
                system_msg=getattr(agent, "system_prompt", None),
            ):
                delta = getattr(chunk, "delta", None)
                if delta is None:
                    delta = getattr(chunk, "content", None)
                if delta is None:
                    delta = str(chunk)
                if delta:
                    await agent.output_queue.put({"delta": str(delta)})
        except Exception:
            return

    run_task = asyncio.create_task(run_and_signal())
    token_task = asyncio.create_task(stream_llm_tokens())
    queue = agent.output_queue
    message_id = str(uuid.uuid4())
    buffer = ""

    try:
        user_note_id = str(uuid.uuid4())
        user_note = _build_judge_note_text("用户", user_message)
        yield _build_text_message(user_note_id, user_note, "done")

        while True:
            if agent.task_done.is_set() and queue.empty():
                break
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.5)
                if _looks_like_ai_message(chunk):
                    yield chunk
                    continue
                payload = _serialize_chunk(chunk)
                delta = (
                    payload.get("delta")
                    or payload.get("content")
                    or payload.get("text")
                )
                if delta is not None:
                    buffer += str(delta)
                    yield _build_text_message(message_id, buffer, "streaming")
            except asyncio.TimeoutError:
                if run_task.done():
                    break

        result = await run_task
        token_task.cancel()
        if result:
            buffer = str(result)
        if buffer:
            yield _build_text_message(message_id, buffer, "done")
            judge_note_id = str(uuid.uuid4())
            judge_note = _build_judge_note_text("AI", buffer)
            yield _build_text_message(judge_note_id, judge_note, "done")
    finally:
        agent._default_timeout = original_timeout
