# The Skill Model

> How Kernos organizes every capability the agent can reach.

## The frame

Every agent harness has a tool surface — the set of things the agent can actually do. Most harnesses treat this surface as either a fixed static list (everything the agent can do is declared in config at startup) or an unconstrained dynamic catalog (the agent browses a directory of capabilities on every turn). Neither shape scales well.

A fixed list breaks as soon as capabilities need to be added without a restart — new MCP servers, workspace-built tools, member-specific integrations. An unconstrained catalog burns tokens and context room every turn with tools the agent doesn't need, and forces the agent itself to do librarian work on every interaction.

Kernos handles capabilities through a single lens with four axes: **substrate**, **surface**, **lifecycle**, and **scope**. Understanding these as four independent dimensions of any capability is what makes the whole tool catalog tractable.

## Substrate — where a capability physically lives

The three substrates Kernos supports:

- **Kernel tools.** Native Python functions built into the Kernos codebase. Memory reads, the covenant editor, plan management, relational messaging. Always available, version-controlled, part of the system.
- **MCP tools.** Capabilities provided by external MCP servers. A Google Calendar integration runs via `npx @modelcontextprotocol/server-calendar`. A GitHub integration runs via a Python MCP server. These are external processes the kernel connects to and exposes as tools.
- **Workspace-built tools.** Capabilities the agent wrote itself. The agent writes Python in a sandboxed subprocess, exercises it, and registers it as a first-class tool. From the surface side, a workspace-built tool looks the same as a kernel tool.

A single capability the agent invokes — *"send this invoice to the client"*, *"add this to my daughter's calendar"* — may resolve through any of the three substrates. The agent doesn't pick the substrate; it picks the capability. Substrate is a property of implementation, not a dimension of use.

## Surface — how a capability appears to the agent

The universal tool catalog is one registry that merges all three substrates. The agent sees a single unified list of capabilities regardless of where they physically live. This unification is what makes substrate invisible: an MCP-backed calendar tool and a kernel-backed memory tool sit next to each other in the catalog with identical invocation shape.

On top of the catalog, Kernos uses **three-tier surfacing** to decide which capabilities the agent actually sees on any given turn:

**Tier 1 — common check.** A small set of always-surfaced capabilities appears on every turn with no LLM call. These are the capabilities every turn might plausibly need: messaging, memory access, plan management, the workspace itself. Low cost, zero branching.

**Tier 2 — LLM catalog scan.** When the Tier 1 set doesn't contain what the turn needs, a lightweight LLM scan reads the turn's shape and pulls the relevant subset from the full catalog. This happens only when Tier 1 didn't cover the need. It's a cheap call that hands back "these five tools are probably relevant to what's happening."

**Tier 3 — promotion.** When a capability gets used and succeeds, it gets *promoted* in the surface — for this context space, for this member, it gets surfaced more aggressively on subsequent turns. Frequently-used capabilities float to the top of the agent's awareness; rarely-used ones stay in the catalog but don't consume context budget unless needed.

The agent never browses its toolbox. It gets a budgeted window of the tools that matter right now, based on what the turn looks like and what has worked recently in this context.

## Lifecycle — how a capability comes into existence and evolves

Capabilities follow different lifecycles depending on substrate:

**Kernel tools** are created through the normal code-ship cycle — spec'd, implemented, reviewed, tested, deployed. They appear the moment the kernel restarts after a deploy.

**MCP tools** are configured at the instance level — a household adds a Google Calendar integration once; a small-business team adds a Slack integration once. Once configured, the MCP server runs as a subprocess and the tools it exposes become available through the unified catalog.

**Workspace-built tools** have the richest lifecycle. The agent identifies a recurring friction, designs a small Python utility, writes it, exercises it live, registers it in the catalog. From that point on, the utility is a first-class tool that can be promoted through Tier 3 just like any kernel tool. Workspace-built tools can also be deprecated — either by the member explicitly or by a later built-better replacement.

The important property: **capability creation never requires a restart.** An agent can build a new workspace tool during a conversation and use it three turns later. An MCP server can be added without downtime. The catalog is live state, not compiled state.

## Scope — who can see a capability

Some capabilities are universal across an instance; others are scoped.

- **Instance-scoped.** Available across all members, all context spaces. Kernel tools are typically instance-scoped. So are most MCP integrations.
- **Member-scoped.** Available to one specific member. A workspace tool one family member built for their fitness tracking is not surfaced to their spouse's agent.
- **Space-scoped.** Available only in specific context spaces. A project-specific tool that an engineer's work-space needs isn't useful in their personal-space; Tier 3 promotion naturally concentrates it where it matters, but explicit scoping can enforce it.

Scope interacts with the other three axes. A member-scoped, workspace-built, kernel-substrate capability gets promoted aggressively in its owning member's active space and appears nowhere else.

## Two grounded examples

### A small-business team

A three-person marketing consultancy runs Kernos. They've added MCP servers for Google Calendar, Slack, and their CRM. One of the partners has a workspace-built tool they wrote last month that parses client brief emails into a structured intake template.

When the partner says *"draft a response to the new lead that came in this morning,"* the agent sees: the email integration (Tier 1, always surfaced), the brief-parsing tool (Tier 3 promoted — it gets used on every new-lead response), and the messaging tools for reply composition. It does *not* see: the calendar tool, the Slack integration, the CRM contact manager — not because they're unavailable, but because the turn's shape doesn't warrant surfacing them.

If the draft response turns out to require checking whether a follow-up call is already scheduled, the agent triggers a Tier 2 scan, the calendar tool surfaces, and from that point on in the current context space, calendar operations are slightly more present.

### A family household

A household of four runs Kernos. The parents' context spaces include a shared calendar MCP; each family member has their own member-scoped workspace. The oldest teenager has built a workspace tool that tracks homework deadlines across their classes.

When the teenager says *"when's my history essay due again?"*, the agent sees: their personal homework tracker (Tier 3, heavily promoted in their school-space), the calendar tool (Tier 1), and their memory. It does not see: their parents' shared-calendar covenants, the grocery list their parent maintains, or the teenager's sibling's book reading log. Scope handles the isolation; surfacing handles the relevance.

If the parent asks *"what's on the family calendar this weekend?"*, their agent sees the shared calendar prominently, the household's shared reminders, and the coordination tools — and does not see, for example, the teenager's homework tracker, which is out of scope.

## Why this frame matters

The four-axis lens makes capability management a composition problem rather than a configuration problem. Any new capability — a kernel tool, an MCP integration, a workspace-built helper — has an obvious home in terms of substrate, an obvious surface story (catalog entry, surfacing tier), an obvious lifecycle, and an obvious scope. The frame keeps the system's tool surface organized without central coordination.

The agent, in turn, doesn't have to understand the lens. It understands "the capabilities I have right now" — the budgeted window surface produces — and invokes them. The four axes are the system's way of talking about tools; the agent just uses them.

## Related

- **[Judgment-vs-Plumbing](judgment-vs-plumbing.md)** — the pattern the tool dispatch itself follows
- **[Action Loop](action-loop.md)** — what happens when a tool gets invoked
- **[Agentic workspace (capability)](../architecture/overview.md)** — how workspace-built tools actually get made
