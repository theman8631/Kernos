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
        # Optional callback for retry notifications: async fn(attempt, max_retries, delay)
        self.on_retry_notify: Any = None

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
    @staticmethod
    def _translate_tools(tools: list[dict]) -> list[dict]:
        """Convert Anthropic-format tool defs to OpenAI Responses API function format."""
        result = []
        for t in tools:
            schema = t.get("input_schema", {"type": "object", "properties": {}})
            result.append({
                "type": "function",
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": schema,
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
                call_name = item.get("name", "")

                # Unpack synthetic multi_tool_use.parallel into individual calls
                if call_name == "multi_tool_use.parallel":
                    for sub_call in args.get("tool_uses", []):
                        sub_args = sub_call.get("parameters", {})
                        if isinstance(sub_args, str):
                            try:
                                sub_args = json.loads(sub_args)
                            except json.JSONDecodeError:
                                sub_args = {}
                        content_blocks.append(ContentBlock(
                            type="tool_use",
                            id=sub_call.get("recipient_name", "") + "_" + item.get("call_id", ""),
                            name=sub_call.get("recipient_name", ""),
                            input=sub_args,
                        ))
                    logger.info("CODEX_PARALLEL_UNPACK: unpacked %d tool calls from multi_tool_use.parallel",
                        len(args.get("tool_uses", [])))
                else:
                    content_blocks.append(ContentBlock(
                        type="tool_use",
                        id=item.get("call_id", item.get("id", "")),
                        name=call_name,
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

        # Codex API has a ~32KB limit on the instructions field.
        # Strategy: use the static/dynamic split from Anthropic's cache boundary.
        # Static (RULES + ACTIONS) → instructions field (stable, fits in 30KB).
        # Dynamic (NOW + STATE + RESULTS + MEMORY) → developer message in input
        # (no size limit beyond the model's context window).
        # This is intentional architecture, not just overflow handling.
        _INSTRUCTIONS_LIMIT = 30000

        if isinstance(system, list) and len(system) >= 2:
            # Cache-boundary format: [static, dynamic]
            instructions_str = system[0].get("text", "") if isinstance(system[0], dict) else str(system[0])
            dynamic_str = system[1].get("text", "") if isinstance(system[1], dict) else str(system[1])
            # If static alone exceeds limit, trim it too
            if len(instructions_str) > _INSTRUCTIONS_LIMIT:
                cut = instructions_str.rfind("\n", 0, _INSTRUCTIONS_LIMIT)
                if cut <= 0:
                    cut = _INSTRUCTIONS_LIMIT
                dynamic_str = instructions_str[cut:] + "\n\n" + dynamic_str
                instructions_str = instructions_str[:cut]
        elif isinstance(system, list):
            instructions_str = "\n\n".join(b.get("text", "") for b in system if b.get("text"))
            dynamic_str = ""
        else:
            instructions_str = system
            dynamic_str = ""

        # If no split was available and instructions exceed limit, split on newline
        if not dynamic_str and len(instructions_str) > _INSTRUCTIONS_LIMIT:
            cut = instructions_str.rfind("\n", 0, _INSTRUCTIONS_LIMIT)
            if cut <= 0:
                cut = _INSTRUCTIONS_LIMIT
            dynamic_str = instructions_str[cut:]
            instructions_str = instructions_str[:cut]

        translated_input = self._translate_input(messages)
        if dynamic_str:
            translated_input.insert(0, {"role": "developer", "content": dynamic_str})
            logger.info("CODEX_SPLIT: instructions=%dKB developer_msg=%dKB input_items=%d",
                len(instructions_str) // 1024, len(dynamic_str) // 1024,
                len(translated_input))

        body: dict[str, Any] = {
            "model": model,
            "instructions": instructions_str,
            "input": translated_input,
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

        # Log actual request payload size for debugging API limits
        _payload_bytes = len(json.dumps(body))
        _tool_count = len(body.get("tools", []))
        _tool_bytes = len(json.dumps(body.get("tools", []))) if body.get("tools") else 0
        logger.info("CODEX_REQUEST: payload=%dKB tools=%d tool_schemas=%dKB input_items=%d",
            _payload_bytes // 1024, _tool_count, _tool_bytes // 1024, len(body.get("input", [])))

        url = self._resolve_url()
        headers = self._headers()
        headers["accept"] = "text/event-stream"

        _max_retries = int(os.environ.get("KERNOS_CODEX_MAX_RETRIES", "3"))
        last_exc: Exception | None = None
        for attempt in range(_max_retries):
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
                if attempt < _max_retries - 1:
                    _delay = min(1.5 * (1.5 ** attempt), 15.0)
                    logger.warning("REASON_RETRY: attempt=%d/%d delay=%.1fs transient=%s",
                        attempt + 2, _max_retries, _delay, str(exc)[:80])
                    await asyncio.sleep(_delay)
                    continue
                raise ReasoningProviderError(f"Codex transient error after {_max_retries} attempts: {exc}") from exc
            except Exception as exc:
                last_exc = exc
                if attempt < _max_retries - 1:
                    _delay = min(1.5 * (1.5 ** attempt), 15.0)
                    logger.warning("REASON_RETRY: attempt=%d/%d delay=%.1fs error=%s",
                        attempt + 2, _max_retries, _delay, str(exc)[:80])
                    await asyncio.sleep(_delay)
                    continue
                raise ReasoningConnectionError(f"Codex request failed after {_max_retries} attempts: {exc}") from exc

        raise ReasoningConnectionError(f"Codex request failed: {last_exc}") from last_exc

    @staticmethod
    async def _collect_sse_response(resp: Any) -> dict:
        """Read an SSE stream and return the final response object.

        Accumulates text from delta events during streaming, then merges
        into the final response if the completed event has empty output.
        """
        final_response: dict = {}
        buffer = ""

        # Accumulate streamed content: {output_index: {type, text, ...}}
        _streamed_items: dict[int, dict] = {}
        _streamed_text: dict[int, list[str]] = {}  # output_index → text chunks
        _streamed_fn_args: dict[str, list[str]] = {}  # call_id → argument chunks

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

                # Accumulate output items and text deltas
                elif event_type == "response.output_item.added":
                    oi = event.get("output_index", 0)
                    item = event.get("item", {})
                    _streamed_items[oi] = item
                elif event_type == "response.output_text.delta":
                    oi = event.get("output_index", 0)
                    delta = event.get("delta", "")
                    if oi not in _streamed_text:
                        _streamed_text[oi] = []
                    _streamed_text[oi].append(delta)
                elif event_type == "response.output_item.done":
                    # Completed output item — may have full arguments
                    oi = event.get("output_index", 0)
                    item = event.get("item", {})
                    if item:
                        _streamed_items[oi] = item  # Overwrite with completed version
                elif event_type == "response.function_call_arguments.delta":
                    # Key by output_index (reliable) AND call_id/item_id (fallback)
                    oi = event.get("output_index", -1)
                    call_id = event.get("call_id", event.get("item_id", ""))
                    delta = event.get("delta", "")
                    # Use output_index as primary key for reconstruction
                    key = f"oi:{oi}" if oi >= 0 else call_id
                    if key not in _streamed_fn_args:
                        _streamed_fn_args[key] = []
                    _streamed_fn_args[key].append(delta)
                    # Also store by call_id for backwards compat
                    if call_id and call_id != key:
                        if call_id not in _streamed_fn_args:
                            _streamed_fn_args[call_id] = []
                        _streamed_fn_args[call_id].append(delta)

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

        # If final_response.output is empty but we accumulated streamed content,
        # reconstruct the output from deltas
        if final_response:
            output = final_response.get("output", [])
            if not output and (_streamed_text or _streamed_items or _streamed_fn_args):
                # Collect all output indices we know about
                all_indices = set()
                all_indices.update(_streamed_items.keys())
                all_indices.update(_streamed_text.keys())
                # Also extract indices from fn_args keys like "oi:0"
                for key in _streamed_fn_args:
                    if key.startswith("oi:"):
                        try:
                            all_indices.add(int(key[3:]))
                        except ValueError:
                            pass

                reconstructed = []
                for oi in sorted(all_indices):
                    item = dict(_streamed_items.get(oi, {}))
                    if oi in _streamed_text:
                        full_text = "".join(_streamed_text[oi])
                        if item.get("type") == "message":
                            item["content"] = [{"type": "output_text", "text": full_text}]
                        else:
                            item.setdefault("type", "output_text")
                            item["text"] = full_text
                    # Reconstruct function call arguments — try output_index first, then call_id
                    oi_key = f"oi:{oi}"
                    call_id = item.get("call_id", item.get("id", ""))
                    fn_args = _streamed_fn_args.get(oi_key) or _streamed_fn_args.get(call_id)
                    if fn_args:
                        item["arguments"] = "".join(fn_args)
                    reconstructed.append(item)
                final_response["output"] = reconstructed

        if not final_response:
            raise ReasoningProviderError("Codex stream ended without response.completed")

        return final_response
