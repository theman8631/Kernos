# Live Verification: Google Calendar via MCP

**Deliverable:** 1A.3
**Status:** PENDING
**Last tested:** not yet

## Prerequisites

- 1A.2 or 1A.2b live verified
- Google Cloud project with Calendar API enabled
- OAuth 2.0 credentials (Desktop App type) downloaded as JSON
- `@cocal/google-calendar-mcp` auth flow completed (tokens saved locally)
- Node.js installed
- At least one event on your Google Calendar today or tomorrow

## Setup

Same as 1A.2 (uvicorn + ngrok + Twilio) or 1A.2b (Discord bot). No additional infra needed — the MCP server starts as a subprocess when the app starts.

Ensure `.env` has:
```
GOOGLE_OAUTH_CREDENTIALS_PATH=/path/to/your/gcp-oauth.keys.json
```

If you haven't run the OAuth flow yet:
```bash
GOOGLE_OAUTH_CREDENTIALS=/path/to/your/gcp-oauth.keys.json npx @cocal/google-calendar-mcp auth
```
A browser opens — authorize with your Google account. Tokens are saved locally and reused on future starts.

## Agent Awareness (Cold Session)

Start a fresh conversation. These must be the first messages — no priming.

| # | Ask (exact message) | What you're checking | Good answer | Bad answer | Status |
|---|---|---|---|---|---|
| 1 | "What are you?" | Identity | Identifies as Kernos | Generic AI identity | ⬜ |
| 2 | "What tools do you have access to?" | Tool awareness | Mentions calendar access. Does NOT claim email, file management, or other tools it doesn't have | Doesn't know about calendar, or hallucinates extra tools | ⬜ |
| 3 | "How would you check my schedule?" | Tool usage understanding | Describes using calendar tools, not guessing | Says it would guess or make something up | ⬜ |
| 4 | "Can you send an email for me?" | No hallucinated tools | Clearly says it can't do email yet | Pretends it can or tries to | ⬜ |
| 5 | "Do you know who I am?" | Auth/trust awareness | Knows you're the owner | Doesn't know | ⬜ |

**If any of these fail:** Fix the system prompt or tool definitions before proceeding to functional tests.

## Tests

| # | Action | Expected | Status |
|---|---|---|---|
| 1 | Send "What's on my schedule today?" | Lists real events from your calendar | ⬜ |
| 2 | Send "Do I have anything tomorrow?" | Real calendar data or "nothing scheduled" | ⬜ |
| 3 | Send "What's the capital of France?" | "Paris" — no tool use, no regression | ⬜ |
| 4 | Send "What's my next meeting?" | Real calendar data via natural phrasing | ⬜ |
| 5 | Send "What's on my schedule [empty day]?" | Friendly "nothing scheduled" message | ⬜ |

## Troubleshooting

- **App starts but no calendar tools in logs:** Check `GOOGLE_OAUTH_CREDENTIALS_PATH` in `.env`. Run the MCP auth flow again.
- **"I don't have access to your calendar" response:** MCP server failed to connect. Check Node.js is installed. Verify `npx @cocal/google-calendar-mcp` runs manually.
- **Calendar data is wrong:** Verify you're querying the right Google account (the one you authed with).
