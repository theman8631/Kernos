# specs/ directory convention

Kernos active specs live in **Notion** (`Inbox: Architect → CC`), not in this
repository. This directory is an **archive** — once a spec has shipped, it
lands under `specs/completed/` for reference.

Nothing belongs at the top level of `specs/`. If you find a file sitting
directly in this directory, it is almost certainly an orphan — either a
stale reference copy from a bundle drop or a file that should have been
routed to `docs/reference/` or `specs/completed/`. Either delete it or move
it to the correct canonical home.

The one exception is this README, which documents the convention so future
contributors don't get confused by untracked orphans reappearing in
`git status`.
