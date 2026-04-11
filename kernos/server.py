import json
import logging
import os
import shutil
from pathlib import Path

import sys

import discord
from discord import app_commands
from dotenv import load_dotenv
from mcp import StdioServerParameters

import dataclasses

from kernos.kernel.credentials import resolve_anthropic_credential
from kernos.messages.adapters.discord_bot import DiscordAdapter
from kernos.messages.handler import MessageHandler
from kernos.capability.client import AuthCommand, MCPClientManager
from kernos.capability.known import KNOWN_CAPABILITIES
from kernos.capability.registry import CapabilityRegistry, CapabilityStatus
from kernos.kernel.event_types import EventType
from kernos.kernel.events import JsonEventStream, emit_event
from kernos.kernel.engine import TaskEngine
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonTenantStore

load_dotenv()


class _ColorFormatter(logging.Formatter):
    """Console formatter that color-codes log lines by event type."""

    # ANSI color codes
    _COLORS = {
        "ROUTE": "\033[36m",        # cyan — routing decisions
        "SPACE_SWITCH": "\033[35m",  # magenta — space changes
        "TOOL_": "\033[33m",         # yellow — tool surfacing/budget/promotion
        "REASON_": "\033[32m",       # green — reasoning/LLM
        "LLM_": "\033[32m",          # green — LLM calls
        "CODEX_": "\033[32m",        # green — provider
        "COMPACTION": "\033[34m",    # blue — compaction
        "FACT_HARVEST": "\033[34m",  # blue — fact harvest
        "DOMAIN_": "\033[35m",       # magenta — domain creation/migration
        "GATE": "\033[91m",          # bright red — gate decisions
        "PLAN_": "\033[95m",         # bright magenta — plan execution
        "CODE_EXEC": "\033[93m",     # bright yellow — code execution
        "WORKSPACE": "\033[93m",     # bright yellow — workspace
        "CROSS_DOMAIN": "\033[96m",  # bright cyan — cross-domain signals
        "AWARENESS": "\033[96m",     # bright cyan — awareness
        "WARNING": "\033[91m",       # bright red — warnings
        "ERROR": "\033[91m",         # bright red — errors
        "FRICTION": "\033[91m",      # bright red — friction
        "MESSAGE_ANALYSIS": "\033[36m",  # cyan — message analyzer
        "PHASE_TIMING": "\033[90m",  # gray — timing (low priority)
        "TURN_TIMING": "\033[90m",   # gray — timing
    }
    _RESET = "\033[0m"

    def format(self, record):
        msg = super().format(record)
        # Check for event prefixes in the message
        for prefix, color in self._COLORS.items():
            if prefix in record.getMessage():
                return f"{color}{msg}{self._RESET}"
        # Color by level
        if record.levelno >= logging.ERROR:
            return f"\033[91m{msg}{self._RESET}"
        if record.levelno >= logging.WARNING:
            return f"\033[93m{msg}{self._RESET}"
        return msg


_handler = logging.StreamHandler()
_handler.setFormatter(_ColorFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"))
logging.root.addHandler(_handler)
logging.root.setLevel(logging.INFO)
# Prevent duplicate output from basicConfig
for h in logging.root.handlers:
    if h is not _handler:
        logging.root.removeHandler(h)

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
adapter = DiscordAdapter()

OWNER_USER_ID = int(os.getenv("DISCORD_OWNER_ID", "0"))
_PENDING_CONFIRMATION_PATH = Path("/tmp/kernos_pending_confirmation.json")


def _write_pending_confirmation(channel_id: int, message: str, delete_message_id: int = 0) -> None:
    """Write a pending confirmation file for the new process to pick up."""
    data = {"channel_id": channel_id, "message": message}
    if delete_message_id:
        data["delete_message_id"] = delete_message_id
    _PENDING_CONFIRMATION_PATH.write_text(json.dumps(data))


@tree.command(name="restart", description="Restart the Kernos bot")
async def restart_command(interaction: discord.Interaction) -> None:
    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    logger.info("Restart requested by %s", interaction.user)
    await interaction.response.send_message("Restarting...", ephemeral=True)
    restart_msg = await interaction.channel.send("⏳")
    _write_pending_confirmation(interaction.channel_id, "Ready.", delete_message_id=restart_msg.id)
    os.execv(sys.executable, [sys.executable] + sys.argv)


@tree.command(name="wipe", description="Wipe all data and start fresh (factory reset)")
async def wipe_command(interaction: discord.Interaction) -> None:
    global handler

    if interaction.user.id != OWNER_USER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    data_dir = Path(os.getenv("KERNOS_DATA_DIR", "./data"))

    await interaction.response.send_message("Wiping...", ephemeral=True)
    await client.change_presence(activity=discord.Activity(
        type=discord.ActivityType.playing, name="factory reset..."))
    wipe_msg = await interaction.channel.send("⏳")
    _write_pending_confirmation(interaction.channel_id, "Ready.", delete_message_id=wipe_msg.id)

    # 1. Null the handler so on_message rejects new messages during wipe.
    current_handler = handler
    handler = None

    # 2. Stop the awareness evaluator — it writes to data/ on a timer.
    if current_handler and getattr(current_handler, "_evaluator", None):
        try:
            await current_handler._evaluator.stop()
            logger.info("Wipe: awareness evaluator stopped")
        except Exception as exc:
            logger.warning("Wipe: failed to stop evaluator: %s", exc)

    # 3. Disconnect MCP servers — release file handles and child processes.
    if current_handler and current_handler.mcp:
        try:
            await current_handler.mcp.disconnect_all()
            logger.info("Wipe: MCP servers disconnected")
        except Exception as exc:
            logger.warning("Wipe: failed to disconnect MCP: %s", exc)

    # 4. Delete everything inside data/ — produces a truly blank state.
    #    .env and secrets/ live outside data/ so they are never touched.
    #    All tenant data (conversations, state, events, spaces, awareness,
    #    compaction, audit, archive) lives under data/{tenant_id}/.
    if data_dir.exists():
        shutil.rmtree(data_dir)
        logger.info("Wipe: removed %s", data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Wipe: recreated empty %s", data_dir)

    # 5. Restart the process — all in-memory state is discarded.
    os.execv(sys.executable, [sys.executable] + sys.argv)


# None until on_ready completes MCP setup.
handler: MessageHandler | None = None


@client.event
async def on_ready():
    global handler
    logger.info("Starting Kernos server")
    instance_id = os.getenv("KERNOS_INSTANCE_ID", "")
    if instance_id:
        logger.info("INSTANCE: id=%s (from KERNOS_INSTANCE_ID)", instance_id)
    else:
        logger.info("INSTANCE: id derived per-adapter (set KERNOS_INSTANCE_ID for cross-channel identity)")
    logger.info("Discord adapter connected as %s", client.user)

    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
    events = JsonEventStream(data_dir)
    state = JsonStateStore(data_dir)

    try:
        await emit_event(
            events, EventType.SYSTEM_STARTED, "system", "server", payload={}
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
        mcp_manager.register_auth_command(
            "google-calendar",
            AuthCommand(
                command="npx",
                args=["@cocal/google-calendar-mcp", "auth", "normal"],
                env={"GOOGLE_OAUTH_CREDENTIALS": credentials_path},
                probe_tool="get-current-time",
            ),
        )
    else:
        logger.warning(
            "GOOGLE_OAUTH_CREDENTIALS_PATH not set — calendar tools unavailable"
        )

    brave_api_key = os.getenv("BRAVE_API_KEY", "")
    if brave_api_key:
        mcp_manager.register_server(
            "brave-search",
            StdioServerParameters(
                command="npx",
                args=["-y", "@modelcontextprotocol/server-brave-search"],
                env={"BRAVE_API_KEY": brave_api_key},
            ),
        )
    else:
        logger.warning("BRAVE_API_KEY not set — web search tools unavailable")

    lightpanda_path = os.getenv("LIGHTPANDA_PATH", os.path.expanduser("~/bin/lightpanda"))
    if Path(lightpanda_path).is_file():
        mcp_manager.register_server(
            "lightpanda",
            StdioServerParameters(
                command=lightpanda_path,
                args=["mcp"],
            ),
        )
    else:
        logger.warning(
            "Lightpanda binary not found at %s — web browser tools unavailable. "
            "Set LIGHTPANDA_PATH or install to ~/bin/lightpanda",
            lightpanda_path,
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
        cap = registry.get(server_name) or registry.get_by_server_name(server_name)
        if cap:
            cap.status = CapabilityStatus.CONNECTED
            cap.tools = [t["name"] for t in tools]
    connected = [c.name for c in registry.get_connected()]
    logger.info("Capability registry ready — connected: %s", connected or "none")

    provider_name = os.getenv("KERNOS_LLM_PROVIDER", "anthropic")
    if provider_name == "openai-codex":
        from kernos.kernel.credentials import resolve_openai_codex_credential
        from kernos.kernel.reasoning import OpenAICodexProvider
        provider = OpenAICodexProvider(credential=resolve_openai_codex_credential())
    elif provider_name == "ollama":
        from kernos.providers.ollama_provider import OllamaProvider
        provider = OllamaProvider()
    else:
        provider = AnthropicProvider(api_key=resolve_anthropic_credential())

    # Build fallback chain (automatic failover: primary → fallback1 → fallback2 → ...)
    # KERNOS_LLM_FALLBACK is comma-separated: "ollama:glm-5.1:cloud,ollama:gemma4:31b-cloud"
    fallback_providers: list = []
    fallback_spec = os.getenv("KERNOS_LLM_FALLBACK", "")
    if fallback_spec:
        for entry in fallback_spec.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                if entry.startswith("ollama:"):
                    from kernos.providers.ollama_provider import OllamaProvider
                    _model = entry[len("ollama:"):]
                    fb = OllamaProvider(model=_model)
                    fallback_providers.append(fb)
                    logger.info("Fallback provider ready: ollama/%s", _model)
                elif entry == "ollama":
                    from kernos.providers.ollama_provider import OllamaProvider
                    fb = OllamaProvider()
                    fallback_providers.append(fb)
                    logger.info("Fallback provider ready: ollama (default model)")
                elif entry == "openai-codex":
                    from kernos.kernel.credentials import resolve_openai_codex_credential
                    from kernos.kernel.reasoning import OpenAICodexProvider
                    fb = OpenAICodexProvider(credential=resolve_openai_codex_credential())
                    fallback_providers.append(fb)
                    logger.info("Fallback provider ready: openai-codex")
                elif entry == "anthropic":
                    fb = AnthropicProvider(api_key=resolve_anthropic_credential())
                    fallback_providers.append(fb)
                    logger.info("Fallback provider ready: anthropic")
            except Exception as exc:
                logger.warning("Failed to init fallback provider %s: %s", entry, exc)

    reasoning = ReasoningService(provider, events, mcp_manager, audit, fallback_providers=fallback_providers)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(mcp_manager, conversations, tenants, audit, events, state, reasoning, registry, engine, secrets_dir=os.getenv("KERNOS_SECRETS_DIR", "./secrets"))
    handler.register_mcp_tools_in_catalog()

    logger.info("MessageHandler ready (data_dir=%s)", data_dir)

    # Register adapters and channels for outbound messaging
    adapter.set_client(client)
    handler.register_adapter("discord", adapter)
    handler.register_channel(
        name="discord", display_name="Discord", platform="discord",
        can_send_outbound=True, channel_target="",  # Updated per-message
    )

    # Register SMS channel if Twilio credentials are configured
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_phone = os.getenv("TWILIO_PHONE_NUMBER", "")
    if twilio_sid and twilio_token and twilio_phone:
        from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
        sms_adapter = TwilioSMSAdapter()
        handler.register_adapter("sms", sms_adapter)
        owner_phone = os.getenv("OWNER_PHONE_NUMBER", "")
        handler.register_channel(
            name="sms", display_name="Twilio SMS", platform="sms",
            can_send_outbound=True, channel_target=owner_phone,
        )

        # Start SMS polling for inbound messages (no webhook needed)
        from kernos.sms_poller import SMSPoller
        sms_poller = SMSPoller(
            adapter=sms_adapter, handler=handler,
            account_sid=twilio_sid, auth_token=twilio_token,
            twilio_number=twilio_phone,
            interval=float(os.getenv("KERNOS_SMS_POLL_INTERVAL", "30")),
        )
        await sms_poller.start()
        logger.info(
            "SMS channel registered — polling interval=%ss, outbound to %s",
            sms_poller._interval, owner_phone,
        )

    # CLI is always registered but can't push
    handler.register_channel(
        name="cli", display_name="CLI Terminal", platform="cli",
        can_send_outbound=False,
    )

    # Send pending confirmation from a prior /restart or /wipe
    if _PENDING_CONFIRMATION_PATH.is_file():
        try:
            pending = json.loads(_PENDING_CONFIRMATION_PATH.read_text())
            channel = await client.fetch_channel(pending["channel_id"])

            # Delete the pre-restart placeholder (⏳)
            _del_id = pending.get("delete_message_id")
            if _del_id:
                try:
                    old_msg = await channel.fetch_message(int(_del_id))
                    await old_msg.delete()
                except Exception:
                    pass

            # Send "Ready." and auto-delete after 5 seconds
            conf_msg = await channel.send(pending["message"])
            logger.info("Sent pending confirmation to channel %s", pending["channel_id"])
            _PENDING_CONFIRMATION_PATH.unlink()

            async def _delete_after(msg, delay=5):
                import asyncio as _aio
                await _aio.sleep(delay)
                try:
                    await msg.delete()
                except Exception:
                    pass
            import asyncio as _aio
            _aio.create_task(_delete_after(conf_msg))
        except Exception as exc:
            logger.warning("Failed to send pending confirmation: %s", exc)

    # AwarenessEvaluator starts lazily per-tenant on first message
    # (handler._maybe_start_evaluator). No startup guessing needed.

    # Recover any plans interrupted by crash/restart
    try:
        await handler.recover_active_plans()
    except Exception as exc:
        logger.warning("Failed to recover active plans: %s", exc)

    await tree.sync()
    logger.info("Slash commands synced")





import random

_DEFAULT_THINKING_EMOJI = ["⏳", "🤔", "💭", "🧠", "👀", "💡", "🔍"]

DISCORD_MAX_LENGTH = 2000


def _chunk_response(text: str) -> list[str]:
    """Split text into chunks that fit Discord's 2000-char limit.

    Collapses triple+ newlines to double (prevents excessive spacing in Discord).
    Splits on newlines where possible; falls back to hard cuts.
    """
    import re
    text = re.sub(r'\n{3,}', '\n\n', text)
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


# Guard against duplicate Discord gateway deliveries.
# Discord can re-deliver events on gateway reconnect or missed ACKs.
_seen_message_ids: set[int] = set()
_SEEN_MAX = 200


@client.event
async def on_message(message):
    # Deduplicate gateway re-deliveries
    if message.id in _seen_message_ids:
        return
    _seen_message_ids.add(message.id)
    if len(_seen_message_ids) > _SEEN_MAX:
        # Discard oldest half to bound memory
        to_remove = sorted(_seen_message_ids)[:_SEEN_MAX // 2]
        _seen_message_ids.difference_update(to_remove)

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

    # Send placeholder, edit to final response (eliminates dead air)
    # Pick emoji from active space if available, else defaults
    _emoji_pool = _DEFAULT_THINKING_EMOJI
    try:
        _tp = await handler.state.get_tenant_profile(normalized.tenant_id)
        if _tp and _tp.last_active_space_id:
            _sp = await handler.state.get_context_space(normalized.tenant_id, _tp.last_active_space_id)
            if _sp and getattr(_sp, 'thinking_emoji', None):
                _emoji_pool = _sp.thinking_emoji
    except Exception:
        pass
    # Emoji placeholder (just the emoji, no text) + typing animation while processing
    placeholder = await message.channel.send(random.choice(_emoji_pool))
    try:
        async with message.channel.typing():
            response_text = await handler.process(normalized)
    except Exception as exc:
        logger.error("Handler error: %s", exc, exc_info=True)
        await placeholder.edit(content="Something went wrong — try again in a moment.")
        try:
            await message.add_reaction("⚠️")
        except Exception:
            pass
        return

    if not response_text:  # Merged message — response comes from primary turn
        try:
            await placeholder.delete()
        except Exception:
            pass
        return

    chunks = _chunk_response(response_text)
    await placeholder.edit(content=chunks[0])
    for chunk in chunks[1:]:
        await message.channel.send(chunk)


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set in .env")
    client.run(token)
