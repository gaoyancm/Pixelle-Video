"""Phase 04-E L2: the Decision Agent.

Toonflow Agent-as-Tool: a system prompt defines the stage machine and the
available sub-agent tools; the LLM picks which tool to call via
function-calling style output, and the decision agent dispatches to the
matching sub-agent. Mock LLM only — no real API.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pixelle_video.orchestration.agents.sub_agents import SubAgent

TOOLS = (
    "run_content_strategist",
    "run_copywriter",
    "run_storyboard_planner",
    "run_supervisor",
)

SYSTEM_PROMPT = (
    "你是内容策划决策 Agent。阶段机：intent_classification → audience_analysis "
    "→ copy_writing → storyboard → supervision。可用工具（function calling）："
    + ", ".join(TOOLS)
    + "。每次只选择一个工具并以 {tool: 工具名, prompt: 给子Agent的上下文} JSON 响应。"
)


class DecisionAgent:
    """Parse the LLM's tool call and dispatch to the right sub-agent."""

    def __init__(
        self,
        sub_agents: dict[str, SubAgent],
        *,
        llm_caller: Callable[[str], Awaitable[str]] | None = None,
    ):
        self.sub_agents = sub_agents
        self.llm_caller = llm_caller

    async def decide(self, request_text: str, context: str = "") -> dict[str, Any]:
        """Ask the LLM which tool to call next; return the dispatch order."""
        if self.llm_caller is None:
            return self._heuristic_dispatch(context)
        prompt = f"{SYSTEM_PROMPT}\n\n用户需求：{request_text}\n当前上下文：{context}"
        reply = await self.llm_caller(prompt)
        return self._parse_tool_call(reply)

    @staticmethod
    def _parse_tool_call(reply: str) -> dict[str, Any]:
        import json
        import re

        match = re.search(r"\{[^}]*\"tool\"[^}]*\}", reply)
        if not match:
            raise ValueError("LLM reply contains no tool call")
        data = json.loads(match.group(0))
        tool = data.get("tool")
        if tool not in TOOLS:
            raise ValueError(f"unknown tool: {tool}")
        return {"tool": tool, "prompt": data.get("prompt", "")}

    @staticmethod
    def _heuristic_dispatch(context: str) -> dict[str, Any]:
        # Deterministic fallback: run every tool in stage order.
        for tool in TOOLS:
            if tool.split("_", 1)[1] not in context:
                return {"tool": tool, "prompt": context}
        return {"tool": "run_supervisor", "prompt": context}

    async def execute_tool(self, tool: str, prompt: str) -> dict[str, Any]:
        sub_agent = self.sub_agents.get(tool)
        if sub_agent is None:
            raise ValueError(f"no sub-agent registered for tool {tool}")
        # Toonflow Agent-as-Tool: the sub-agent receives the tool context.
        result = await sub_agent.run(f"[{tool}] {prompt}")
        return {"tool": tool, "result": result}
