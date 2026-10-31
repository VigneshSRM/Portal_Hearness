"""Agent loop: LLM + tools + a human approval gate for every write."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from .llm import ChatModel
from .tools import Approver, Toolset, deny_all

log = logging.getLogger(__name__)

FALLBACK_SYSTEM_PROMPT = """You are the Redmine Ticketing Automation Agent.
You help with daily overviews, dashboards (SLA, team, performance, time spent), CSV reports,
"assigned to me" lists, closure templates and ticket closure.
Hard rule: never change anything without the user's explicit permission. Write tools ask the
user for approval; if they decline, accept it and do not retry. Never invent ticket data -
always use tools and cite ticket numbers (#123)."""

MAX_TOOL_RESULT_CHARS = 12000


def load_system_prompt(path: str | Path | None) -> str:
    if path:
        p = Path(path)
        if p.exists():
            return p.read_text(encoding="utf-8")
    return FALLBACK_SYSTEM_PROMPT


class AgentSession:
    def __init__(
        self,
        toolset: Toolset,
        llm: ChatModel,
        *,
        system_prompt: str,
        approver: Approver = deny_all,
        max_steps: int = 8,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.toolset = toolset
        self.llm = llm
        self.approver = approver
        self.max_steps = max_steps
        self.on_event = on_event or (lambda *_: None)
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    def ask(self, text: str) -> str:
        self.messages.append({"role": "user", "content": text})
        schemas = self.toolset.schemas()
        for _ in range(self.max_steps):
            message = self.llm.complete(self.messages, schemas)
            tool_calls = message.get("tool_calls") or []
            assistant: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            self.messages.append(assistant)
            if not tool_calls:
                return message.get("content") or ""
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                self.on_event("tool_call", {"name": name, "arguments": raw_args})
                result = self.toolset.invoke(name, raw_args, approver=self.approver)
                content = json.dumps(result, default=str, ensure_ascii=False)
                if len(content) > MAX_TOOL_RESULT_CHARS:
                    content = content[:MAX_TOOL_RESULT_CHARS] + '... [truncated]'
                self.on_event("tool_result", {"name": name, "result": result})
                self.messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": content})
        return "I stopped after reaching the maximum number of steps. Please narrow the request."
