import json
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from spoonos_server.core.agents.react_agent import (
    create_react_agent,
    stream_agent_events,
)
from spoonos_server.core.config import load_config
from spoonos_server.core.schemas import ChatMessage, StreamRequest
from spoonos_server.core.agents.mirror_battle import (
    BattleState,
    run_mirror_battle_turn,
    normalize_dimensions,
)


router = APIRouter()
config = load_config()

# In-memory store to allow future session/memory extensions.
SESSION_STORE: Dict[str, List[ChatMessage]] = {}
MIRROR_BATTLE_STORE: Dict[str, BattleState] = {}


def _merge_messages(
    session_id: str, messages: Optional[List[ChatMessage]]
) -> List[ChatMessage]:
    history = SESSION_STORE.get(session_id, [])
    if messages:
        history = history + messages
    SESSION_STORE[session_id] = history
    return history


def _json_default(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    as_dict = getattr(value, "dict", None)
    if callable(as_dict):
        return as_dict()
    return str(value)


def _extract_parts_text(event: Dict[str, Any]) -> str:
    parts = event.get("parts", []) if isinstance(event, dict) else []
    texts = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            texts.append(part.get("text", ""))
    return "\n".join(t for t in texts if t)


@router.post("/v1/agent/stream")
async def stream_agent(request: StreamRequest, http_request: Request) -> StreamingResponse:
    if not request.message and not request.messages:
        raise HTTPException(status_code=400, detail="message or messages required.")

    session_id = request.session_id or str(uuid.uuid4())
    if request.messages:
        _merge_messages(session_id, request.messages)

    user_message = (
        request.message
        if request.message
        else request.messages[-1].content  # type: ignore[index]
    )

    raw_text = (
        http_request.query_params.get("raw_text") == "1"
        or http_request.headers.get("x-raw-text") == "1"
    )

    async def event_stream() -> AsyncIterator[str]:
        if request.battle and request.battle.enabled:
            state = MIRROR_BATTLE_STORE.get(session_id) or BattleState()
            if request.battle.selected_dimensions and state.awaiting_dimension_choice:
                state.selected_dimensions = normalize_dimensions(
                    request.battle.selected_dimensions
                )
                if state.selected_dimensions:
                    state.awaiting_dimension_choice = False
            text, new_state, done = await run_mirror_battle_turn(
                config=config,
                state=state,
                user_message=user_message,
            )
            MIRROR_BATTLE_STORE[session_id] = new_state
            event = {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "parts": [{"type": "text", "text": text, "state": "done"}],
            }
            if raw_text:
                yield _extract_parts_text(event)
                return
            payload = json.dumps(event, ensure_ascii=False, default=_json_default)
            if request.stream_mode == "sse":
                yield f"data: {payload}\n\n"
            else:
                yield payload
            if done:
                MIRROR_BATTLE_STORE.pop(session_id, None)
            return

        agent = create_react_agent(
            config=config,
            system_prompt=request.system_prompt,
            profile_prompt=request.profile_prompt,
            session_id=session_id,
            provider=request.provider,
            model=request.model,
            toolkits=request.toolkits,
            mcp_enabled=request.mcp_enabled,
            sub_agents=request.sub_agents,
        )
        async for event in stream_agent_events(agent, user_message, request.timeout):
            if raw_text:
                text = _extract_parts_text(event)
                if text:
                    yield text
                continue
            payload = json.dumps(event, ensure_ascii=False, default=_json_default)
            if request.stream_mode == "sse":
                yield f"data: {payload}\n\n"
            else:
                yield payload

    media_type = "text/event-stream" if request.stream_mode == "sse" else "text/plain"
    return StreamingResponse(event_stream(), media_type=media_type)


@router.post("/v1/agent")
async def run_agent(request: StreamRequest, http_request: Request) -> JSONResponse:
    if not request.message and not request.messages:
        raise HTTPException(status_code=400, detail="message or messages required.")

    session_id = request.session_id or str(uuid.uuid4())
    if request.messages:
        _merge_messages(session_id, request.messages)

    user_message = (
        request.message
        if request.message
        else request.messages[-1].content  # type: ignore[index]
    )

    raw_text = (
        http_request.query_params.get("raw_text") == "1"
        or http_request.headers.get("x-raw-text") == "1"
    )
    events: List[Dict[str, object]] = []
    if request.battle and request.battle.enabled:
        state = MIRROR_BATTLE_STORE.get(session_id) or BattleState()
        if request.battle.selected_dimensions and state.awaiting_dimension_choice:
            state.selected_dimensions = normalize_dimensions(
                request.battle.selected_dimensions
            )
            if state.selected_dimensions:
                state.awaiting_dimension_choice = False
        text, new_state, done = await run_mirror_battle_turn(
            config=config,
            state=state,
            user_message=user_message,
        )
        MIRROR_BATTLE_STORE[session_id] = new_state
        event = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "parts": [{"type": "text", "text": text, "state": "done"}],
        }
        events.append(event)
        if raw_text:
            return JSONResponse({"text": _extract_parts_text(event)})
        if done:
            MIRROR_BATTLE_STORE.pop(session_id, None)
        return JSONResponse({"events": events})

    agent = create_react_agent(
        config=config,
        system_prompt=request.system_prompt,
        profile_prompt=request.profile_prompt,
        session_id=session_id,
        provider=request.provider,
        model=request.model,
        toolkits=request.toolkits,
        mcp_enabled=request.mcp_enabled,
        sub_agents=request.sub_agents,
    )
    async for event in stream_agent_events(agent, user_message, request.timeout):
        payload = json.loads(
            json.dumps(event, ensure_ascii=False, default=_json_default)
        )
        events.append(payload)
    if raw_text:
        texts = []
        for event in events:
            texts.append(_extract_parts_text(event))
        return JSONResponse({"text": "\n".join(t for t in texts if t)})

    return JSONResponse({"events": events})
