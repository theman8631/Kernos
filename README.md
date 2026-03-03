# KERNOS

<img width="350" height="350" alt="ChatGPT Image Mar 2, 2026, 11_15_22 PM" src="https://github.com/user-attachments/assets/121632b6-9811-4c8e-8126-4f44f2f1ca9f" />

*Kernel for Extensible Runtime, Networked Orchestration Services*

## Architecture

```
[Twilio SMS webhook]
        │
        ▼
 TwilioSMSAdapter.inbound()
        │  translates form payload → NormalizedMessage
        ▼
  handle_message()           ← only sees NormalizedMessage, never Twilio
        │  calls Claude API, returns plain string
        ▼
 TwilioSMSAdapter.outbound()
        │  translates string → TwiML
        ▼
[Twilio delivers SMS]
```

**Critical constraint:** The handler (`kernos/messages/handler.py`) has zero imports
from adapters. The adapters have zero imports from the handler. They share only
`NormalizedMessage`. This is what makes adding Discord, Telegram, or voice a
matter of writing one new adapter file — not refactoring the kernel.

---

## Setup

### Prerequisites

- Python 3.11+
- Node.js (required for the Google Calendar MCP server, which runs via `npx`)

### 1. Clone and install

```bash
git clone https://github.com/theman8631/Kernos.git
cd Kernos
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `TWILIO_ACCOUNT_SID` | Twilio Console → Account Info |
| `TWILIO_AUTH_TOKEN` | Twilio Console → Account Info |
| `TWILIO_PHONE_NUMBER` | Twilio Console → Phone Numbers |
| `OWNER_PHONE_NUMBER` | Your personal phone number (E.164 format: `+12025550100`) |

### 3. Run the server

```bash
uvicorn kernos.app:app --reload --port 8000
```

### 4. Expose to Twilio (local dev)

Twilio needs a public URL to POST webhook events. Use [ngrok](https://ngrok.com):

```bash
ngrok http 8000
```

Copy the HTTPS URL (e.g. `https://abc123.ngrok.io`).

### 5. Configure Twilio webhook

In the Twilio Console:
1. Go to **Phone Numbers → Manage → Active Numbers**
2. Click your phone number
3. Under **Messaging → A message comes in**, set:
   - **Webhook URL:** `https://abc123.ngrok.io/sms/inbound`
   - **HTTP method:** `POST`
4. Save

### 6. Text your number

Send any SMS to your Twilio number. You'll get a Claude response back.

---

## Project structure

```
kernos/
├── app.py                          # FastAPI app, webhook wiring
└── messages/
    ├── models.py                   # NormalizedMessage dataclass + AuthLevel enum
    ├── handler.py                  # Claude API call — no platform knowledge
    └── adapters/
        ├── base.py                 # Abstract adapter interface
        └── twilio_sms.py           # Twilio-specific translation layer
```

---

## Health check

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"0.1.0"}
```

---

## Google Calendar Setup (Phase 1A.3)

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

---

## What's next (Phase 1A.4)

- Connect Google Calendar via MCP
- "What's on my schedule today?" works over SMS with real calendar data

See `KERNOS-BLUEPRINT.md` for the full implementation plan.
