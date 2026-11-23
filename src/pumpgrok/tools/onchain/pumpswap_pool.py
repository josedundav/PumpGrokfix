"""Canonical PumpSwap pool balances for a graduated pump.fun mint.

The pool address is the index-0 pool created by the pump pool-authority PDA,
quoted in wrapped SOL, and owned by ``pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA``.
Real SOL depth is the quote-vault token balance, not the bonding-curve reserve.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.pda import (
    PUMP_AMM_PROGRAM,
    WSOL_MINT,
    b58encode,
    canonical_pool_address,
)
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

_BASE_MINT_AT = 43
_QUOTE_MINT_AT = 75
_BASE_VAULT_AT = 139
_QUOTE_VAULT_AT = 171
_MIN_LEN = _QUOTE_VAULT_AT + 32


@dataclass(frozen=True, slots=True)
class PoolBalances:
    """Vault balances of the canonical pool. Quote lamports are real SOL."""

    pool: str
    base_vault: str
    quote_vault: str
    base_units: int
    quote_lamports: int


def _pubkey_at(data: bytes, offset: int) -> str:
    return b58encode(data[offset : offset + 32])


def vaults_from_pool_data(data: bytes, mint: str) -> tuple[str, str]:
    """Base and quote vault addresses when the pool is the canonical pair."""
    if len(data) < _MIN_LEN:
        raise DataStale(f"pumpswap pool account is short mint={mint}")
    base_mint = _pubkey_at(data, _BASE_MINT_AT)
    quote_mint = _pubkey_at(data, _QUOTE_MINT_AT)
    if base_mint != mint or quote_mint != WSOL_MINT:
        raise DataStale(f"pumpswap pool mints do not match mint={mint}")
    return _pubkey_at(data, _BASE_VAULT_AT), _pubkey_at(data, _QUOTE_VAULT_AT)


def _account_bytes(value: object, mint: str) -> bytes:
    data = value.get("data") if isinstance(value, dict) else None
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
        raise DataStale(f"pumpswap pool data unreadable mint={mint}")
    try:
        return base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError) as exc:
        raise DataStale(f"pumpswap pool data unreadable mint={mint}") from exc


async def _token_amount(rpc: RpcClient, vault: str, mint: str) -> int:
    try:
        result = await rpc.call("getTokenAccountBalance", [vault])
    except RpcError as exc:
        raise DataStale(f"pumpswap vault unreadable mint={mint}") from exc
    if not isinstance(result, dict) or result.get("value") is None:
        raise DataStale(f"pumpswap vault missing mint={mint}")
    try:
        return int(result["value"]["amount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataStale(f"pumpswap vault amount unreadable mint={mint}") from exc


async def read_canonical_pool(mint: str, rpc: RpcClient) -> PoolBalances:
    """Read the canonical pool's base and quote vault balances.

    A missing account, a wrong owner, or a null vault balance is
    :class:`DataStale`.
    """
    pool = canonical_pool_address(mint)
    try:
        result = await rpc.call("getAccountInfo", [pool, {"encoding": "base64"}])
    except RpcError as exc:
        raise DataStale(f"pumpswap pool unreadable mint={mint}") from exc
    if not isinstance(result, dict) or result.get("value") is None:
        raise DataStale(f"pumpswap pool missing mint={mint}")
    value = result["value"]
    if not isinstance(value, dict) or value.get("owner") != PUMP_AMM_PROGRAM:
        raise DataStale(f"pumpswap pool owner mismatch mint={mint}")
    base_vault, quote_vault = vaults_from_pool_data(_account_bytes(value, mint), mint)
    return PoolBalances(
        pool=pool,
        base_vault=base_vault,
        quote_vault=quote_vault,
        base_units=await _token_amount(rpc, base_vault, mint),
        quote_lamports=await _token_amount(rpc, quote_vault, mint),
    )
