"""Minimal OpenAI-compatible Chat Completions client (no SDK dependency).

Works with any endpoint implementing ``POST {base_url}/chat/completions`` with tool
calling: OpenAI, Azure OpenAI (v1 API or deployments + ``api-version``), Ollama
(``http://localhost:11434/v1``), vLLM, LM Studio, OpenRouter, Groq, and the
OpenAI-compatible endpoints offered by Google Gemini and Anthropic.
"""

from __future__ import annotations

from typing import Any, Protocol

import requests

from ..config import LLMConfig
from ..errors import HarnessError


class ChatModel(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        ...


class OpenAICompatibleChat:
    def __init__(self, cfg: LLMConfig, session: requests.Session | None = None) -> None:
        if not cfg.base_url or not cfg.model:
            raise HarnessError("LLM is not configured: set LLM_BASE_URL, LLM_MODEL and LLM_API_KEY.")
        self.cfg = cfg
        self.session = session or requests.Session()

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", **self.cfg.headers}
        if self.cfg.api_key and not any(h.lower() in ("authorization", "api-key") for h in self.cfg.headers):
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        body: dict[str, Any] = {"model": self.cfg.model, "messages": messages, "temperature": self.cfg.temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        try:
            resp = self.session.post(url, json=body, headers=headers, params=self.cfg.query_params or None,
                                     timeout=self.cfg.timeout)
        except requests.RequestException as exc:
            raise HarnessError(f"Could not reach the LLM endpoint {self.cfg.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            raise HarnessError(f"LLM endpoint returned HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise HarnessError(f"Unexpected LLM response: {resp.text[:300]}") from exc
        return message
