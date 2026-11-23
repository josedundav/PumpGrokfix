"""onchain.top_holders: concentration against total supply (TOOL-002).

Wraps the ported :func:`pumpgrok.tools.onchain.holders.holder_snapshot`
without editing it (characterization pins). Shares are against
``getTokenSupply``, not against the sample. Token accounts whose authority
is a pump.fun or PumpSwap account (the bonding-curve vault and the
pumpswap pool vault) are excluded by default; ``exclude`` still drops
extra addresses. ``owner`` stays null on the row: the authority is used
only to decide exclusion.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pumpgrok.core.errors import DataStale
from pumpgrok.core.ids import ambient_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.core.tool import Tool, ToolInput, ToolOutput, ToolSpec, tool
from pumpgrok.tools._common import J5, SOLANA_RPC_SOURCE, MintStr, meta_now
from pumpgrok.tools.onchain.holders import holder_snapshot
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

#: Authorities owned by these programs are curve or pool inventory, not holders.
_INVENTORY_PROGRAMS = frozenset(
    {
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    }
)


def _value_list(result: object, mint: str) -> list[object]:
    if not isinstance(result, dict) or not isinstance(result.get("value"), list):
        raise DataStale(f"onchain.top_holders holder accounts unreadable mint={mint}")
    return list(result["value"])


def _token_authority(account: object, mint: str) -> str:
    """Token-account authority. Unreadable accounts fail closed."""
    if not isinstance(account, dict):
        raise DataStale(f"onchain.top_holders holder account unreadable mint={mint}")
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or parsed.get("type") != "account":
        raise DataStale(f"onchain.top_holders holder account unreadable mint={mint}")
    info = parsed.get("info")
    owner = info.get("owner") if isinstance(info, dict) else None
    if not isinstance(owner, str) or not owner:
        raise DataStale(f"onchain.top_holders holder account unreadable mint={mint}")
    return owner


def _program_owner(account: object, mint: str) -> str:
    """Program that owns an authority account. A missing account is unreadable."""
    if not isinstance(account, dict):
        raise DataStale(f"onchain.top_holders authority account unreadable mint={mint}")
    owner = account.get("owner")
    if not isinstance(owner, str) or not owner:
        raise DataStale(f"onchain.top_holders authority account unreadable mint={mint}")
    return owner


async def _inventory_token_accounts(rpc: RpcClient, addresses: list[str], mint: str) -> set[str]:
    """Token accounts whose authority is a pump bonding curve or a PumpSwap pool."""
    if not addresses:
        return set()
    try:
        token_res = await rpc.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}])
    except RpcError as exc:
        raise DataStale(f"onchain.top_holders holder accounts unreadable mint={mint}") from exc
    token_values = _value_list(token_res, mint)
    if len(token_values) != len(addresses):
        raise DataStale(f"onchain.top_holders holder accounts unreadable mint={mint}")
    authority_by_token = {
        address: _token_authority(account, mint)
        for address, account in zip(addresses, token_values, strict=True)
    }
    authorities = list(dict.fromkeys(authority_by_token.values()))
    try:
        auth_res = await rpc.call("getMultipleAccounts", [authorities, {"encoding": "base64"}])
    except RpcError as exc:
        raise DataStale(f"onchain.top_holders authority accounts unreadable mint={mint}") from exc
    auth_values = _value_list(auth_res, mint)
    if len(auth_values) != len(authorities):
        raise DataStale(f"onchain.top_holders authority accounts unreadable mint={mint}")
    program_by_authority = {
        authority: _program_owner(account, mint)
        for authority, account in zip(authorities, auth_values, strict=True)
    }
    return {
        address
        for address, authority in authority_by_token.items()
        if program_by_authority[authority] in _INVENTORY_PROGRAMS
    }


class HolderRow(BaseModel):
    """One holder with a 0..1 share of total supply."""

    model_config = ConfigDict(frozen=True)

    address: str
    owner: str | None = None
    amount_units: int = Field(ge=0)
    share: Decimal = Field(ge=0, le=1)


class TopHoldersIn(ToolInput):
    """Concentration for ``mint`` excluding ``exclude`` (curve/LP accounts)."""

    mint: MintStr
    limit: int = Field(default=20, ge=1, le=20)
    exclude: list[str] = Field(default_factory=list)


class TopHoldersOut(ToolOutput):
    """Supply-based concentration with the excluded accounts echoed."""

    holders: list[HolderRow]
    top1_share: Decimal = Field(ge=0, le=1)
    top5_share: Decimal = Field(ge=0, le=1)
    top10_share: Decimal = Field(ge=0, le=1)
    supply_units: int = Field(ge=0)
    excluded: list[str] = Field(default_factory=list)


TOP_HOLDERS_SPEC = ToolSpec(
    name="onchain.top_holders",
    description=(
        "Largest token accounts with shares against total supply. "
        "Bonding-curve and PumpSwap pool vaults are excluded, as are "
        "addresses passed in exclude. Owners stay unresolved on the row."
    ),
    input_model=TopHoldersIn,
    output_model=TopHoldersOut,
    side_effect=SideEffect.READ,
    timeout_s=15.0,
    retry=J5,
    rate_limit_key=SOLANA_RPC_SOURCE,
    backend="src/pumpgrok/tools/onchain/holders.py holder_snapshot + getTokenSupply",
)


async def _supply_units(rpc: RpcClient, mint: str) -> int:
    """Total supply in base units. A missing or zero supply is unreadable."""
    try:
        supply_res = await rpc.call("getTokenSupply", [mint])
    except RpcError as exc:
        raise DataStale(f"onchain.top_holders supply failed mint={mint}") from exc
    try:
        supply = int(supply_res["value"]["amount"])
    except (KeyError, ValueError, TypeError) as exc:
        raise DataStale(f"onchain.top_holders supply malformed mint={mint}") from exc
    if supply <= 0:
        raise DataStale(f"onchain.top_holders zero supply mint={mint}")
    return supply


def _snapshot_addresses(snap: dict[str, Any], mint: str) -> list[str]:
    """Largest-account addresses. A malformed sample is unreadable."""
    entries = snap.get("holders")
    if not isinstance(entries, list):
        raise DataStale(f"onchain.top_holders snapshot malformed mint={mint}")
    addresses: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("address"), str):
            raise DataStale(f"onchain.top_holders snapshot malformed mint={mint}")
        addresses.append(entry["address"])
    return addresses


async def run_top_holders(inp: TopHoldersIn, rpc: RpcClient) -> dict[str, Any]:
    """Wrap the ported snapshot and return a validated typed payload."""
    snap = await holder_snapshot(inp.mint, rpc, limit=inp.limit)
    if not snap.get("ok"):
        raise DataStale(f"onchain.top_holders snapshot failed mint={inp.mint}")
    addresses = _snapshot_addresses(snap, inp.mint)
    inventory = await _inventory_token_accounts(rpc, addresses, inp.mint)
    supply = await _supply_units(rpc, inp.mint)
    excluded = set(inp.exclude) | inventory
    rows: list[HolderRow] = []
    try:
        for entry in snap["holders"]:
            address = str(entry["address"])
            if address in excluded:
                continue
            amount = int(Decimal(str(entry["uiAmount"])) * (10 ** int(entry["decimals"])))
            rows.append(
                HolderRow(
                    address=address,
                    owner=None,
                    amount_units=amount,
                    share=Decimal(amount) / Decimal(supply),
                )
            )
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        raise DataStale(f"onchain.top_holders snapshot malformed mint={inp.mint}") from exc
    rows.sort(key=lambda r: r.amount_units, reverse=True)

    def _share(n: int) -> Decimal:
        return min(Decimal(1), sum((r.share for r in rows[:n]), Decimal(0)))

    out = {
        "holders": [r.model_dump() for r in rows],
        "top1_share": _share(1),
        "top5_share": _share(5),
        "top10_share": _share(10),
        "supply_units": supply,
        "excluded": sorted(excluded),
        "meta": meta_now(
            SOLANA_RPC_SOURCE,
            inp.correlation_id or (str(a) if (a := ambient_correlation()) else None),
            rpc.clock,
        ),
    }
    return TopHoldersOut.model_validate(out).model_dump()


def build_top_holders_tool(rpc: RpcClient) -> Tool:
    """Bind ``onchain.top_holders`` to one injected RPC client."""

    @tool(TOP_HOLDERS_SPEC)
    async def _top_holders(payload: TopHoldersIn) -> dict[str, Any]:
        return await run_top_holders(payload, rpc)

    return _top_holders
