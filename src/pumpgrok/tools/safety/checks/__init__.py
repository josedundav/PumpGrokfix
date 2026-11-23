"""Safety check result envelope and check-id vocabulary (TOOL-003).

Every check returns a reasoned row; a check without data returns
``status="blind"`` instead of raising. ``REQUIRED_IDS`` force a KILL
verdict when blind; ``ADVISORY_IDS`` (transfer tax and honeypot) have no
data source and never force the verdict on their own. The create-window
bundle check is required.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CheckId = Literal[
    "mint_authority",
    "freeze_authority",
    "lp_status",
    "transfer_tax",
    "honeypot",
    "top10_concentration",
    "bundler_cluster",
    "deployer_history",
    "token2022_extensions",
    "liquidity_depth",
]

CHECK_IDS: tuple[CheckId, ...] = (
    "mint_authority",
    "freeze_authority",
    "lp_status",
    "transfer_tax",
    "honeypot",
    "top10_concentration",
    "bundler_cluster",
    "deployer_history",
    "token2022_extensions",
    "liquidity_depth",
)

REQUIRED_IDS: frozenset[str] = frozenset(
    {
        "mint_authority",
        "freeze_authority",
        "lp_status",
        "top10_concentration",
        "deployer_history",
        "token2022_extensions",
        "liquidity_depth",
        "bundler_cluster",
    }
)


ADVISORY_IDS: frozenset[str] = frozenset({"transfer_tax", "honeypot"})

# Amendment 2026-10-25: SAFE-4 (top10_concentration) and SAFE-8
# (deployer_history) report BLIND as advisory while no paid RPC provider is
# recorded; every other required blind still forces KILL.
HOLDER_ADVISORY_PAIR: frozenset[str] = frozenset({"top10_concentration", "deployer_history"})


class CheckResult(BaseModel):
    """One scored check with a human-readable reason (never a bare boolean)."""

    model_config = ConfigDict(frozen=True)

    id: CheckId
    status: Literal["pass", "warn", "fail", "blind"]
    value: str | float | bool | None = None
    threshold: str | float | None = None
    weight: Decimal = Field(ge=0, le=1)
    reason: str = Field(min_length=1)
