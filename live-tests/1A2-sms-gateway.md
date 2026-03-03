# Live Verification: SMS Gateway

**Deliverable:** 1A.2
**Status:** PENDING
**Last tested:** not yet

## Prerequisites

- Twilio account (twilio.com — free trial works)
- Twilio phone number purchased (~$1.15/month)
- Anthropic API key (from console.anthropic.com)
- ngrok installed (`brew install ngrok` or ngrok.com/download)

## Setup

1. **Copy env file and fill in real values:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-...        # From console.anthropic.com
   TWILIO_ACCOUNT_SID=AC...            # From twilio.com/console
   TWILIO_AUTH_TOKEN=...               # From twilio.com/console
   TWILIO_PHONE_NUMBER=+1XXXXXXXXXX   # Your Twilio number
   OWNER_PHONE_NUMBER=+1XXXXXXXXXX    # Your personal phone number
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Start the app:**
   ```bash
   uvicorn kernos.app:app --reload --port 8000
   ```
   Confirm: you see `Uvicorn running on http://127.0.0.1:8000` and no errors.

4. **Start ngrok (separate terminal):**
   ```bash
   ngrok http 8000
   ```
   Copy the `https://...ngrok-free.app` URL it gives you.

5. **Point Twilio at your ngrok URL:**
   - Go to twilio.com/console/phone-numbers
   - Click your phone number
   - Under "Messaging" → "A message comes in"
   - Set to **Webhook**, paste: `https://YOUR-NGROK-URL/sms/inbound`
   - Method: **HTTP POST**
   - Save

## Tests

| # | Action | Expected | Status |
|---|---|---|---|
| 1 | Text "Hello" | Conversational reply from Claude | ⬜ |
| 2 | Text "Who am I talking to?" | Identifies itself as Kernos | ⬜ |
| 3 | Text "What's 2+2?" | Concise SMS-friendly answer | ⬜ |
| 4 | Kill uvicorn, text, restart | Twilio retries; response comes through once app is back | ⬜ |

## Troubleshooting

- **No response at all:** Check ngrok terminal — is the POST coming through? Check uvicorn logs for errors.
- **Twilio error message:** Your webhook URL might be wrong. Must end in `/sms/inbound`.
- **"Something went wrong" response:** Check your `ANTHROPIC_API_KEY` is valid.
- **ngrok free tier limits:** Free ngrok URLs change on restart. Update Twilio webhook each time. (Or use `ngrok http 8000 --domain=your-domain` with a paid ngrok plan.)
