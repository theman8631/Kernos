# Kernos

Kernos is a personal agentic operating system. It runs as a persistent agent that learns who you are, connects to the tools you need, and handles the work you shouldn't have to think about. You interact with it through text — SMS, Discord, or (soon) a web interface — and it manages the rest.

> **Start here:** [`kernos-introduction.md`](kernos-introduction.md) — the canonical introduction. Innovation overview plus a navigable map of every architecture, behavior, capability, identity, and roadmap surface in this docs tree. The running agent also reaches this document when asked what Kernos is.

## How It Works

Kernos has three layers:

1. **Adapters** receive messages from platforms (Discord, SMS) and normalize them into a common format.
2. **The Message Handler** orchestrates everything: routing messages to the right context space, assembling conversation history, calling the reasoning engine, extracting knowledge, and persisting state.
3. **The Kernel** provides the intelligence infrastructure: events, state, memory, reasoning, behavioral rules, file storage, capabilities, and proactive awareness.

The agent (currently Claude Sonnet) runs inside the handler's reasoning service. The kernel is not the agent — it's the infrastructure that gives the agent context, memory, tools, and boundaries. The kernel owns all state and safety enforcement. The agent's job is to think, respond, and use the tools the kernel surfaces.

## Core Principles

- **Proper stewardship.** You manage someone's digital life. Act accordingly.
- **Intent over instruction.** Understand what they mean, not just what they say.
- **Conservative by default.** When uncertain, ask. Never assume permission.
- **Honest about limits.** Say what you don't know. Never fabricate.
- **Own your mistakes.** If you get something wrong, acknowledge it and correct it.
- **Be yourself.** You have a personality that evolves with the relationship.
- **Memory is your responsibility.** You remember things automatically — the user shouldn't have to repeat themselves.

## Documentation Structure

This documentation is organized for quick retrieval. Read the section relevant to your question:

| Section | What it covers |
|---------|---------------|
| [architecture/](architecture/overview.md) | How the system works — kernel, handler, adapters, spaces, memory, events |
| [capabilities/](capabilities/overview.md) | What tools are available — calendar, email, web, files, memory search |
| [behaviors/](behaviors/covenants.md) | How the agent behaves — covenants, dispatch gate, proactive awareness |
| [identity/](identity/who-you-are.md) | Who the agent is — soul system, onboarding, personality evolution |
| [roadmap/](roadmap/vision.md) | Where this is going — vision, decided next steps, future directions |
