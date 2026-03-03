## NOW

**Status:** Phase 1A COMPLETE — All deliverables live-verified
**Owner:** Architect
**Action:** Phase 1B planning. Architect will produce specs for kernel implementation.

### Phase 1A Final Status (all live-verified)
- **1A.1** AIOS evaluation: Complete — decision: reference-only, not fork
- **1A.2** SMS gateway: Code complete, tests pass. Live SMS blocked on Twilio A2P registration (submitted, pending approval)
- **1A.2b** Discord adapter: Live-verified 2026-02-28. Messages flow, calendar works, error handling confirmed
- **1A.3** Google Calendar via MCP: Live-verified 2026-02-28. Real calendar data retrieved and returned via Discord
- **1A.4** Persistence: Live-verified 2026-03-01. Conversation memory survives restart, three-store separation working (conversation/tenant/audit), auto-provisioning, shadow archive structure in place, typing indicator added

### Phase 1A Completion Criteria — MET
✅ Text the bot, ask about your schedule, get a real answer
✅ Architecture separates platform adapter from handler from capability
✅ Persistence survives restart
✅ Using it yourself daily (Discord path)
Note: SMS path blocked on A2P but architecture is platform-agnostic — Discord proves the full pipeline

### What's next
Phase 1B: The Kernel. Architect producing specs for kernel core, memory system, security framework, and agent SDK. See Blueprint Part 4 for full 1B deliverable list.

> **Rule:** This block is always the first thing in the file. Whoever completes a step updates it before handing off. Format is always: Status (what), Owner (who: Founder / Architect / Claude Code), Action (the single next thing to do). If you're opening this file and wondering what to do, start here.

> **What this file is:** The bridge between planning and execution. The founder and Claude (architect) plan here. Claude Code executes against the Active Spec section. Read `KERNOS-BLUEPRINT.md` for full vision and architecture — this file assumes you have.
>
> **Rule:** Claude Code reads this file first, then executes the Active Spec. If something in this file conflicts with the Blueprint, this file wins (it represents more recent decisions).

---

## Live Verification Policy

Every deliverable that adds or changes user-facing capability requires a live test before it is marked complete. Automated tests prove the code works in isolation. Live tests prove it works in the world.

**Structural rule:** Every spec in this file that has user-facing changes MUST include a "Live Verification" section at the end, after Acceptance Criteria. This section contains:
- Prerequisites (accounts, credentials, setup needed)
- Step-by-step deployment instructions
- A test table: what to send, what to expect
- Troubleshooting for common failures

The architect produces this section as part of the spec. Claude Code does not execute it — the founder does. A deliverable is not COMPLETE until live verification passes.

Every live verification includes an Agent Awareness test as the first step. On a cold session (no prior messages), the agent must correctly identify itself, its platform, its available tools, and its trust context. If the agent hallucinates capabilities it doesn't have or fails to know about capabilities it does have, the system prompt or tool configuration must be fixed before proceeding to functional tests. The agent should never need to be told what it can do — it should already know.

| Deliverable | Live Test | How |
|---|---|---|
| 1A.2 | Message the bot, get a conversational response from Claude | Discord (now), SMS (after A2P approval) |
| 1A.2b | Discord adapter works end-to-end | See live verification in 1A.2b spec below |
| 1A.3 | Ask "what's on my schedule today", get real calendar data | Discord first, SMS after A2P |
| 1A.4 | Restart the app, message again, confirm it remembers prior conversation | Will be included in 1A.4 spec |
If a deliverable is purely internal (refactoring, test infrastructure, documentation), live verification is not required. The architect will note "Live verification: N/A" in the spec.

---

## Active Spec: Phase 1A.2 — SMS Gateway with Normalized Messaging

**Status:** COMPLETE — 27/27 tests passing, all acceptance criteria met. Awaiting live verification (see LIVE-TEST-1A2.md).

**Objective:** Build the foundational messaging pipeline. A message comes in via Twilio SMS, gets normalized into a platform-agnostic format, is processed by a handler that calls Claude, and the response goes back out through the adapter. This is the skeleton that every future platform adapter and every future agent plugs into.

**The critical architectural constraint:** The handler NEVER imports or references anything from the adapters. The adapters NEVER import or reference anything from the handler. They share ONLY the NormalizedMessage model. This separation is the foundation of the platform-agnostic messaging layer. Violating it is a build failure.

### File Structure

```
kernos/
├── __init__.py
├── app.py                              # FastAPI application
├── messages/
│   ├── __init__.py
│   ├── models.py                       # NormalizedMessage dataclass
│   ├── handler.py                      # Message handler (calls Claude API)
│   └── adapters/
│       ├── __init__.py
│       ├── base.py                     # Abstract adapter interface
│       └── twilio_sms.py              # Twilio SMS adapter
.env.example
requirements.txt
README.md
```

### 1. Normalized Message Model (`kernos/messages/models.py`)

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

class AuthLevel(str, Enum):
    OWNER_VERIFIED = "owner_verified"
    OWNER_UNVERIFIED = "owner_unverified"
    TRUSTED_CONTACT = "trusted_contact"
    UNKNOWN = "unknown"

@dataclass
class NormalizedMessage:
    content: str
    sender: str
    sender_auth_level: AuthLevel
    platform: str                                    # "sms", "discord", "telegram", "voice", "app"
    platform_capabilities: list[str]                 # e.g. ["text", "mms"] for SMS
    conversation_id: str
    timestamp: datetime
    tenant_id: str                                   # Keyed from day one — Blueprint mandate
    context: Optional[dict] = field(default=None)    # Extensible metadata
```

This is the contract. Every adapter produces one. Every handler consumes one. Nothing else crosses the boundary.

### 2. Abstract Adapter Interface (`kernos/messages/adapters/base.py`)

```python
from abc import ABC, abstractmethod
from kernos.messages.models import NormalizedMessage

class PlatformAdapter(ABC):
    @abstractmethod
    def inbound(self, raw_request: dict) -> NormalizedMessage:
        """Translate a platform-native inbound request into a NormalizedMessage."""
        ...

    @abstractmethod
    def outbound(self, response_text: str, original_message: NormalizedMessage) -> dict:
        """Translate a response string into a platform-native response format."""
        ...
```

### 3. Twilio SMS Adapter (`kernos/messages/adapters/twilio_sms.py`)

Implements `PlatformAdapter`. Key behaviors:

- **Inbound:** Parses Twilio webhook form data (`From`, `Body`, `MessageSid`) into a `NormalizedMessage`. Sets `platform="sms"`, `platform_capabilities=["text", "mms"]`.
- **Auth level:** Compares `From` number against `OWNER_PHONE_NUMBER` env var. Match → `owner_unverified` (phone number is identification, not authentication — per Blueprint). No match → `unknown`.
- **Tenant resolution:** For Phase 1A, single-tenant. Use `OWNER_PHONE_NUMBER` as the `tenant_id`. The architecture supports multi-tenant from day one — we just only have one tenant.
- **Conversation ID:** Use the sender's phone number as conversation ID for now. SMS doesn't have sessions, but the user's phone number gives us conversation continuity per the Blueprint ("context belongs to the user, not the channel").
- **Outbound — SMS length handling:** The adapter owns this constraint (Blueprint-specified). The handler returns full-length responses. The adapter handles formatting:
  - If response ≤ 1600 characters (Twilio's actual limit per segment): send as-is via TwiML.
  - If response > 1600 characters: truncate to 1550 chars + " [...] Reply MORE for the rest." Store the overflow keyed to the conversation_id (an in-memory dict is fine for Phase 1A).
  - If inbound message is "MORE" (case-insensitive) and overflow exists: return next chunk.
- **Outbound format:** Returns a TwiML XML response string (`<Response><Message>text</Message></Response>`).
- **No imports from handler.** This module knows about `NormalizedMessage`, `PlatformAdapter`, and Twilio's data format. Nothing else.

### 4. Message Handler (`kernos/messages/handler.py`)

Receives a `NormalizedMessage`, returns a response string. Key behaviors:

- **Calls Anthropic Claude API** (model: `claude-sonnet-4-20250514`) with the message content.
- **System prompt** tells Claude it is a personal assistant reached via SMS. Instructs it to keep responses concise and SMS-appropriate (a few sentences, not essays). Include the sender's auth level in the system prompt so Claude knows the trust context.
- **Graceful error handling** (Blueprint-mandated). The handler wraps the Claude API call in try/except. Every failure mode produces a friendly string response — never an exception, never silence:
  - `anthropic.APITimeoutError` or `anthropic.APIConnectionError` → `"Something went wrong on my end — try again in a moment."`
  - `anthropic.RateLimitError` → `"I'm a bit overloaded right now. Try again in a minute."`
  - `anthropic.APIStatusError` → `"Something went wrong on my end — try again in a moment."`
  - Any other `Exception` → `"Something unexpected happened. Try again, and if it keeps happening, let me know."`
  - All errors logged with full context (use Python `logging` module).
- **No imports from any adapter.** This module knows about `NormalizedMessage` and the Anthropic SDK. Nothing else.

### 5. FastAPI App (`kernos/app.py`)

- **`GET /health`** — Returns `{"status": "ok"}`. Simple health check.
- **`POST /sms/inbound`** — The Twilio webhook endpoint.
  - Receives Twilio's form-encoded POST body.
  - Instantiates the Twilio adapter, calls `adapter.inbound(form_data)` → gets a `NormalizedMessage`.
  - Passes the `NormalizedMessage` to `handler.process(message)` → gets a response string.
  - Calls `adapter.outbound(response, original_message)` → gets TwiML.
  - Returns the TwiML as `Response(content=twiml, media_type="application/xml")`.
  - If anything fails at the app level, return a TwiML response with a friendly error (never a raw HTTP 500 that Twilio can't use).
- **Twilio webhook validation:** For Phase 1A, skip request signature validation. Add a `# TODO: Validate Twilio request signature` comment where it should go. We'll add it before any real deployment.
- Load env vars from `.env` file using `python-dotenv` at app startup.

### 6. Configuration Files

**`.env.example`:**
```
ANTHROPIC_API_KEY=your-anthropic-api-key
TWILIO_ACCOUNT_SID=your-twilio-account-sid
TWILIO_AUTH_TOKEN=your-twilio-auth-token
TWILIO_PHONE_NUMBER=+1234567890
OWNER_PHONE_NUMBER=+1234567890
```

**`requirements.txt`:**
```
fastapi>=0.115.0
uvicorn>=0.34.0
anthropic>=0.49.0
python-dotenv>=1.0.0
twilio>=9.0.0
```

**`README.md`:** Brief setup instructions — clone, copy `.env.example` to `.env`, fill in keys, `pip install -r requirements.txt`, `uvicorn kernos.app:app --reload`. Mention that Twilio webhook URL needs to point to the `/sms/inbound` endpoint (use ngrok for local dev).

### 7. Tests

Create `tests/` directory with:

- **`tests/test_models.py`** — Verify NormalizedMessage creation and field validation.
- **`tests/test_twilio_adapter.py`** — Test inbound parsing (mock Twilio form data → correct NormalizedMessage). Test outbound formatting (response string → valid TwiML). Test SMS overflow/truncation logic. Test "MORE" continuation. Test owner vs unknown auth level classification.
- **`tests/test_handler.py`** — Test that handler returns a string response (mock the Anthropic client). Test each error path returns a friendly message, not an exception.
- **`tests/test_app.py`** — Integration test using FastAPI's `TestClient`. POST to `/sms/inbound` with mock Twilio payload, verify TwiML response. Test `/health` endpoint.

Use `pytest`. Add to requirements.txt: `pytest>=8.0.0`, `pytest-asyncio>=0.24.0`, `httpx>=0.27.0` (for FastAPI TestClient).

### Acceptance Criteria

All of these must be true when the spec is complete:

1. `pytest` passes with all tests green.
2. The handler module has zero imports from `kernos.messages.adapters` (grep to verify).
3. The twilio_sms module has zero imports from `kernos.messages.handler` (grep to verify).
4. The NormalizedMessage model includes `tenant_id` as a required field.
5. Every error path in the handler returns a friendly string, never raises.
6. The Twilio adapter handles messages over 1600 chars with truncation + MORE continuation.
7. `uvicorn kernos.app:app` starts without errors (with valid env vars).

**Live Verification:** See [live-tests/1A2-sms-gateway.md](live-tests/1A2-sms-gateway.md)

---

## Completed Spec: Phase 1A.2b — Discord Adapter

**Status:** COMPLETE — 44/44 tests passing, live verified 2026-03-01. Agent awareness tests all passed after system prompt fix.

**Objective:** Build a Discord bot adapter so we can live test the messaging pipeline immediately, without waiting for Twilio A2P registration. This is the same NormalizedMessage pattern as the Twilio adapter — a thin translator between Discord's API and the handler. The handler doesn't change at all.

**Why now:** Twilio A2P 10DLC registration has been submitted but takes days to weeks for approval. Discord provides an authenticated messaging channel with zero regulatory gates. Per the Blueprint, Discord is medium-high auth confidence (authenticated account sessions). This was always the second platform (Phase 2.4) — we're pulling it forward to unblock development.

**SMS is still the vision.** The Twilio adapter is built, tested, and waiting. When A2P clears, SMS lights up with zero code changes. Discord is the development and testing channel until then.

### Prerequisites

- 1A.2 code complete and all tests passing (DONE).
- A Discord account.
- A Discord server (create one for testing — takes 10 seconds).
- A Discord bot application (created at discord.com/developers/applications).

### File Changes
kernos/
├── messages/
│   └── adapters/
│       └── discord_bot.py             # NEW — Discord adapter
├── discord_bot.py                      # NEW — Discord bot entry point (separate from FastAPI)
.env.example                            # Add DISCORD_BOT_TOKEN
requirements.txt                        # Add discord.py>=2.3.0

### 1. Discord Adapter (`kernos/messages/adapters/discord_bot.py`)

Implements `BaseAdapter`. Key behaviors:

- **Inbound:** Translates a `discord.Message` into a `NormalizedMessage`.
  - `content` = message.content
  - `sender` = str(message.author.id)
  - `sender_auth_level` = `owner_verified` if author.id matches `DISCORD_OWNER_ID` env var, else `unknown`. Discord accounts are authenticated sessions — per Blueprint, this is medium-high confidence, so we can grant `owner_verified` (unlike SMS which only gets `owner_unverified`).
  - `platform` = "discord"
  - `platform_capabilities` = ["text", "embeds", "attachments", "reactions"]
  - `conversation_id` = str(message.channel.id) (Discord has real channels/threads, unlike SMS)
  - `timestamp` = message.created_at
  - `tenant_id` = env var `OWNER_PHONE_NUMBER` (same single tenant as 1A.2 — this doesn't change)
  - `context` = {"guild_id": str(message.guild.id), "channel_name": message.channel.name} if in a guild, else None

- **Outbound:** Takes a response string and the original NormalizedMessage. Returns the string directly — Discord's API handles sending via `message.channel.send()`. No length limit concerns for Phase 1A (Discord allows 2000 chars per message, and Claude's SMS-optimized responses will be well under that).

- **No imports from handler.** Same rule as the Twilio adapter.

### 2. Discord Bot Entry Point (`kernos/discord_bot.py`)

A separate script that runs the Discord bot. This is NOT part of the FastAPI app — it's a standalone process. The FastAPI app serves Twilio webhooks; the Discord bot connects to Discord's gateway via websocket. They share the handler and models but run independently.
```python
import os
import logging
import discord
from dotenv import load_dotenv
from kernos.messages.adapters.discord_bot import DiscordAdapter
from kernos.messages.handler import handle_message

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
adapter = DiscordAdapter()

@client.event
async def on_ready():
    logger.info(f"Discord bot connected as {client.user}")

@client.event
async def on_message(message):
    # Don't respond to ourselves
    if message.author == client.user:
        return
    # Don't respond to other bots
    if message.author.bot:
        return

    normalized = adapter.inbound(message)
    response_text = handle_message(normalized)
    await message.channel.send(response_text)

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set in .env")
    client.run(token)
```

Key design decisions:
- **Separate process from FastAPI.** The Discord bot and the Twilio webhook server are two different adapters that both feed the same handler. In production they could run as separate services or be unified under one async process. For Phase 1A, keep them separate for simplicity.
- **The handler is identical.** `handle_message()` receives a NormalizedMessage and returns a string. It has no idea whether the message came from SMS or Discord. This is the whole point of the architecture.

### 3. Updated Configuration

**Add to `.env.example`:**
Discord Bot (Phase 1A.2b)
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_OWNER_ID=your-discord-user-id

**Add to `requirements.txt`:**
discord.py>=2.3.0

### 4. Tests

**`tests/test_discord_adapter.py`** (new file):

- Test inbound: mock a `discord.Message` object, verify it produces a correct `NormalizedMessage` with platform="discord", correct capabilities, correct conversation_id from channel.
- Test owner detection: mock message with author.id matching DISCORD_OWNER_ID → `owner_verified`. Non-matching → `unknown`.
- Test outbound: verify it returns the response string unchanged.
- Test tenant_id is set to OWNER_PHONE_NUMBER (same single tenant).
- **No imports from handler in the adapter module** (grep to verify).

### Acceptance Criteria

1. `pytest` passes with all tests green (including all 1A.2 tests — no regressions).
2. The discord_bot adapter has zero imports from `kernos.messages.handler`.
3. The handler still has zero imports from any adapter.
4. `python kernos/discord_bot.py` connects to Discord and responds to messages (with valid bot token).
5. NormalizedMessage from Discord includes platform="discord" and correct auth levels.
6. Existing Twilio adapter and tests are untouched — zero changes to any 1A.2 code.

**Live Verification:** See [live-tests/1A2b-discord-adapter.md](live-tests/1A2b-discord-adapter.md)

---

## Completed Spec: Phase 1A.3 — Google Calendar via MCP

**Status:** COMPLETE — 57/57 tests passing, live verified 2026-03-01. Agent awareness and functional tests all passed.

**Objective:** Add the first real capability to KERNOS. The handler gains the ability to use MCP tools — specifically Google Calendar — so that when you text "What's on my schedule today?" you get a real answer. This is the moment KERNOS becomes something you actually use daily.

**Architectural significance:** This is the first implementation of Pillar 1 (Capability Abstraction). The handler doesn't call the Google Calendar API directly. It connects to an MCP server that exposes calendar tools, passes those tool definitions to Claude, and brokers tool calls between Claude and the MCP server. Any future capability (email, search, file management) plugs in the same way — a new MCP server, same handler loop.

### Prerequisites

- 1A.2 complete and passing all acceptance criteria.
- Google Cloud Platform project with Google Calendar API enabled.
- OAuth 2.0 credentials (Desktop App type) downloaded as JSON.
- Node.js installed (the MCP server is TypeScript; it runs as a subprocess).
- The `@cocal/google-calendar-mcp` package (installed via npx on first run).

### How It Works — The Tool-Use Loop

The 1A.2 handler makes a simple Claude API call: message in → response out. The 1A.3 handler adds a tool-use loop:

```
1. Handler receives NormalizedMessage
2. Handler gets available tool definitions from MCPClientManager (already connected at startup)
3. Handler calls Claude API with message + tool definitions
4. If Claude responds with text only → return it (same as 1A.2)
5. If Claude responds with tool_use → handler calls the MCP tool via MCPClientManager, gets result
6. Handler sends tool result back to Claude
7. Repeat from step 4 until Claude responds with text only
8. Return final text response
```

This is the standard MCP client pattern from the official Python SDK docs. The handler becomes a broker between Claude and MCP servers.

### File Changes

```
kernos/
├── __init__.py
├── app.py                              # Changes — startup/shutdown lifecycle for MCP
├── discord_bot.py                      # Changes — same MCP lifecycle as app.py
├── mcp/                                # NEW — MCP client infrastructure
│   ├── __init__.py
│   └── client.py                       # MCP client manager (connects to servers)
├── messages/
│   ├── __init__.py
│   ├── models.py                       # No changes
│   ├── handler.py                      # Major changes — tool-use loop, accepts MCPClientManager
│   └── adapters/
│       ├── __init__.py
│       ├── base.py                     # No changes
│       └── twilio_sms.py              # No changes
.env.example                            # Add GOOGLE_OAUTH_CREDENTIALS_PATH
requirements.txt                        # Add mcp>=1.26.0
```

### 1. MCP Client Manager (`kernos/mcp/client.py`)

This module manages connections to MCP servers. It is the capability abstraction layer.

**Class: `MCPClientManager`**

```python
class MCPClientManager:
    """Manages connections to MCP servers and exposes their tools."""

    def __init__(self):
        self._servers: dict[str, StdioServerParameters] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._tool_to_session: dict[str, str] = {}  # tool_name → server_name
        self._tools: list[dict] = []  # Anthropic-formatted tool definitions
```

**Methods:**

- `register_server(name: str, params: StdioServerParameters) -> None` — Register an MCP server configuration. Does not connect yet.

- `connect_all() -> None` — For each registered server: spawn the subprocess via `stdio_client()`, create a `ClientSession`, call `session.initialize()`, then `session.list_tools()`. Convert each MCP tool to Anthropic format (`{"name": tool.name, "description": tool.description, "input_schema": tool.inputSchema}`). Build the `_tool_to_session` mapping. Log all discovered tools at INFO level.

- `disconnect_all() -> None` — Clean up all sessions and transports. Called on app shutdown.

- `get_tools() -> list[dict]` — Return all available tools in Anthropic API format. Returns empty list if no servers connected (handler falls back to non-tool behavior).

- `call_tool(tool_name: str, tool_args: dict) -> str` — Look up which session owns this tool via `_tool_to_session`. Call `session.call_tool(tool_name, tool_args)`. Extract text content from the result. On ANY error (tool not found, MCP server error, timeout): log the error and return a descriptive error string like `"Calendar tool error: {description}"`. **Never raise an exception.** The handler will pass this error string to Claude as a tool result, and Claude will explain it to the user conversationally.

**Key design decisions:**

- **Lifecycle:** MCPClientManager is created once at app startup. MCP server subprocesses are long-lived — they start when the app starts and stop when the app stops. NOT spawned per-request.
- **stdio transport:** Use `mcp.client.stdio.stdio_client()` context manager. The manager must keep the transport context alive for the lifetime of the app. Implementation note: store the context manager's `__aenter__` result and call `__aexit__` on disconnect. Alternatively, use `asyncio.create_task` with a long-lived connection loop. The key constraint is that the stdio subprocess must stay alive between requests.
- **Tool namespacing:** Track which session owns which tool via `_tool_to_session`. For Phase 1A.3 there's only one server, but the architecture supports many.
- **No imports from adapters or handler.** This module knows about the MCP SDK only.

### 2. Updated Handler (`kernos/messages/handler.py`)

The handler gains a tool-use loop. Changes from 1A.2:

- **Constructor** now accepts an `MCPClientManager` instance.
- **`process()` method** is now `async` (MCP tool calls are async).
- **Tool-use loop:** After the initial Claude API call, if `response.stop_reason == "tool_use"`, the handler extracts tool call blocks, calls `mcp.call_tool()` for each, packages the results, appends to the message history, and calls Claude again. Repeats until Claude responds with text only.
- **Safety valve:** Maximum 10 iterations of the tool-use loop. If exceeded, return: `"I'm having trouble completing that request. Try asking in a simpler way."`
- **System prompt is built dynamically from connected tools:** `_build_system_prompt()` now accepts the tool list from `mcp.get_tools()`. If calendar tools are present, the CURRENT CAPABILITIES section mentions calendar. If no tools are connected, it keeps the conversation-only language from Phase 1A.2b. The agent must never claim a capability that isn't backed by a real connected tool — the system prompt is the source of truth for what the agent says it can do.
- **Backward compatibility:** If `mcp.get_tools()` returns an empty list (no MCP servers connected), the handler works identically to 1A.2 — just a Claude API call with no tools. Pass `tools=anthropic.NOT_GIVEN` in this case.

**Import rule:** The handler now imports from `kernos.mcp.client`. This is allowed — MCP is capability infrastructure, not a platform adapter. The handler still has ZERO imports from `kernos.messages.adapters`.

**Pseudocode for the tool-use loop:**

```python
async def process(self, message: NormalizedMessage) -> str:
    tools = self.mcp.get_tools()
    messages = [{"role": "user", "content": message.content}]
    system_prompt = self._build_system_prompt(message)

    try:
        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
            tools=tools if tools else anthropic.NOT_GIVEN,
        )

        iterations = 0
        max_iterations = 10

        while response.stop_reason == "tool_use" and iterations < max_iterations:
            iterations += 1
            assistant_content = response.content
            tool_results = []

            for block in assistant_content:
                if block.type == "tool_use":
                    result = await self.mcp.call_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})

            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                system=system_prompt,
                messages=messages,
                tools=tools,
            )

        if iterations >= max_iterations:
            return "I'm having trouble completing that request. Try asking in a simpler way."

        # Extract final text
        text_parts = [block.text for block in response.content if block.type == "text"]
        return "".join(text_parts) if text_parts else "I processed your request but don't have a text response. Try rephrasing?"

    except anthropic.APITimeoutError:
        logger.error("Claude API timeout", exc_info=True)
        return "Something went wrong on my end — try again in a moment."
    except anthropic.APIConnectionError:
        logger.error("Claude API connection error", exc_info=True)
        return "Something went wrong on my end — try again in a moment."
    except anthropic.RateLimitError:
        logger.error("Claude API rate limit", exc_info=True)
        return "I'm a bit overloaded right now. Try again in a minute."
    except anthropic.APIStatusError:
        logger.error("Claude API status error", exc_info=True)
        return "Something went wrong on my end — try again in a moment."
    except Exception:
        logger.error("Unexpected error in handler", exc_info=True)
        return "Something unexpected happened. Try again, and if it keeps happening, let me know."
```

### 3. Updated App (`kernos/app.py`)

Add a FastAPI lifespan handler for MCP server lifecycle:

```python
from contextlib import asynccontextmanager
import os
from fastapi import FastAPI
from mcp import StdioServerParameters
from kernos.mcp.client import MCPClientManager
from kernos.messages.handler import MessageHandler

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: configure and connect MCP servers
    mcp_manager = MCPClientManager()

    credentials_path = os.getenv("GOOGLE_OAUTH_CREDENTIALS_PATH", "")
    if credentials_path:
        mcp_manager.register_server("google-calendar", StdioServerParameters(
            command="npx",
            args=["@cocal/google-calendar-mcp"],
            env={
                "GOOGLE_OAUTH_CREDENTIALS": credentials_path,
            }
        ))
    else:
        logger.warning("GOOGLE_OAUTH_CREDENTIALS_PATH not set — calendar tools unavailable")

    await mcp_manager.connect_all()
    app.state.handler = MessageHandler(mcp_manager)

    yield

    await mcp_manager.disconnect_all()

app = FastAPI(lifespan=lifespan)
```

The `/sms/inbound` endpoint now:
- Uses `request.app.state.handler` instead of creating a handler per request.
- Awaits `handler.process(message)` (it's async now).
- If `GOOGLE_OAUTH_CREDENTIALS_PATH` is not set, the app still starts — it just works like 1A.2 (no tools). This is important for development and testing.

### 3b. Updated Discord Bot (`kernos/discord_bot.py`)

The Discord bot gains the same MCP lifecycle as `app.py`:

- **`on_ready`:** Create `MCPClientManager`, register the Google Calendar server if `GOOGLE_OAUTH_CREDENTIALS_PATH` is set, call `await mcp_manager.connect_all()`, then create `MessageHandler(mcp_manager)` and store as a module-level variable.
- **Startup guard:** The handler variable is `None` until `on_ready` completes. Any message arriving before the handler is ready receives: `"Still starting up — try again in a moment."`
- **`on_message`:** Now calls `await handler.process(normalized)` (async).
- **No credentials:** If `GOOGLE_OAUTH_CREDENTIALS_PATH` is not set, the bot starts with no tools and works identically to Phase 1A.2b — conversation only.

### 4. Updated Configuration

**Add to `.env.example`:**
```
# Google Calendar MCP (Phase 1A.3)
# Path to your Google OAuth credentials JSON (Desktop App type)
# Get this from Google Cloud Console > APIs & Services > Credentials
GOOGLE_OAUTH_CREDENTIALS_PATH=/path/to/your/gcp-oauth.keys.json
```

**Add to `requirements.txt`:**
```
mcp>=1.26.0
```

**Node.js prerequisite:** Document in README that Node.js must be installed for the Google Calendar MCP server (it runs via `npx @cocal/google-calendar-mcp`).

### 5. Google Calendar OAuth Setup (one-time, document in README)

Add a "Google Calendar Setup" section to the README:

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or select existing).
3. Enable the **Google Calendar API** for your project.
4. Go to **APIs & Services > Credentials**.
5. Click **+ CREATE CREDENTIALS > OAuth client ID**.
6. Select **Desktop app** as application type. Download the JSON file.
7. Set `GOOGLE_OAUTH_CREDENTIALS_PATH` in your `.env` to the path of that JSON file.
8. Run the auth flow once:
   ```bash
   GOOGLE_OAUTH_CREDENTIALS=/path/to/your/gcp-oauth.keys.json npx @cocal/google-calendar-mcp auth
   ```
9. A browser opens — authorize with your Google account. Tokens are saved locally.
10. The MCP server now uses saved tokens automatically on future starts.

### 6. Tests

**`tests/test_mcp_client.py`** (new file):

- Test `register_server()` stores config correctly.
- Test `get_tools()` returns empty list before `connect_all()`.
- Test `call_tool()` with a mock session returns expected result string.
- Test `call_tool()` with unknown tool name returns error string, not exception.
- Test `call_tool()` when MCP server returns an error returns error string, not exception.

**`tests/test_handler.py`** (update existing):

- **Keep all existing 1A.2 tests.** They must still pass. When MCPClientManager returns no tools, behavior is identical to 1A.2.
- Add: test handler with mock MCPClientManager that has tools. Mock Claude to return a `tool_use` response, then a text response. Verify handler calls `mcp.call_tool()` and returns the final text.
- Add: test the safety valve — mock Claude to always return `tool_use`. Verify handler returns graceful message after max iterations.
- Add: test that MCP tool failure (call_tool returns error string) results in Claude receiving the error and responding gracefully.
- Add: test handler with MCPClientManager that has no tools — verify it works identically to 1A.2 (backward compatibility).

**`tests/test_app.py`** (update existing):

- Keep all existing tests.
- Add: integration test with mocked MCP that verifies the full flow: inbound SMS → handler with tool use → outbound SMS with calendar data.

### Acceptance Criteria

All of these must be true:

1. `pytest` passes with all tests green (including all 1A.2 tests — no regressions).
2. The handler imports from `kernos.mcp.client` but has zero imports from `kernos.messages.adapters`.
3. The twilio_sms module has zero imports from `kernos.messages.handler` or `kernos.mcp`.
4. MCP server starts as a subprocess on app startup and stops on shutdown.
5. When Claude uses a calendar tool, the handler brokers the call to the MCP server and returns the result.
6. Tool call errors (MCP server down, calendar API error) result in a friendly user-facing message, never an exception.
7. The tool-use loop has a safety valve (max iterations = 10) that produces a graceful message.
8. Non-tool-use messages still work identically to 1A.2 (no regression).
9. If `GOOGLE_OAUTH_CREDENTIALS_PATH` is not set, the app still starts and works without calendar tools.

**Live Verification:** See [live-tests/1A3-calendar-mcp.md](live-tests/1A3-calendar-mcp.md)

---

## Future Spec: Phase 1A.4 — Basic Persistence

**Status:** NOT YET SPECIFIED — rough sketch below, full spec after 1A.3

**Objective:** Persist enough state that the system survives restarts and maintains conversation context. Not a full memory system (that's 1B with MemOS) — just enough to be useful.

**Rough scope:**

- **Tenant record:** JSON file per tenant with `tenant_id`, `status` (provisioning/active/suspended/cancelled), preferences, created_at. Keyed to `tenant_id` from day one.
- **Conversation history:** Store recent messages per tenant so the handler can include them in Claude API calls. Without this, every SMS is a cold start — Claude has no memory of the conversation. Even 10-20 recent messages would transform the experience.
- **Shadow archive path:** The archive directory structure exists from day one (`{tenant_id}/archive/{data_type}/{timestamp}/`), even if nothing is archived yet. Blueprint mandate — the architecture for non-destructive deletion is present from Phase 1A.
- **Storage backend:** JSON files on disk for Phase 1A. Keyed to `tenant_id`. The interface should be abstract enough that swapping in MemOS (Phase 1B) or a database doesn't require rewriting the handler.

**Live Verification:** See [live-tests/1A4-persistence.md](live-tests/1A4-persistence.md)

---

## Decisions Made

### 2026-03-01: Discord adapter added as 1A.2b — primary testing channel

- **What:** Twilio A2P 10DLC registration submitted but takes days to weeks. Discord adapter added as Phase 1A.2b to unblock live testing immediately.
- **Why:** The messaging architecture is platform-agnostic by design. The handler doesn't know or care which adapter delivers the message. Discord provides an authenticated channel (medium-high trust per Blueprint) with zero regulatory gates. This was always the second platform (Blueprint Phase 2.4) — pulling it forward costs nothing architecturally and unblocks everything.
- **SMS status:** Twilio adapter is built, tested, and ready. When A2P registration clears, SMS lights up with zero code changes. SMS remains the target universal entry point per the Blueprint vision.
- **Live testing shift:** All 1A live verification tests run on Discord first. Once A2P is approved, re-run on SMS to confirm.

### 2026-02-28: Live Verification Policy adopted

- **What:** Every deliverable with user-facing changes must include a Live Verification section and pass live testing before being marked complete.
- **Why:** Automated tests prove code works in isolation; live tests prove it works in the world. Without this, we risk building something that passes tests but fails when a real person texts it.
- **Structure:** The architect produces live verification steps as part of each spec. Claude Code does not execute them — the founder does.

### 2026-02-27: Google Calendar MCP server — adopting nspady/google-calendar-mcp

- **Package:** `@cocal/google-calendar-mcp` (npm, run via npx)
- **Repo:** github.com/nspady/google-calendar-mcp
- **License:** MIT
- **Why:** Most mature Google Calendar MCP server available (964 stars, 286 forks, 180 commits, active maintenance). MIT-compatible with KERNOS. Supports stdio + HTTP transports. Features include multi-account support, tool filtering, Docker deployment, conflict detection, free/busy queries, recurring events. Community standard.
- **Why not Python alternatives:** `deciduus/calendar-mcp` is AGPL-licensed (incompatible with MIT KERNOS). `guinacio/mcp-google-calendar` is less mature. Language is irrelevant — MCP servers are external processes communicating over protocol, not library imports.
- **Integration:** KERNOS handler uses the Python `mcp` client SDK (v1.26.0) to communicate with the server via stdio subprocess. The server is an MCP capability, not a code dependency. The handler spawns it as a child process on startup.
- **Phase 2 note:** Re-evaluate `taylorwilsdon/google_workspace_mcp` (Python, covers Calendar + Gmail + Drive + Docs + Sheets + Slides in one server) when adding the email agent. Could simplify the stack by consolidating Google services into one MCP server.

### 2026-02-27: gogcli evaluated — not adopting

- **What:** [gogcli.sh](https://gogcli.sh) — Go CLI for Google Workspace (Gmail, Calendar, Drive, Contacts, Tasks, Sheets). MIT license, 3.2k GitHub stars, JSON output, multi-account support.
- **Decision:** Don't use as the calendar integration for Phase 1A.3. Blueprint mandates MCP for capability abstraction (Pillar 1, Phase 1A.3: "Connect Google Calendar via MCP"). Shelling out to a Go binary and parsing JSON creates a Google-specific code path that bypasses the uniform MCP interface agents need.
- **Fallback:** If no suitable Google Calendar MCP server exists, gogcli could be wrapped as an MCP server. But this is a fallback path, not the plan.

### 2026-02-27: DECISIONS.md created as execution bridge

- **What:** This file is the interface between planning (founder + Claude architect) and execution (Claude Code). Planning happens in conversation, decisions land here, Claude Code executes against the Active Spec section.
- **Process:** Founder and architect plan → architect drafts spec into DECISIONS.md → founder commits → Claude Code reads and executes → results reviewed → status updated.

---

## Open Questions

- **1A.1: AIOS go/no-go.** Founder needs a few hours reading AIOS source. Doesn't block anything until Phase 1B. Likely outcome per Blueprint risk register: reference-only, not fork.

---

## Future Considerations

Design notes for features not yet specced. These inform architecture decisions now so we don't build anything that blocks them later.

### User Aliases / Custom Commands (Phase 2.6)

Users should be able to define shortcuts like "whenever I say /email, show me new emails for the day." This maps to the Blueprint's behavioral contracts — the user is creating a structured specification rule (must: "when I say X, do Y"). The dynamic system prompt architecture supports this — custom commands would inject into the prompt or a routing layer. No code needed now, but nothing should be built that prevents per-user prompt injection later.

### System Dials / Meta-Commands (Phase 1B+)

Users need control over the system itself: restart context, switch LLM providers, spawn new agents, adjust settings. These are kernel operations, not agent capabilities — the user is talking to the OS, not to an agent. In Phase 1B the kernel gets a proper control plane. The handler being a single entry point means we can add command interception (detecting /restart, /switch-model, etc.) later without restructuring. Don't build prefix command parsing yet, but don't build anything that assumes every message goes straight to an LLM either.

### Agent-Created Agents (Phase 4+)

The system should eventually support agents spawning other agents — e.g., an LLM creating a specialized sub-agent for a task. This requires the agent lifecycle management from Phase 1B (Pillar 2) and the agent SDK from Phase 1B.4. No architectural impact on Phase 1A, but the kernel's process model must treat agents as first-class spawnable entities, not hardcoded singletons.

### Proactive Agent Behavior — Outbound Messaging & Time-Triggered Actions (Phase 2+)

The agent should not only respond to messages but initiate them. Key examples from live testing:

- **Pre-appointment reminders:** User says "remind me 15 minutes before appointments." The system should cache the calendar state (periodic pull, not per-question polling), maintain a chronological trigger queue, and send outbound messages at the right time. This requires the messaging gateway to support outbound (send a Discord message or SMS without being prompted) and the kernel scheduler (Phase 1B, Pillar 2) to manage time-based triggers.

- **Contextual conflict detection:** User says "I'm heading to the knitting event now." Agent cross-references current calendar state and replies: "But you have an appointment with [name] in 20 minutes. Want me to let them know you'll be late? Reschedule? Or did you already handle it — should I update the calendar?" This requires shared memory (Pillar 3), proactive reasoning, and inter-agent coordination if email/messaging agents exist.

- **Event caching architecture:** Calendar state should be pulled periodically (cron-style) and held locally, not queried per-request. Time-sensitive triggers (reminders, travel-time alerts) fire from the cached state. Calendar update polling interval is separate from trigger evaluation. This is fundamentally a scheduler problem (Phase 1B) — the agent needs a heartbeat, not just request-response.

These capabilities represent the transition from reactive (responds when asked) to ambient (works in the background). The messaging gateway already supports this structurally — adapters can send messages, not just receive them. The missing piece is the kernel scheduler and a persistence layer for trigger state.

### Calendar Response Completeness (Phase 1A or 2)

During live testing, the agent omitted event descriptions/notes when summarizing calendar entries. When an event has a description, location, or notes, the agent should include them in its summary unless brevity was specifically requested. This is a system prompt refinement — tell the agent to include event details (description, location, attendees) when present.

---

## Phase 1A Status Tracker

| ID    | Deliverable                          | Status              | Notes                                        |
|-------|--------------------------------------|---------------------|----------------------------------------------|
| 1A.1  | Evaluate AIOS codebase               | NOT STARTED         | Doesn't block 1A.2-1A.4                      |
| 1A.2  | SMS Gateway + normalized messaging   | BLOCKED (A2P)       | Code complete, tests pass. A2P registration submitted. |
| 1A.2b | Discord adapter                      | COMPLETE            | Live verified 2026-02-28                      |
| 1A.3  | Calendar capability via MCP          | COMPLETE            | Live verified 2026-02-28                      |
| 1A.4  | Basic persistence                    | COMPLETE            | Live verified 2026-03-01                      |

**Phase 1A Completion Criteria (from Blueprint):** You text the number, ask about your schedule, and get a real answer. You use it yourself at least once a day. The architecture already separates platform adapter from handler from capability.

---

*Last updated: 2026-03-01*
