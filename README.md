# Kernos

<img width="180" height="180" alt="Kernos" src="https://github.com/user-attachments/assets/121632b6-9811-4c8e-8126-4f44f2f1ca9f" align="right" />

**A personal agent that works around the clock and earns its keep one correct small action at a time.**

<br clear="right"/>

---

## What Kernos is

Kernos is a personal agent runtime built around a simple promise: talk to it in plain language, and it frames your request against an awareness of context that goes far back chronologically, with attention tightly scoped to the domain you're in. The relevant context arrives when it's needed, the right tool is already at hand, and the things worth remembering are remembered without you curating them. When the job gets technical, Kernos writes the code, finds the API, wires the integration, and files the result — all within the same conversation.

Most agent harnesses (OpenClaw, Hermes, the typical LangChain or CrewAI assembly) run a single agent loop where every system concern competes for the agent's attention inline: memory retrieval, safety checks, tool routing, multi-member disclosure, skill selection. Every turn, everything pours into the same context. Kernos splits that. One principal agent handles the conversation. A half-dozen bounded **cohort agents** — specialized LLM workers for routing, gating, fact extraction, cross-member disclosure judgment, friction observation — run around it without ever appearing in its context. Judgment work runs on LLMs; state work runs in Python.

The point of this shape is cognitive focus. The principal agent receives a curated, orchestrated context each turn and spends its full attention producing the best possible response — never disoriented by meta-concerns about which tool to look up, which policy applies, or whether disclosure is warranted. What sticks in the principal's thread is a cohesive, relevant conversation. Cohorts leverage small, specialized units of judgment to surface what's critical and keep what isn't out of the way. The principal agent never sees the cohorts, and the cohorts never see each other.

The pieces you'd normally wire together in an agent framework — orchestration, retrieval, safety enforcement, tool routing, disclosure logic — are not surfaced elements here. Neither the user nor the agent needs to learn a syntax or invocation pattern to access them. The only systems that surface are the ones relevant to the cognitive conversation. Everything else happens around that conversation, not inside it.

---

## One agent, many domains

A typical agent session is one conversation thread that grows until it breaks. Spin up a second thread and you have two agents who don't know each other. Kernos runs **multiple parallel context spaces** per member — work, personal, a specific project, a research sprint — each with its own ledger, its own facts, its own promoted tool set, its own compaction rhythm.

The magic is that neither you nor the agent ever sees this happening. You keep one continuous conversation; the agent delicately weaves it into whichever specialized domain the topic belongs to. A single endless conversation, routed across many specialist threads, invisibly. The router does this work before the agent sees the turn.

Switching between domains doesn't mean starting over. Move from the work space to the personal space mid-conversation, come back an hour later, pick up where you left off — the agent holds the thread on both sides. The moment in most tools where you say *"I just talked about that, did you forget?"* doesn't happen here. It gets it.

Each space becomes a specialist in its own domain — a deep-memory thread tuned for that domain's work, a tool set promoted by that domain's patterns — while the agent underneath remains one continuous identity that knows everything is *yours*.

The practical effect: **100 domains in Kernos is better than 100 chat threads with one model.** 100 chat threads forget you and forget each other. Kernos specializes the cognition surface per-domain without siloing the person behind it.

---

## Architectural contributions

|  |  |
| --- | --- |
| **Cohort architecture** | One principal agent surrounded by bounded specialist LLM workers — routing, gating, fact extraction, disclosure judgment, friction observation — that run around the agent without ever appearing in its context. Judgment work on LLMs; state work in Python. Most harnesses run one agent loop with every system concern competing for attention inline; Kernos splits that so the principal agent keeps its full attention on the conversation. |
| **Context spaces** | Multiple parallel context spaces per member, each with its own memory, tool promotion, and compaction boundary. Invisible to the user and the agent — a single conversation routes transparently across specialist domains. See [One agent, many domains](#one-agent-many-domains) above. |
| **Dual memory: Ledger + Facts** | Two stores, two jobs. **Ledger** holds the conversational arc, compressed at compaction boundaries rather than summarized turn-by-turn. **Facts** holds structured knowledge, reconciled in a single LLM call against the existing store rather than extracted per-turn and deduplicated after the fact. Lossless narrative retrieval and deduplicated fact supersession, both at once. |
| **Multi-member disclosure layering** | One hatched agent per member, not per install. A relationship matrix declares permissions between members; a Messenger cohort sits above permissions and evaluates whether a response serves the disclosing member's welfare. Your spouse *can* see your calendar, but Kernos still makes a judgment about the therapy appointment. |
| **Infrastructure-level safety** | Most agent systems gate what the agent can reach. Kernos gates what the agent does, under which covenant, under which initiator context. Every tool call passes through a gate that classifies effect (`read` / `soft_write` / `hard_write`) and evaluates it against user-declared covenants. Reactive soft-writes pass. Hard-writes gate. Non-reactive paths gate. Covenant violations surface as conflicts the agent must resolve — not as silent denials. Safety as behavioral shaping, not access control. |
| **Cognitive UI grammar** | The system prompt as a typed document with named zones — RULES, ACTIONS, NOW, STATE, RESULTS, PROCEDURES, MEMORY — cacheable prefix, and provenance tags on every knowledge fragment. The runtime refreshes zones selectively without rebuilding the prompt. The agent knows where every piece of context came from. |

---

## Capability surface

|  |  |
| --- | --- |
| **Multi-channel presence** | Discord, SMS via Twilio, Telegram. One handler, one identity across channels. Adding a new platform is ~150 lines. |
| **Agentic workspace** | The agent writes Python in a sandboxed subprocess, exercises it live, and registers it as a first-class tool in the universal catalog. The discipline: build the smallest durable handle that removes recurring friction. 50-line helpers, not frameworks. |
| **Self-directed execution** | `manage_plan` creates multi-phase plans with budget ceilings. Each step runs the full turn pipeline with three-tier resilience — provider failover, step retries with exponential backoff, and hourly slow-poll after fast retries exhaust. Plans survive restarts; active plans rediscover themselves on startup. |
| **Friction-driven improvement** | A friction observer watches the turn trace. When patterns emerge — repeated failures, recurring confusions, missing primitives — the system proposes covenant changes, new procedures, and concrete spec drafts grounded in live evidence. |
| **Provider flexibility** | Anthropic, OpenAI Codex, or Ollama behind a `Provider` ABC. Three named fallback chains — `primary` / `simple` / `cheap` — built by a single chain builder. Swap providers without touching the agent. |

---

## Quick install

```
git clone https://github.com/theman8631/Kernos.git
cd Kernos
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # fill in API keys
python kernos/server.py
```

Requires Python 3.11+, an LLM API key (Anthropic, OpenAI/Codex, or Ollama), and at least one messaging adapter credential (Discord token, Twilio, or Telegram bot token). Node.js is required for MCP servers that run via `npx`.

**[Full install guide →](docs/install.md)**

---

## Architecture

- **[Cohort architecture](docs/architecture/cohort-and-judgment.md)** — judgment-vs-plumbing discipline, the principal-never-sees-cohorts rule
- **[Context spaces](docs/architecture/context-spaces.md)** — parallel domain threads with routed turn assignment
- **[Memory: Ledger and Facts](docs/architecture/memory.md)** — two stores reconciled at compaction boundaries
- **[Multi-member disclosure layering](docs/architecture/disclosure-and-messenger.md)** — welfare-first cross-member exchange
- **[Infrastructure-level safety](docs/architecture/safety-and-gate.md)** — behavioral contracts at the kernel
- **[Cognitive UI grammar](docs/architecture/cognitive-ui.md)** — the system prompt as a typed document

**[Full architecture →](docs/architecture/overview.md)** · **[Pipeline reference →](docs/architecture/pipeline-reference.md)** · **[Primitives →](docs/architecture/primitives-reference.md)**

---

## Design frames

Three load-bearing patterns that recur across the codebase.

**The Skill Model.** The universal tool catalog is one registry merging kernel tools, MCP tools, and workspace-built tools. Three-tier surfacing: common check per turn (no LLM call), LLM catalog scan on miss, promotion on successful use. The agent never browses its toolbox — it gets a budgeted window of the tools that matter right now.

**Judgment-vs-Plumbing.** Judgment work — semantic understanding, contextual evaluation, disclosure decisions — runs as bounded cohorts. Plumbing — lookups, serialization, dispatch, state mutation — runs as Python and never enters an LLM context. The principal agent never sees a cohort; cohorts never see each other.

**The Action Loop.** Every turn is six phases: Provision, Route, Assemble, Reason, Consequence, Persist. Tool calls go through the dispatch gate with effect classification. Completed actions leave receipts the agent reads on the next turn. The loop is uniform across reactive, proactive, and self-directed work.

**[Full design frames →](docs/design-frames/)**

---

## V2 direction

V1 is a reactive runtime with ambient extensions. V2 inverts the shape: a continuous **Cognition Kernel** running per member, maintaining a structured **World Model** across fused streams (conversation, calendar, email, location, plan state), running idle-cycle reflection and projection passes, and surfacing through a single aggressive relevance filter. Turns become privileged consumers of a running process rather than the engine itself.

V1's covenant system, dispatch gate, Messenger cohort, sensitivity classification, and stewardship are precisely the alignment fabric a continuous-cognition layer needs to stay trustworthy. **V1 is the alignment substrate; V2 is the cognition layer built on it.**

**[V2 direction →](docs/v2/direction.md)** · **[Alignment substrate →](docs/v2/alignment-substrate.md)** · **[Roadmap →](docs/roadmap.md)**

---

## License

MIT — see [LICENSE](LICENSE). Built by [@theman8631](https://github.com/theman8631).

<sub>Status — 1,980 tests · 52 eval scenarios · production-shaped runtime, self-hosted single-process.</sub>

---

*The agent thinks. The kernel remembers, notices, routes, and protects.*
