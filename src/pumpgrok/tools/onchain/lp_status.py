"""onchain.lp_status: curve completion, then the canonical PumpSwap pool.

A token still on the bonding curve is read from the curve account. A
completed curve, a missing curve, or a curve the decoder rejects as empty
is graduated: the canonical PumpSwap pool must be readable or the check is
:class:`DataStale`. LP-mint lock fields stay null.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from pumpgrok.core.errors import DataStale
from pumpgrok.core.ids import ambient_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.core.tool import Tool, ToolInput, ToolOutput, ToolSpec, tool
from pumpgrok.tools._common import J5, SOLANA_RPC_SOURCE, MintStr, meta_now
from pumpgrok.tools.onchain.curve_layout import (
    CurveDecodeError,
    CurveState,
    decode_curve_account_base64,
)
from pumpgrok.tools.onchain.pumpswap_pool import read_canonical_pool
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError


class LpStatusIn(ToolInput):
    """Liquidity stage for ``mint`` from its curve account."""

    mint: MintStr
    curve_account: MintStr


class LpStatusOut(ToolOutput):
    """Stage plus completion flag; LP-mint fields are unwired (null)."""

    stage: Literal["bonding_curve", "graduated", "unknown"]
    curve_complete: bool | None = None
    lp_mint: str | None = None
    lp_burned_pct: float | None = None
    lp_locked_pct: float | None = None
    locker: str | None = None
    lock_expires_at: datetime | None = None
    checked: list[str] = Field(default_factory=list)


LP_STATUS_SPEC = ToolSpec(
    name="onchain.lp_status",
    description=(
        "Liquidity stage from the bonding-curve account, or from the "
        "canonical PumpSwap pool once the curve is complete or empty."
    ),
    input_model=LpStatusIn,
    output_model=LpStatusOut,
    side_effect=SideEffect.READ,
    timeout_s=20.0,
    retry=J5,
    rate_limit_key=SOLANA_RPC_SOURCE,
    backend="src/pumpgrok/tools/onchain/curve_layout.py via solana_rpc getAccountInfo",
)


def _open_curve(result: object) -> CurveState | None:
    """A decoded curve that is still open. Empty, missing, or complete is not."""
    if not isinstance(result, dict) or result.get("value") is None:
        return None
    value = result["value"]
    if not isinstance(value, dict):
        return None
    try:
        state = decode_curve_account_base64(value.get("data"))
    except CurveDecodeError:
        return None
    if state.complete:
        return None
    return state


async def run_lp_status(inp: LpStatusIn, rpc: RpcClient) -> dict[str, Any]:
    """Fetch the curve, or the canonical pool once the token has graduated."""
    try:
        result = await rpc.call("getAccountInfo", [inp.curve_account, {"encoding": "base64"}])
    except RpcError as exc:
        raise DataStale(f"onchain.lp_status upstream failed mint={inp.mint}") from exc
    state = _open_curve(result)
    if state is not None:
        stage, complete, checked = "bonding_curve", False, ["curve_account"]
    else:
        await read_canonical_pool(inp.mint, rpc)
        stage, complete, checked = "graduated", True, ["pumpswap_pool"]
    out = {
        "stage": stage,
        "curve_complete": complete,
        "lp_mint": None,
        "lp_burned_pct": None,
        "lp_locked_pct": None,
        "locker": None,
        "lock_expires_at": None,
        "checked": checked,
        "meta": meta_now(
            SOLANA_RPC_SOURCE,
            inp.correlation_id or (str(a) if (a := ambient_correlation()) else None),
            rpc.clock,
        ),
    }
    return LpStatusOut.model_validate(out).model_dump()


def build_lp_status_tool(rpc: RpcClient) -> Tool:
    """Bind ``onchain.lp_status`` to one injected RPC client."""

    @tool(LP_STATUS_SPEC)
    async def _lp_status(payload: LpStatusIn) -> dict[str, Any]:
        return await run_lp_status(payload, rpc)

    return _lp_status
