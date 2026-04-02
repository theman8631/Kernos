"""Kernos FastAPI server — webhook-based SMS inbound.

This is the cloud deployment path for receiving SMS via Twilio webhooks.
For local/development use, SMS inbound uses polling via SMSPoller in server.py.
Run this when deploying to a server with a public URL.

    uvicorn kernos.app:app --host 0.0.0.0 --port 8000
"""
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mcp import StdioServerParameters

load_dotenv()

import dataclasses

from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
from kernos.kernel.credentials import resolve_anthropic_credential
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_twilio_adapter = TwilioSMSAdapter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect MCP servers, init stores, emit system.started. Shutdown: inverse."""
    data_dir = os.getenv("KERNOS_DATA_DIR", "./data")

    events = JsonEventStream(data_dir)
    state = JsonStateStore(data_dir)

    try:
        await emit_event(
            events, EventType.SYSTEM_STARTED, "system", "app", payload={}
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

    conversations = JsonConversationStore(data_dir)
    tenants = JsonTenantStore(data_dir)
    audit = JsonAuditStore(data_dir)

    provider_name = os.getenv("KERNOS_LLM_PROVIDER", "anthropic")
    if provider_name == "openai-codex":
        from kernos.kernel.credentials import resolve_openai_codex_credential
        from kernos.kernel.reasoning import OpenAICodexProvider
        provider = OpenAICodexProvider(credential=resolve_openai_codex_credential())
    else:
        provider = AnthropicProvider(api_key=resolve_anthropic_credential())
    reasoning = ReasoningService(provider, events, mcp_manager, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)
    app.state.handler = MessageHandler(
        mcp_manager, conversations, tenants, audit, events, state, reasoning, registry, engine,
        secrets_dir=os.getenv("KERNOS_SECRETS_DIR", "./secrets"),
    )
    logger.info("MessageHandler ready (data_dir=%s)", data_dir)

    yield

    try:
        await emit_event(
            events, EventType.SYSTEM_STOPPED, "system", "app", payload={}
        )
    except Exception as exc:
        logger.warning("Failed to emit system.stopped: %s", exc)

    await app.state.handler.shutdown_runners()
    await mcp_manager.disconnect_all()


app = FastAPI(title="Kernos", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "0.1.0"})


@app.post("/sms/inbound")
async def sms_inbound(request: Request) -> Response:
    """
    Twilio SMS webhook endpoint.

    Wiring: Twilio adapter (inbound) → handler → Twilio adapter (outbound)
    """
    # TODO: Validate Twilio request signature before processing in production.
    form_data = await request.form()
    raw = dict(form_data)

    logger.info("Inbound SMS from=%s body=%r", raw.get("From"), raw.get("Body"))

    try:
        handler: MessageHandler = request.app.state.handler
        message = _twilio_adapter.inbound(raw)
        response_text = await handler.process(message)
        if not response_text:  # Merged message — no reply needed
            return Response(content="<Response/>", media_type="application/xml")
        twiml = _twilio_adapter.outbound(response_text, message)
        logger.info("Response to=%s twiml=%r", message.sender, twiml)
        return Response(content=twiml, media_type="application/xml")
    except Exception as exc:
        logger.error("Unhandled error in sms_inbound: %s", exc, exc_info=True)
        error_twiml = (
            "<Response><Message>Something went wrong. Please try again.</Message></Response>"
        )
        return Response(content=error_twiml, media_type="application/xml")
