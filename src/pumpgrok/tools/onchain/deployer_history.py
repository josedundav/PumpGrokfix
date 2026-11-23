"""onchain.deployer_history: creator wallet history from RPC (TOOL-002).

WHALE-only grant (R35); ``safety.score_report`` composes it in-process for
RUG. ``tokens_seen`` counts pump.fun create instructions (``Create`` and
``CreateV2``) inside the lookback window, not the creator's signatures.
A busy wallet can sign a hundred times in a few seconds without launching
anything. ``rugs`` and ``worst_pnl_pct`` stay unwired until the reputation
book lands (Phase 4+). A signature whose transaction cannot be classified
fails closed as :class:`DataStale` (the check stays BLIND).
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import Field

from pumpgrok.core.errors import DataStale
from pumpgrok.core.ids import ambient_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.core.tool import Tool, ToolInput, ToolOutput, ToolSpec, tool
from pumpgrok.tools._common import J5, SOLANA_RPC_SOURCE, MintStr, meta_now, utcnow
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

_SIG_LIMIT = 100
_DAY_S = 86_400


class DeployerHistoryIn(ToolInput):
    """History for ``creator`` over the trailing ``lookback_days``."""

    creator: MintStr
    lookback_days: int = Field(default=30, ge=1, le=90)


class DeployerHistoryOut(ToolOutput):
    """Create-instruction history; adjudicated rug fields stay unwired."""

    tokens_seen: int = Field(ge=0)
    tokens_bought: int = Field(ge=0)
    rugs: int = Field(ge=0)
    worst_pnl_pct: Decimal | None = None
    is_known_bad: bool = False
    first_seen_at: datetime | None = None
    recent_signatures: list[str] = Field(default_factory=list)


DEPLOYER_HISTORY_SPEC = ToolSpec(
    name="onchain.deployer_history",
    description=(
        "Creator launches from pump create instructions in the signature "
        "window; adjudicated rug fields are unwired pending the reputation book."
    ),
    input_model=DeployerHistoryIn,
    output_model=DeployerHistoryOut,
    side_effect=SideEffect.READ,
    timeout_s=20.0,
    retry=J5,
    rate_limit_key=SOLANA_RPC_SOURCE,
    backend=(
        "src/pumpgrok/tools/onchain/deployer_history.py via "
        "getSignaturesForAddress and getTransaction"
    ),
)


#: pump.fun program. Create logs count only while this program is the
#: innermost invoke frame, so an ATA ``Create`` or a router wrapper does not.
_PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_CREATE_INSTRUCTIONS = frozenset({"Create", "create", "CreateV2", "create_v2"})
_TX_CONFIG = {"encoding": "json", "maxSupportedTransactionVersion": 1}


def _log_frame(line: str) -> tuple[str | None, str]:
    """Classify one log line as invoke, success, failed, instruction, or other."""
    ident: str | None = None
    kind = "other"
    if line.startswith("Program log: Instruction: "):
        ident = line.removeprefix("Program log: Instruction: ").strip()
        kind = "instruction"
    elif line.startswith("Program "):
        name, sep, tail = line[len("Program ") :].partition(" ")
        if sep and tail.startswith("invoke ["):
            ident, kind = name, "invoke"
        elif sep and tail == "success":
            ident, kind = name, "success"
        elif sep and tail.startswith("failed"):
            ident, kind = name, "failed"
    return ident, kind


def count_pump_creates(logs: list[object]) -> int:
    """Count pump ``Create`` / ``CreateV2`` instructions in one transaction log.

    The name is matched only while the pump program is the current invoke
    frame. ``Program log: Create`` from the associated-token program and
    router instructions such as ``CreateCoinAndBuyBondingCurveV3`` do not
    count. A non-text log line is unreadable and raises :class:`DataStale`.
    """
    stack: list[str] = []
    found = 0
    for line in logs:
        if not isinstance(line, str):
            raise DataStale("onchain.deployer_history log line is not text")
        ident, kind = _log_frame(line)
        if kind == "invoke" and ident is not None:
            stack.append(ident)
        elif kind in ("success", "failed") and ident is not None:
            if stack and stack[-1] == ident:
                stack.pop()
        elif (
            kind == "instruction"
            and ident in _CREATE_INSTRUCTIONS
            and stack
            and stack[-1] == _PUMP_PROGRAM
        ):
            found += 1
    return found


async def _creates_in_transaction(rpc: RpcClient, signature: str, creator: str) -> int:
    """Return the create count for one signature, or fail closed when unreadable."""
    try:
        tx = await rpc.call("getTransaction", [signature, _TX_CONFIG])
    except RpcError as exc:
        raise DataStale(
            f"onchain.deployer_history transaction unreadable creator={creator}"
        ) from exc
    if not isinstance(tx, dict):
        raise DataStale(f"onchain.deployer_history transaction unreadable creator={creator}")
    meta = tx.get("meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("logMessages"), list):
        raise DataStale(f"onchain.deployer_history transaction unreadable creator={creator}")
    if meta.get("err") is not None:
        return 0
    return count_pump_creates(meta["logMessages"])


async def run_deployer_history(inp: DeployerHistoryIn, rpc: RpcClient) -> dict[str, Any]:
    """Scan the signature window and return a validated typed payload."""
    try:
        sigs = await rpc.call("getSignaturesForAddress", [inp.creator, {"limit": _SIG_LIMIT}])
    except RpcError as exc:
        raise DataStale(f"onchain.deployer_history upstream failed creator={inp.creator}") from exc
    if not isinstance(sigs, list):
        raise DataStale(f"onchain.deployer_history malformed creator={inp.creator}")
    cutoff = utcnow(rpc.clock).timestamp() - inp.lookback_days * _DAY_S
    kept: list[dict[str, Any]] = []
    for entry in sigs:
        if not isinstance(entry, dict) or not isinstance(entry.get("signature"), str):
            continue
        block_time = entry.get("blockTime")
        if isinstance(block_time, int) and block_time < cutoff:
            continue
        kept.append(entry)
    stamps = [e["blockTime"] for e in kept if isinstance(e.get("blockTime"), int)]
    tokens_seen = 0
    for entry in kept:
        tokens_seen += await _creates_in_transaction(rpc, entry["signature"], inp.creator)
    out = {
        "tokens_seen": tokens_seen,
        "tokens_bought": 0,
        "rugs": 0,
        "worst_pnl_pct": None,
        "is_known_bad": False,
        "first_seen_at": datetime.fromtimestamp(min(stamps), UTC) if stamps else None,
        "recent_signatures": [e["signature"] for e in kept[:10]],
        "meta": meta_now(
            SOLANA_RPC_SOURCE,
            inp.correlation_id or (str(a) if (a := ambient_correlation()) else None),
            rpc.clock,
        ),
    }
    return DeployerHistoryOut.model_validate(out).model_dump()


def build_deployer_history_tool(rpc: RpcClient) -> Tool:
    """Bind ``onchain.deployer_history`` to one injected RPC client."""

    @tool(DEPLOYER_HISTORY_SPEC)
    async def _deployer_history(payload: DeployerHistoryIn) -> dict[str, Any]:
        return await run_deployer_history(payload, rpc)

    return _deployer_history
