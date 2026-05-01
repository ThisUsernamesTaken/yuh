# AI Collaboration Workspace

Shared between **Claude** and **Codex**. The top-level
[../../AI_COLLAB_LOG.md](../../AI_COLLAB_LOG.md) remains the canonical mailbox
for cross-agent decisions and the 10-minute heartbeat. This folder holds the
durable artifacts that would otherwise bloat the mailbox.

## Layout

| Folder | Purpose | Owner |
|---|---|---|
| `claude/` | Claude's working notes, hypotheses, rough drafts | Claude (write); Codex (read) |
| `codex/` | Codex's working notes, hypotheses, rough drafts | Codex (write); Claude (read) |
| `decisions/` | Durable decision records — each one is the final word on a question (e.g., "why we picked quote-source for the live shadow"). Both agents read and append. | Both |
| `experiments/` | Experiment outputs that informed a decision — backtest tables, sweep dumps, attribution joins. Cite the file from the relevant decision record. | Both |

## Conventions

- Filenames: `YYYY-MM-DD_short-slug.md` (e.g. `2026-04-25_crossed-book-guard.md`).
- Each note ends with a one-line **Status** field: `draft`, `superseded by ...`, or `final`.
- If you supersede another agent's note, link to it; never delete.
- Code changes still go through the regular edit path. These notes are *about* the changes, not the changes themselves.
- Keep the top-level mailbox short — link to longer notes here instead of pasting tables inline.
