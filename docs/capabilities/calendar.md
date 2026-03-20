# Google Calendar

Google Calendar is a pre-installed, universal capability. It is available in every context space by default.

## What You Can Do

- Read the user's schedule (today, this week, specific dates)
- List events with details (time, location, attendees, description)
- Create new events
- Update existing events (reschedule, add attendees, change details)
- Delete events
- Find availability / free-busy times
- Get current time

## Available Tools

| Tool | Effect | Description |
|------|--------|-------------|
| get-current-time | read | Get the current date and time |
| list-events | read | List calendar events for a date range |
| search-events | read | Search events by text query |
| get-event | read | Get details of a specific event |
| create-event | hard_write | Create a new calendar event |
| update-event | hard_write | Update an existing event |
| delete-event | hard_write | Delete a calendar event |
| list-calendars | read | List available calendars |
| get-colors | read | Get available event colors |
| find-free-time | read | Find available time slots |
| list-recurring-instances | read | List instances of a recurring event |
| quick-add-event | hard_write | Create event from natural language |
| move-event | hard_write | Move event to a different calendar |

## Date Awareness

The current date and time are always injected into the system prompt — the agent knows what year, day, and time it is without calling any tool. The `get-current-time` tool is for precise timezone lookups, not basic date awareness.

## Setup

Requires Google OAuth credentials. The user connects their Google account through the credential handoff flow.

## Planned

- Calendar-triggered proactive awareness (e.g., "you have a meeting in 30 minutes")
