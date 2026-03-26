"""LLM provider abstraction layer.

Supports Gemini, Anthropic, and a mock provider for testing.
Provider is selected via configuration and is swappable without changing loop logic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class LLMResponse:
    content: str
    model: str
    provider: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for LLM provider implementations."""

    name: str

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        json_mode: bool = False,
    ) -> LLMResponse: ...


class MockProvider:
    """Mock LLM provider for testing. Returns configurable canned responses."""

    name = "mock"

    def __init__(self, responses: list[str] | None = None):
        self._responses = responses or ["Mock LLM response"]
        self._call_count = 0
        self.call_log: list[dict[str, Any]] = []

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        json_mode: bool = False,
    ) -> LLMResponse:
        start = time.monotonic()
        response_text = self._responses[self._call_count % len(self._responses)]
        self._call_count += 1
        elapsed = (time.monotonic() - start) * 1000

        call_record = {
            "system_prompt": system_prompt,
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
        }
        self.call_log.append(call_record)

        return LLMResponse(
            content=response_text,
            model="mock-model",
            provider="mock",
            tokens_in=len(system_prompt) // 4,
            tokens_out=len(response_text) // 4,
            latency_ms=elapsed,
        )


class GeminiProvider:
    """Google Gemini LLM provider. Requires GEMINI_API_KEY environment variable."""

    name = "gemini"

    def __init__(self, model: str = "gemini-2.5-pro"):
        self.model = model
        # Actual initialization deferred to first call to avoid import errors
        # when google-genai is not installed (e.g., during testing with MockProvider)
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            import os

            from google import genai

            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY environment variable is required")
            self._client = genai.Client(api_key=api_key)

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        json_mode: bool = False,
    ) -> LLMResponse:
        self._ensure_client()
        start = time.monotonic()

        contents = []
        for msg in messages:
            contents.append({"role": msg.get("role", "user"), "parts": [{"text": msg["content"]}]})

        config: dict[str, Any] = {
            "system_instruction": system_prompt,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if json_mode:
            config["response_mime_type"] = "application/json"

        response = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )

        elapsed = (time.monotonic() - start) * 1000

        text = ""
        try:
            text = response.text or ""
        except ValueError:
            pass

        if not text:
            import sys
            candidates = getattr(response, "candidates", None)
            if candidates:
                candidate = candidates[0]
                finish_reason = getattr(candidate, "finish_reason", "UNKNOWN")
                content_obj = getattr(candidate, "content", None)
                parts = getattr(content_obj, "parts", None) or []
                part_texts = []
                for part in parts:
                    t = getattr(part, "text", None)
                    if t:
                        part_texts.append(t)
                if part_texts:
                    text = chr(10).join(part_texts)
                else:
                    print(
                        f">>> [GEMINI-DIAG] Empty response. "
                        f"finish_reason={finish_reason}, "
                        f"parts_count={len(parts)}, "
                        f"content_obj={type(content_obj).__name__}",
                        file=sys.stderr,
                    )
            else:
                print(
                    f">>> [GEMINI-DIAG] No candidates in response.",
                    file=sys.stderr,
                )

        usage = getattr(response, "usage_metadata", None)
        tokens_in = getattr(usage, "prompt_token_count", 0) or 0 if usage else 0
        tokens_out = getattr(usage, "candidates_token_count", 0) or 0 if usage else 0

        return LLMResponse(
            content=text,
            model=self.model,
            provider="gemini",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=elapsed,
        )


class AnthropicProvider:
    """Anthropic Claude LLM provider. Requires ANTHROPIC_API_KEY environment variable."""

    name = "anthropic"

    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        self.model = model
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            import os

            import anthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY environment variable is required")
            self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        json_mode: bool = False,
    ) -> LLMResponse:
        self._ensure_client()
        start = time.monotonic()

        formatted_messages = [
            {"role": msg.get("role", "user"), "content": msg["content"]} for msg in messages
        ]

        response = await self._client.messages.create(
            model=self.model,
            system=system_prompt,
            messages=formatted_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        elapsed = (time.monotonic() - start) * 1000
        text = response.content[0].text if response.content else ""

        return LLMResponse(
            content=text,
            model=self.model,
            provider="anthropic",
            tokens_in=response.usage.input_tokens if response.usage else 0,
            tokens_out=response.usage.output_tokens if response.usage else 0,
            latency_ms=elapsed,
        )


def create_provider(provider_name: str, model: str | None = None) -> LLMProvider:
    """Factory function to create an LLM provider by name."""
    providers = {
        "gemini": lambda: GeminiProvider(model=model or "gemini-2.5-pro"),
        "anthropic": lambda: AnthropicProvider(model=model or "claude-sonnet-4-20250514"),
        "mock": lambda: MockProvider(),
    }
    if provider_name not in providers:
        raise ValueError(f"Unknown provider: {provider_name}. Available: {list(providers.keys())}")
    return providers[provider_name]()
