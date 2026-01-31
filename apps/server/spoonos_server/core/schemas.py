from typing import List, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant|tool)$")
    content: str


class SubAgentSpec(BaseModel):
    name: str
    system_prompt: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    toolkits: Optional[List[str]] = None
    mcp_enabled: Optional[bool] = None


class BattleConfig(BaseModel):
    enabled: bool = True
    selected_dimensions: Optional[List[str]] = None
    max_rounds_per_dimension: int = 6


class StreamRequest(BaseModel):
    message: Optional[str] = None
    messages: Optional[List[ChatMessage]] = None
    session_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    profile_prompt: Optional[str] = None
    toolkits: Optional[List[str]] = None
    mcp_enabled: Optional[bool] = None
    sub_agents: Optional[List[SubAgentSpec]] = None
    battle: Optional[BattleConfig] = None
    stream_mode: str = Field("sse", pattern="^(sse|raw)$")
    timeout: float = 120.0
