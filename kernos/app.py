import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mcp import StdioServerParameters

load_dotenv()

from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
from kernos.messages.handler import MessageHandler
from kernos.capability.client import MCPClientManager
from kernos.kernel.event_types import EventType
from kernos.kernel.events import JsonEventStream, emit_event
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonAuditStore, JsonConversationStore, JsonTenantStore

logging.basicConfig(level=logging.INFO)
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

    conversations = JsonConversationStore(data_dir)
    tenants = JsonTenantStore(data_dir)
    audit = JsonAuditStore(data_dir)

    provider = AnthropicProvider(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    reasoning = ReasoningService(provider, events, mcp_manager, audit)
    app.state.handler = MessageHandler(
        mcp_manager, conversations, tenants, audit, events, state, reasoning
    )
    logger.info("MessageHandler ready (data_dir=%s)", data_dir)

    yield

    try:
        await emit_event(
            events, EventType.SYSTEM_STOPPED, "system", "app", payload={}
        )
    except Exception as exc:
        logger.warning("Failed to emit system.stopped: %s", exc)

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
        twiml = _twilio_adapter.outbound(response_text, message)
        logger.info("Response to=%s twiml=%r", message.sender, twiml)
        return Response(content=twiml, media_type="application/xml")
    except Exception as exc:
        logger.error("Unhandled error in sms_inbound: %s", exc, exc_info=True)
        error_twiml = (
            "<Response><Message>Something went wrong. Please try again.</Message></Response>"
        )
        return Response(content=error_twiml, media_type="application/xml")
