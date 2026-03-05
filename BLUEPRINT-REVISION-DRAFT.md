# Blueprint Revision Draft

> **What this is:** Replacement text for sections of KERNOS-BLUEPRINT.md that no longer reflect the project's actual architecture and direction. The founder reviews, adjusts, and commits. These changes bring the Blueprint into alignment with where KERNOS actually came from and where it's heading.
>
> **What changed:** KERNOS was originally planned as an assembly of existing projects (fork AIOS, integrate MemOS, port OpenFang patterns). What actually happened is that the architecture was derived from first principles, validated against real-world experience (OSBuilder interview), and implemented as its own system. The Blueprint should tell the true story.

---

## REPLACE: Part 3 — "Kernel: Fork AIOS" Section

**Old title:** Kernel: Fork AIOS (Rutgers University)

**New section:**

### Kernel: Built from First Principles

The KERNOS kernel architecture was derived from first-principles analysis of what traditional operating system concepts (process scheduling, memory management, resource abstraction, access control) look like when applied to LLM-based agents serving non-technical users.

- **Origin:** A February 26, 2026 conversation between the founder and Claude, deriving agentic OS pillars from traditional OS theory. Refined through architectural brainstorming and a structured interview with OSBuilder (OpenClaw's primary agent), whose real-world experience running a production agent system validated key design decisions and revealed critical failure modes.
- **Architecture:** Five primitives (Event Stream, State Store, Capability Graph, Reasoning Service, Task Engine) composing into three operational modes (reactive, proactive, generative). See `specs/KERNEL-ARCHITECTURE-OUTLINE-v2.md` for the full design.
- **Core inversion:** "The agent thinks, the kernel remembers." Unlike existing systems where agents manage their own memory, tool discovery, and safety enforcement, KERNOS separates reasoning (agent) from infrastructure (kernel). The agent receives pre-assembled context, reasons about the current moment, and returns a response. Everything else is kernel responsibility.
- **Language:** Python for faster iteration in the kernel. No external kernel codebase is forked or integrated — the kernel is KERNOS-native.

**Prior art acknowledged:** The AIOS project (Rutgers University, COLM 2025) independently validated the OS-as-metaphor approach for LLM agent systems. Their work on treating LLMs as cores and building scheduling abstractions was useful early validation that the conceptual direction was sound. However, AIOS solves a fundamentally different problem — efficient GPU resource scheduling for concurrent agent workloads — and shares no architectural DNA with KERNOS. KERNOS is an application-layer kernel mediating between user intent and AI capabilities, not a hardware resource scheduler.

---

## REPLACE: Part 3 — "Memory Layer: Integrate MemOS" Section

**Old title:** Memory Layer: Integrate MemOS (MemTensor)

**New section:**

### Memory Architecture: Open Design Problem

Long-term memory is the hardest unsolved architectural problem in KERNOS and the foundation of the project's competitive moat (Pillar 3: "Memory as the Moat"). The current implementation provides persistence infrastructure (event stream for raw history, State Store for structured facts), but the memory architecture — how the system retrieves, consolidates, decays, and composes knowledge over months and years of use — is not yet designed.

**What we have today:**

- An append-only Event Stream that captures everything that happens (messages, tool calls, agent actions, capability changes). This is a transaction log, not a query surface.
- A State Store that holds structured, queryable state (user facts, behavioral contracts, capability state). Currently implemented as simple file-based storage with tenant isolation.
- Memory projectors (designed, not yet built) that will extract facts from the event stream and write them to the State Store.

**What we need and don't yet have:**

- **Semantic retrieval** — finding relevant knowledge across thousands of accumulated facts, not just keyword lookup. "What did we discuss about the Portland project?" requires understanding, not string matching.
- **Memory consolidation** — compressing and strengthening frequently-accessed knowledge while gracefully decaying unused information. Without this, the State Store grows without bound and relevance degrades.
- **Temporal reasoning** — understanding when things happened and how knowledge has changed. "What did I tell you last March?" is a time-scoped query against accumulated knowledge.
- **Composable context assembly** — assembling the right subset of knowledge for a specific agent, task, and moment. The inline annotation pattern (placing relevant context exactly where it's relevant in the user's message) requires sophisticated retrieval and relevance scoring.
- **Provenance and versioning** — tracking where knowledge came from and how it's changed. "User's name is Greg" (stated directly) vs. "User prefers afternoon meetings" (inferred from 12 interactions) have different confidence levels and different update semantics.

**Research inputs for this design:**

| Project | What's relevant to us | Status |
|---|---|---|
| MemOS (MemTensor) | MemCube abstraction — portable, versionable, composable memory units with metadata and provenance. MCP integration. 159% improvement over OpenAI memory on temporal reasoning. | Evaluate for State Store implementation. The MemCube model (self-contained memory units with provenance) aligns well with our needs. |
| Mem0 | Production-grade memory layer with graph-based retrieval. Apache 2.0. Simpler than MemOS but battle-tested. | Evaluate as practical alternative or complement. |
| MemoryOS (BAI-LAB) | Hierarchical memory management (short-term, mid-term, long-term with distinct retrieval strategies). EMNLP 2025 Oral. | Study for hierarchical design patterns. |
| A-MEM (AIOS team) | Agentic memory research — how agents interact with structured memory. | Study for agent-memory interaction patterns. |

**Design principle:** The "agent thinks, kernel remembers" inversion means the kernel owns memory entirely. Agents don't manage, query, or maintain their own memory — they receive pre-assembled context from the kernel and return observations the kernel persists. Whatever memory architecture we adopt must support this inversion, not fight it.

**Timeline:** Memory architecture design begins after Phase 1B kernel foundation is complete. The current State Store abstraction is deliberately simple — it needs to be replaced with a real memory system, not incrementally extended. This is a design-first problem: get the architecture right before building.

---

## REPLACE: Part 3 — "Security Patterns: Learn from OpenFang" Section

**Old title:** Security Patterns: Learn from OpenFang (RightNow-AI)

**New section:**

### Security Model: Built for Our Threat Surface

KERNOS security is built around behavioral contracts as the primary safety mechanism, not access restriction (see "The Trust Model: Access vs. Contract" section). The system's actual threat surface differs significantly from local-execution agent systems:

**Our threat surface:**

- **Prompt injection** — malicious content in user messages, emails, or retrieved documents that attempts to override agent instructions
- **Credential exposure** — API keys, OAuth tokens, and user secrets must be isolated per tenant and never leaked through agent responses
- **Unauthorized MCP tool calls** — agents calling tools they shouldn't, or calling tools with parameters that violate behavioral contracts
- **Channel spoofing** — SMS/caller ID is trivially spoofable (see Sender Authentication section); unauthenticated channels get restricted capabilities
- **Cross-tenant data leakage** — one tenant's data appearing in another tenant's context (the most critical multi-tenancy concern)

**What we don't need to solve (yet):**

- WASM sandboxing for local code execution (we call cloud APIs, not running untrusted binaries)
- Merkle audit trails (our append-only event stream provides audit capability natively)
- Ed25519 manifest signing (no agent package distribution in Phase 1-2)

**Prior art acknowledged:** OpenFang (RightNow-AI) provided the conceptual insight that safety lives in behavioral specification, not in access restriction — a lesson learned from the OpenClaw incident. Their specific technical patterns (WASM sandboxing, taint tracking, manifest signing) are designed for a Rust-based local execution environment and are not directly applicable to KERNOS. However, their graduated approval gates and kill/pause/resume mechanisms informed our behavioral contract design. If KERNOS later supports local code execution or untrusted plugin installation, OpenFang's sandboxing patterns become directly relevant.

---

## REPLACE: Part 3 — "Protocol Stack" Section

**Old title:** Protocol Stack: Adopt Standards

**New section:**

### Protocol Stack

| Protocol | Purpose | Status |
|---|---|---|
| MCP (Model Context Protocol) | Capability abstraction — tool and data access | **Adopted.** In production. Core to Capability Graph. |
| OAuth 2.1 | Authorization and tool access | **Adopted.** Industry standard for capability connections. |
| A2A (Agent-to-Agent Protocol) | Inter-agent communication (enterprise/local) | **Monitor.** Google-backed, relevant when we build inter-user agent communication (Phase 4+). Not adopted yet — no current use case. |
| ANP (Agent Network Protocol) | Inter-agent communication (open internet) | **Monitor.** Emerging. Relevant for Phase 4+ marketplace and open agent ecosystem. |
| AG-UI (Agent-User Interaction Protocol) | Agent-to-frontend event streaming | **Evaluate for Phase 3.** Relevant when building the mobile app. Not adopted yet. |
| A2UI (Agent-to-UI) | Agent-generated interface widgets | **Evaluate for Phase 3.** Google-backed. Relevant for dynamic UI in the app layer. |
| W3C DID | Decentralized agent identity | **Monitor.** Speculative. Relevant if the ecosystem moves toward decentralized identity. No current use case. |

**Principle:** Adopt protocols when we have a production use case, not speculatively. MCP and OAuth earned their place through implementation. Everything else earns its place when its phase arrives.

---

## REPLACE: Part 6 — "Key Repositories to Fork/Clone" Section

**Old section:**

### Key Repositories to Fork/Clone

- github.com/agiresearch/AIOS (kernel foundation)
- github.com/agiresearch/Cerebrum (agent SDK)
- github.com/MemTensor/MemOS (memory layer)
- Study: github.com/RightNow-AI/openfang (security patterns)
- Study: github.com/mem0ai/mem0 (practical memory patterns)

**New section:**

### Reference Projects

The KERNOS kernel is built from first principles, not forked from any existing project. The following projects are reference material — studied for concepts and lessons, not integrated as dependencies.

| Project | What we learned | Current relevance |
|---|---|---|
| MemOS (MemTensor) | MemCube abstraction, memory provenance, temporal reasoning | **High.** Primary candidate for memory architecture design. Evaluate when designing the long-term memory system. |
| Mem0 | Production memory patterns, graph-based retrieval | **High.** Practical alternative or complement to MemOS for memory architecture. |
| OpenFang (RightNow-AI) | Behavioral contracts as safety mechanism, graduated trust | **Medium.** Conceptual lessons absorbed. Specific patterns relevant if we add local code execution. |
| AIOS (Rutgers) | Validated OS-as-metaphor for agent systems | **Low.** Architecturally unrelated. Solves GPU resource scheduling, not user-facing intelligence. |
| MemoryOS (BAI-LAB) | Hierarchical memory tiers | **Medium.** Study for memory architecture design. |

---

## REPLACE: Part 8 — "First Steps" Item 4

**Old:** Spend a few hours reading AIOS source — make a go/no-go decision on forking vs. reference-only

**New:** ~~Spend a few hours reading AIOS source~~ — **COMPLETE.** Evaluated, determined reference-only, later determined architecturally unrelated. KERNOS kernel designed from first principles. See kernel architecture outline.

---

## UPDATE: Deliverable 1A.1

**Old:** `1A.1 Evaluate AIOS codebase (read, don't fork yet — go/no-go on fork vs. reference-only)`

**New:** `1A.1 Evaluate AIOS codebase — COMPLETE. Decision: reference-only, later determined architecturally unrelated. Kernel designed from first principles.`

---

## UNCHANGED (confirmed congruent)

The following Blueprint sections were reviewed and remain aligned with the project's direction. No changes needed:

- **Part 1: Vision & Philosophy** — The pitch, core insight, design principles, and "Why Not Just Use Claude/ChatGPT?" section are the foundation. Still perfectly congruent.
- **Part 3: Non-Destructive Deletion / Shadow Archive** — In production. Core principle.
- **Part 3: The Trust Model (Access vs. Contract)** — Absorbed into behavioral contracts architecture. Core principle.
- **Part 3: Sender Authentication** — Channel trust levels implemented. Core principle.
- **Part 3: Primary Interface (Platform-Agnostic Messaging Layer)** — Normalized message format in production. Core architecture.
- **Part 3: Multi-Tenancy** — tenant_id on everything from day one. In production.
- **Part 3: Workspace Lifecycle & Onboarding** — Design is sound, implementation in later phases.
- **Part 3: Cloud Architecture** — Direction is sound.
- **Phase structure (1A → 1B → 2 → 3 → 4)** — Progression is correct. Detail within phases governed by kernel outline.
- **Part 5: Working Protocol** — Session protocol and specification philosophy are working well.
- **Part 7: Risk Register** — Still accurate. "AIOS codebase too academic" risk is now resolved (we're not using it).
- **Appendix A: Protocol Landscape** — Reference material, still useful as-is.

---

## NOTE: Pillar-to-Implementation Mapping (Appendix B)

The current mapping references AIOS modules. Should be updated to reference KERNOS primitives:

| Pillar | Primary Implementation | Protocols | Phase |
|---|---|---|---|
| 1. Capability Abstraction | Capability Graph + MCP | MCP | 1A-1B |
| 2. Lifecycle & Resources | Task Engine + Reasoning Service | — | 1B |
| 3. Persistent Context | Event Stream + State Store + Memory Architecture (TBD) | MCP (for tool-based memory access) | 1B-2 |
| 4. Identity, Trust & Boundaries | Behavioral Contracts + Channel Trust | OAuth 2.1 | 1B-3 |
| 5. Inter-Agent Communication | Shared State Store (coordination) + structured deliberation | A2A (evaluate Phase 4) | 2-4 |
| 6. User-System Interface | Message Gateway → App (Phase 3) | AG-UI (evaluate Phase 3) | 1A-3 |
