"""`pumpgrok safety score <mint>`: one JSON safety report, read-only."""

from __future__ import annotations

import argparse
from typing import Any

from pumpgrok.cli.errors import UsageError
from pumpgrok.cli.wiring import _call_use, _run_wired
from pumpgrok.config.settings import Settings


def safety_verb(args: argparse.Namespace, settings: Settings) -> int:
    """Score ``args.mint`` through the rug seat's ``safety.score_report`` grant."""
    if args.cmd != "score":
        raise UsageError(f"unknown safety command {args.cmd!r}")
    size = int(args.size_lamports)
    if size < 0:
        raise UsageError("bad --size-lamports: want a non-negative integer")
    fields: dict[str, Any] = {"mint": args.mint, "intended_size_lamports": size}
    creator = getattr(args, "creator", None)
    if isinstance(creator, str) and creator:
        fields["creator"] = creator
    return _run_wired(
        "safety score",
        "rug",
        settings,
        ("safety.score_report",),
        _call_use("safety score", 0, fields),
    )
