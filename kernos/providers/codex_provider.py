"""OpenAI Codex OAuth provider — ChatGPT Codex Responses API."""
import asyncio
import json
import logging
import os
import time
from typing import Any

from kernos.kernel.exceptions import (
    ReasoningConnectionError,
    ReasoningProviderError,
    ReasoningRateLimitError,
    ReasoningTransientError,
)
from kernos.providers.base import ContentBlock, Provider, ProviderResponse

logger = logging.getLogger(__name__)

_OPENAI_SIMPLE_MODEL = "gpt-4o"
_OPENAI_CHEAP_MODEL = "gpt-4o-mini"


class OpenAICodexProvider(Provider):
    """ChatGPT Codex OAuth provider — uses chatgpt.com/backend-api/codex/responses.

    NOT the standard OpenAI API. Mirrors OpenClaw's openai-codex-responses transport:
    - Endpoint: https://chatgpt.com/backend-api/codex/responses
    - Body: OpenAI Responses API format (instructions, input, tools)
    - Headers: Bearer token, chatgpt-account-id, originator: pi, OpenAI-Beta
    - Auth: ChatGPT OAuth credentials, not OPENAI_API_KEY
    """

    provider_name = "openai-codex"

    def __init__(
        self,
        credential: "OpenAICodexCredential",
        model: str = "",
    ) -> None:
        self._credential = credential
        self.main_model = model or os.getenv("OPENAI_CODEX_MODEL", "gpt-5.4")
        self.simple_model = os.getenv("OPENAI_CODEX_SIMPLE_MODEL", self.main_model)
        self.cheap_model = os.getenv("OPENAI_CODEX_CHEAP_MODEL", "gpt-5.4-nano")
        self._base_url = os.getenv(
            "OPENAI_CODEX_BASE_URL", "https://chatgpt.com/backend-api"
        )
        self._http: Any = None

    async def _ensure_http(self) -> Any:
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=120.0)
        return self._http

    async def _ensure_valid_token(self) -> None:
        """Refresh the access token if expired or within 60s of expiry."""
        now_ms = int(time.time() * 1000)
        if self._credential["expires"] and self._credential["expires"] > now_ms + 60_000:
            return
        from kernos.kernel.credentials import refresh_openai_codex_credential
        logger.info("CODEX_REFRESH: token expired or near expiry, refreshing")
        self._credential = await refresh_openai_codex_credential(self._credential)

    def _headers(self) -> dict[str, str]:
        """Build request headers matching OpenClaw's Codex wire contract."""
        return {
            "Authorization": f"Bearer {self._credential['access']}",
            "chatgpt-account-id": self._credential["accountId"],
            "originator": "pi",
            "User-Agent": "pi (python)",
            "Content-Type": "application/json",
            "OpenAI-Beta": "responses=experimental",
            "accept": "application/json",
        }

    @staticmethod
    def _translate_tools(tools: list[dict]) -> list[dict]:
        """Convert Anthropic-format tool defs to OpenAI Responses API function format."""
        result = []
        for t in tools:
            result.append({
                "type": "function",
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            })
        return result

    @staticmethod
    def _translate_input(messages: list[dict]) -> list[dict]:
        """Convert Anthropic-format messages to OpenAI Responses API input items."""
        items: list[dict] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, str):
                if role == "assistant":
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    })
                else:
                    items.append({
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": content}],
                    })
                continue

            if isinstance(content, list):
                tool_calls = []
                text_parts = []
                tool_results = []

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "tool_use":
                        tool_calls.append(block)
                    elif btype == "tool_result":
                        tool_results.append(block)
                    elif btype == "text":
                        text_parts.append(block.get("text", ""))

                if tool_calls:
                    if text_parts:
                        text = "".join(text_parts)
                        items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        })
                    for tc in tool_calls:
                        items.append({
                            "type": "function_call",
                            "call_id": tc.get("id", ""),
                            "name": tc.get("name", ""),
                            "arguments": json.dumps(tc.get("input", {})),
                        })
                elif tool_results:
                    for tr in tool_results:
                        items.append({
                            "type": "function_call_output",
                            "call_id": tr.get("tool_use_id", ""),
                            "output": tr.get("content", ""),
                        })
                elif text_parts:
                    items.append({
                        "type": "message",
                        "role": role if role != "assistant" else "user",
                        "content": [{"type": "input_text", "text": "".join(text_parts)}],
                    })

        return items

    @staticmethod
    def _parse_response(data: dict) -> ProviderResponse:
        """Parse OpenAI Responses API response into Kernos-native format."""
        status = data.get("status", "completed")
        output_items = data.get("output", [])

        if status == "incomplete":
            stop_reason = "max_tokens"
        elif any(item.get("type") == "function_call" for item in output_items):
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"

        content_blocks: list[ContentBlock] = []

        for item in output_items:
            item_type = item.get("type", "")

            if item_type == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        content_blocks.append(
                            ContentBlock(type="text", text=part.get("text", ""))
                        )

            elif item_type == "output_text":
                # Direct output_text item (structured output / text format)
                content_blocks.append(
                    ContentBlock(type="text", text=item.get("text", ""))
                )

            elif item_type == "function_call":
                try:
                    args = json.loads(item.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                content_blocks.append(ContentBlock(
                    type="tool_use",
                    id=item.get("call_id", item.get("id", "")),
                    name=item.get("name", ""),
                    input=args,
                ))

        if not content_blocks:
            content_blocks.append(ContentBlock(type="text", text=""))

        usage = data.get("usage", {})
        return ProviderResponse(
            content=content_blocks,
            stop_reason=stop_reason,
            input_tokens=usage.get("input_tokens", usage.get("prompt_tokens", 0)),
            output_tokens=usage.get("output_tokens", usage.get("completion_tokens", 0)),
        )

    def _resolve_url(self) -> str:
        """Build the Codex responses endpoint URL."""
        base = self._base_url.rstrip("/")
        if base.endswith("/codex/responses"):
            return base
        if base.endswith("/codex"):
            return f"{base}/responses"
        return f"{base}/codex/responses"

    async def complete(
        self,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        output_schema: dict | None = None,
    ) -> ProviderResponse:
        await self._ensure_valid_token()
        http = await self._ensure_http()

        # Codex doesn't support prompt caching — flatten to one string
        if isinstance(system, list):
            system_str = "\n\n".join(b.get("text", "") for b in system if b.get("text"))
        else:
            system_str = system

        body: dict[str, Any] = {
            "model": model,
            "instructions": system_str,
            "input": self._translate_input(messages),
            "store": False,
            "stream": True,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if tools:
            body["tools"] = self._translate_tools(tools)
        if output_schema:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "output",
                    "schema": output_schema,
                }
            }

        url = self._resolve_url()
        headers = self._headers()
        headers["accept"] = "text/event-stream"

        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                async with http.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code == 401:
                        await resp.aread()
                        raise ReasoningProviderError(
                            f"Codex auth failed (401): {resp.text[:300]}"
                        )
                    if resp.status_code == 429:
                        await resp.aread()
                        raise ReasoningRateLimitError(
                            f"Codex rate limited (429): {resp.text[:300]}"
                        )
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise ReasoningProviderError(
                            f"Codex API error ({resp.status_code}): {resp.text[:300]}"
                        )
                    data = await self._collect_sse_response(resp)
                return self._parse_response(data)
            except (ReasoningRateLimitError, ReasoningProviderError):
                raise  # 4xx / known errors — don't retry
            except ReasoningTransientError as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning("REASON_RETRY: attempt=2 transient=%s", exc)
                    await asyncio.sleep(1.5)
                    continue
                raise ReasoningProviderError(f"Codex transient error after retries: {exc}") from exc
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning("REASON_RETRY: attempt=2 error=%s", exc)
                    await asyncio.sleep(1.5)
                    continue
                raise ReasoningConnectionError(f"Codex request failed: {exc}") from exc

        raise ReasoningConnectionError(f"Codex request failed: {last_exc}") from last_exc

    @staticmethod
    async def _collect_sse_response(resp: Any) -> dict:
        """Read an SSE stream and return the final response object."""
        final_response: dict = {}
        buffer = ""

        async for chunk in resp.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                idx = buffer.index("\n\n")
                block = buffer[:idx]
                buffer = buffer[idx + 2:]

                data_lines = [
                    line[5:].strip() for line in block.split("\n")
                    if line.startswith("data:")
                ]
                if not data_lines:
                    continue
                data_str = "\n".join(data_lines).strip()
                if not data_str or data_str == "[DONE]":
                    continue

                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type in ("response.completed", "response.done"):
                    final_response = event.get("response", event)
                elif event_type == "response.failed":
                    msg = ""
                    if "response" in event:
                        err = event["response"].get("error", {})
                        msg = err.get("message", "")
                    raise ReasoningProviderError(
                        f"Codex response failed: {msg or event_type}"
                    )
                elif event_type == "error":
                    err = event.get("error", {})
                    msg = err.get("message", event.get("message", event.get("code", "unknown")))
                    error_type = err.get("type", "")
                    logger.warning("CODEX_STREAM_ERROR: event=%s", json.dumps(event)[:500])
                    if error_type == "server_error":
                        raise ReasoningTransientError(f"Codex server error: {msg}")
                    raise ReasoningProviderError(f"Codex stream error: {msg}")

        if not final_response:
            raise ReasoningProviderError("Codex stream ended without response.completed")

        return final_response
