SPOONOS Agents Directory
========================

This folder hosts agent builders and sub-agent helpers for the SPOONOS server.

How to extend Agent functionality
---------------------------------
1) Add a new agent module
   - Create a new file in this folder (for example, `my_agent.py`).
   - Build a factory that returns a `SpoonReactAI` (or compatible) instance.
   - Keep agent wiring minimal; the main agent is assembled in
     `react_agent.py`.

2) Add sub-agents
   - Use the existing sub-agent pipeline in `sub_agents.py`.
   - Each sub-agent is described by `SubAgentSpec` in
     `spoonos_server/core/schemas.py`.
   - Sub-agents are created via `create_subagents()` and exposed to the main
     agent as a tool (`SubAgentTool`).

How to register with the main agent
-----------------------------------
- Main agent entrypoint: `react_agent.create_react_agent()`.
  - This is the primary assembly point for the system prompt, toolkits, MCP
    tools, and sub-agents.
- To always include a new agent:
  - Add it to `react_agent.py` (for example, append tools or wrap logic before
    returning the agent).
- To include it dynamically as a sub-agent:
  - Pass `sub_agents` in the API request body (see
    `spoonos_server/api/routes/agent.py`).
  - Each item must follow the `SubAgentSpec` schema.

Mirror Battle System
--------------------
This repo adds a "Mirror Battle" workflow (main orchestrator + opp + judge).
Key pieces:
- Orchestrator logic + state: `mirror_battle.py`
- Opponent sub-agent: `opp_agent.py` (JSON output, contrarian stance)
- Judge sub-agent: `judge_agent.py` (strict JSON verdict + blood change)
- API entry: `spoonos_server/api/routes/agent.py` (gate via request.battle)
- Request schema: `spoonos_server/core/schemas.py` (BattleConfig)

How to trigger Mirror Battle
----------------------------
Use the `/v1/agent/stream` endpoint with `battle.enabled=true`.
You can also preselect dimensions:
```json
{
  "message": "我想 All in SOL",
  "stream_mode": "raw",
  "battle": { "enabled": true, "selected_dimensions": ["HowMuch", "Exit"] }
}
```

Local demo script
-----------------
Run:
```
python apps/server/scripts/mirror_battle_demo.py
```
It includes two cases:
1) "All in SOL" with HowMuch + Exit
2) User concedes in round 2 (session_id: battle-demo-concede)

Tips
----
- Keep sub-agent system prompts concise; they are appended at creation time in
  `create_subagents`.
- If the sub-agent needs tools, reuse `toolkits` and `mcp_enabled` in
  `SubAgentSpec` so it matches the main agent’s capabilities.
