"""Verify all dispatch kill-switch flags resolve to False.

Imports user_config and asserts every flag from the catastrophe writeup
is False. Prints PASS/FAIL summary. Run after any user_config edit
related to the 2026-05-04 catastrophe kill switch.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from any cwd: ensure repo root is on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import user_config  # noqa: E402

FLAGS = [
    "MRC_ENABLED",
    "MRC_FORCE_EXIT",
    "MRC_TP_MODULATION",
    "MRC_RECLAIM_ATTACH_ENABLED",
    "MRC_PATH_SIG_PROTECTIVE_ESC",
    "BB_PURE_TAPE_EXIT_SHADOW_ENABLED",
    "BB_PURE_TAPE_EXIT_GATE_ENABLED",
    "TP_TAKER_CONVERT_ENABLED",
    "PRE_EXPIRY_TAKER_ENABLED",
    "SR_TP_CAP_ENABLED",
    "WALL_CONSUMPTION_EXIT_ENABLED",
    "KALSHI_LAG_TP_ENABLED",
    "FLAT_CONFIRM_RECHECK_ENABLED",
    "BB_PURE_VELOCITY_VETO_ENABLED",
]


def main() -> int:
    failures = []
    for name in FLAGS:
        val = getattr(user_config, name, "<not set>")
        ok = val is False
        marker = "OK " if ok else "FAIL"
        print(f"{marker} {name} = {val!r}")
        if not ok:
            failures.append(name)
    print()
    if failures:
        print(f"FAIL: {len(failures)} flag(s) not False: {failures}")
        return 1
    print(f"PASS: all {len(FLAGS)} dispatch flags are False.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
