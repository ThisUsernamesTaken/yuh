# AGENTS.md — DEPRECATED

**This file is no longer the source of truth.**

The architecture this document described (TA_FORCED + microstructure
pressure + DOMINANT-DIRECTION gate + wallet-copy disabled) was retired in
the 2026-05-02 strategic reset. The engine now runs **`BB_PURE` only**
(pure Brownian-Bridge mispricing).

## Where to look instead

| You want to know | Read |
|---|---|
| What the engine does today | [`CLAUDE.md`](CLAUDE.md) |
| How to set up a new machine | [`docs/NEW_INSTANCE_SETUP.md`](docs/NEW_INSTANCE_SETUP.md) |
| One-page orientation | [`README.md`](README.md) |
| The trading thesis (math) | [`bb_pure.py`](bb_pure.py) (~190 lines) |
| Live-behavior knobs | [`user_config.py`](user_config.py) |

## Why this file still exists

Old AI sessions and external links may reference `AGENTS.md`. Deleting it
breaks those links silently. Leaving stale content was worse — fresh AI
agents read it, found it contradicted CLAUDE.md, and got confused. So
this file now serves only as a redirect.

If you arrived here from a search, follow the links above.
