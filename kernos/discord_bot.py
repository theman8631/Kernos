import logging
import os
from pathlib import Path

import discord
from dotenv import load_dotenv
from mcp import StdioServerParameters

import dataclasses

from kernos.kernel.credentials import resolve_anthropic_credential
from kernos.messages.adapters.discord_bot import DiscordAdapter
from kernos.messages.handler import MessageHandler
from kernos.capability.client import MCPClientManager
from kernos.capability.known import KNOWN_CAPABILITIES
from kernos.capability.registry import CapabilityRegistry, CapabilityStatus
from kernos.kernel.event_types import EventType
from kernos.kernel.events import JsonEventStream, emit_event
from kernos.kernel.engine import TaskEngine
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonTenantStore

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
adapter = DiscordAdapter()

# None until on_ready completes MCP setup.
handler: MessageHandler | None = None


@client.event
async def on_ready():
    global handler
    logger.info("Discord bot connected as %s", client.user)

    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    events = JsonEventStream(data_dir)
    state = JsonStateStore(data_dir)

    try:
        await emit_event(
            events, EventType.SYSTEM_STARTED, "system", "discord_bot", payload={}
        )
    except Exception as exc:
        logger.warning("Failed to emit system.started: %s", exc)

    mcp_manager = MCPClientManager(events=events)

    credentials_path = os.getenv("GOOGLE_OAUTH_CREDENTIALS_PATH", "")
    if credentials_path:
        mcp_manager.register_server(
            "google-calendar",
            StdioServerParameters(
                command="npx",
                args=["@cocal/google-calendar-mcp"],
                env={"GOOGLE_OAUTH_CREDENTIALS": credentials_path},
            ),
        )
    else:
        logger.warning(
            "GOOGLE_OAUTH_CREDENTIALS_PATH not set — calendar tools unavailable"
        )

    await mcp_manager.connect_all()

    conversations = JsonConversationStore(data_dir)
    tenants = JsonTenantStore(data_dir)
    audit = JsonAuditStore(data_dir)

    # Build capability registry from known catalog, promote connected servers
    registry = CapabilityRegistry(mcp=mcp_manager)
    for cap in KNOWN_CAPABILITIES:
        registry.register(dataclasses.replace(cap))
    for server_name, tools in mcp_manager.get_tool_definitions().items():
        cap = registry.get(server_name)
        if cap:
            cap.status = CapabilityStatus.CONNECTED
            cap.tools = [t["name"] for t in tools]
    connected = [c.name for c in registry.get_connected()]
    logger.info("Capability registry ready — connected: %s", connected or "none")

    provider = AnthropicProvider(api_key=resolve_anthropic_credential())
    reasoning = ReasoningService(provider, events, mcp_manager, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(mcp_manager, conversations, tenants, audit, events, state, reasoning, registry, engine)
    logger.info("MessageHandler ready (data_dir=%s)", data_dir)


DISCORD_MAX_LENGTH = 2000


def _chunk_response(text: str) -> list[str]:
    """Split text into chunks that fit Discord's 2000-char limit.

    Splits on newlines where possible; falls back to hard cuts.
    """
    if len(text) <= DISCORD_MAX_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= DISCORD_MAX_LENGTH:
            chunks.append(text)
            break

        # Find the last newline within the limit
        cut = text.rfind("\n", 0, DISCORD_MAX_LENGTH)
        if cut <= 0:
            # No newline found — hard cut
            cut = DISCORD_MAX_LENGTH

        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")

    return chunks


_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".json", ".csv", ".yaml", ".yml",
    ".toml", ".html", ".css", ".js", ".ts", ".sh", ".xml",
}


@client.event
async def on_message(message):
    # Don't respond to ourselves
    if message.author == client.user:
        return
    # Don't respond to other bots
    if message.author.bot:
        return

    if handler is None:
        await message.channel.send("Still starting up — try again in a moment.")
        return

    normalized = adapter.inbound(message)

    # Process Discord attachments: download text files into context for the handler
    if message.attachments:
        text_attachments = []
        binary_rejections = []
        for att in message.attachments:
            ext = Path(att.filename).suffix.lower()
            if ext in _TEXT_EXTENSIONS:
                try:
                    raw = await att.read()
                    content = raw.decode("utf-8")
                    text_attachments.append({"filename": att.filename, "content": content})
                except Exception as exc:
                    logger.warning("Failed to read attachment %s: %s", att.filename, exc)
                    binary_rejections.append(att.filename)
            else:
                binary_rejections.append(att.filename)

        if text_attachments:
            if normalized.context is None:
                normalized.context = {}
            normalized.context["attachments"] = text_attachments

        if binary_rejections:
            rejection_note = (
                "I can only handle text files right now — "
                f"{', '.join(binary_rejections)} cannot be processed (binary or unreadable)."
            )
            await message.channel.send(rejection_note)
            if not text_attachments and not message.content:
                return

    async with message.channel.typing():
        response_text = await handler.process(normalized)
    for chunk in _chunk_response(response_text):
        await message.channel.send(chunk)


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set in .env")
    client.run(token)
