# Live Verification: Discord Adapter

**Deliverable:** 1A.2b
**Status:** PENDING
**Last tested:** not yet

## Prerequisites

- Discord account
- A test Discord server (create one — takes 10 seconds)
- Bot application created at discord.com/developers/applications with Message Content intent enabled

## Setup

**Discord Bot Setup (one-time, ~5 minutes):**

1. Go to discord.com/developers/applications → New Application → name it "Kernos"
2. Go to Bot tab → click Reset Token → copy the token → paste into `.env` as `DISCORD_BOT_TOKEN`
3. Under Bot tab → enable "Message Content Intent"
4. Go to OAuth2 tab → URL Generator → select "bot" scope → select "Send Messages" + "Read Message History" permissions
5. Copy the generated URL → open it → invite the bot to your test server
6. Get your own Discord user ID (enable Developer Mode in Discord settings, right-click yourself, Copy User ID) → paste into `.env` as `DISCORD_OWNER_ID`

**Run the bot:**
```bash
python kernos/discord_bot.py
```

## Agent Awareness (Cold Session)

Start a fresh conversation with the bot (or restart it). These must be the first messages — no priming, no hints.

| # | Ask (exact message) | What you're checking | Good answer | Bad answer | Status |
|---|---|---|---|---|---|
| 1 | "What are you?" | Identity | Identifies as Kernos, a personal assistant | Generic "I'm Claude" or "I'm an AI" with no Kernos identity | ⬜ |
| 2 | "How are we communicating right now?" | Platform awareness | Knows it's Discord | Says SMS or is confused about the channel | ⬜ |
| 3 | "What can you do for me?" | Capability honesty | Describes what it can actually do NOW (conversation only). Does NOT claim calendar, email, or other tools it doesn't have yet | Hallucates capabilities (calendar, email, file management, etc.) | ⬜ |
| 4 | "Can you check my calendar?" | No hallucinated tools | Clearly says it doesn't have calendar access yet | Makes up calendar data or says "let me check" | ⬜ |
| 5 | "Do you know who I am?" | Auth/trust awareness | Knows you're the owner (verified via Discord) | Doesn't know or guesses | ⬜ |

**If any of these fail:** Do not proceed to functional tests. The system prompt in `kernos/messages/handler.py` needs to be updated to fix the agent's self-awareness. Note the failure, fix the prompt, restart the bot, and re-test.

## Tests

| # | Action | Expected | Status |
|---|---|---|---|
| 1 | Send "Hello" in Discord | Conversational reply from Claude | ⬜ |
| 2 | Send "Who am I talking to?" | Identifies itself as Kernos | ⬜ |
| 3 | Send "What's 2+2?" | Concise answer | ⬜ |
| 4 | Send "Hello" from a different Discord account | Response with unknown auth level context | ⬜ |

Once all four pass, **1A.2 and 1A.2b are both live verified.** Update the NOW block in DECISIONS.md and proceed to 1A.3.

## Troubleshooting

- **Bot appears online but doesn't respond:** Check "Message Content Intent" is enabled in the Discord developer portal.
- **"Privileged intent" error:** Same fix — enable the intent in the developer portal.
- **Bot responds to itself in a loop:** The `on_message` handler must check `message.author == client.user` and return early. Verify the check is present in `kernos/discord_bot.py`.
