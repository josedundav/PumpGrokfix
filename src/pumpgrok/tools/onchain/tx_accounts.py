"""Parsed-transaction helpers for the safety readers.

A transport error or a null body raises :class:`DataStale`. Versioned reads
go through :func:`transaction_read_config`, and the RPC client forces
``maxSupportedTransactionVersion`` 1.
"""

from __future__ import annotations

from typing import Any

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError, transaction_read_config


def account_keys(tx: dict[str, Any]) -> list[str]:
    """Static keys plus address-lookup loads. ``jsonParsed`` keys use ``pubkey``."""
    message = tx.get("transaction")
    message = message.get("message") if isinstance(message, dict) else None
    raw = message.get("accountKeys") if isinstance(message, dict) else None
    keys = [_key_text(key) for key in raw] if isinstance(raw, list) else []
    keys = [key for key in keys if key]
    meta = tx.get("meta")
    loaded = meta.get("loadedAddresses") if isinstance(meta, dict) else None
    if isinstance(loaded, dict):
        for bucket in ("writable", "readonly"):
            extra = loaded.get(bucket)
            if isinstance(extra, list):
                keys.extend(key for key in extra if isinstance(key, str) and key)
    return keys


def _key_text(key: object) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, dict) and isinstance(key.get("pubkey"), str):
        return str(key["pubkey"])
    return ""


def signer_count(tx: dict[str, Any]) -> int:
    """Required signatures, else the number of parsed keys marked as signers."""
    message = tx.get("transaction")
    message = message.get("message") if isinstance(message, dict) else None
    if not isinstance(message, dict):
        return 0
    header = message.get("header")
    if isinstance(header, dict) and isinstance(header.get("numRequiredSignatures"), int):
        return int(header["numRequiredSignatures"])
    raw = message.get("accountKeys")
    if not isinstance(raw, list):
        return 0
    return sum(1 for key in raw if isinstance(key, dict) and key.get("signer") is True)


def first_signer(tx: dict[str, Any]) -> str | None:
    """Fee payer, the first required signer, when the message names one."""
    keys = account_keys(tx)
    if signer_count(tx) > 0 and keys:
        return keys[0]
    return None


def is_success(tx: dict[str, Any]) -> bool:
    meta = tx.get("meta")
    return isinstance(meta, dict) and meta.get("err") is None


def token_owner_deltas(meta: dict[str, Any], mint: str, label: str) -> dict[str, int]:
    """Net token units per owner for ``mint``. A malformed mint row is unreadable."""
    pre = _side_amounts(meta.get("preTokenBalances"), mint, label)
    post = _side_amounts(meta.get("postTokenBalances"), mint, label)
    owners: dict[str, int] = {}
    for key in set(pre) | set(post):
        owner = key[1]
        owners[owner] = owners.get(owner, 0) + post.get(key, 0) - pre.get(key, 0)
    return owners


def _side_amounts(raw: object, mint: str, label: str) -> dict[tuple[int, str], int]:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise DataStale(f"{label} token balances unreadable")
    amounts: dict[tuple[int, str], int] = {}
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("mint") != mint:
            continue
        owner = entry.get("owner")
        if not isinstance(owner, str) or not owner:
            raise DataStale(f"{label} token owner unreadable")
        try:
            index = int(entry["accountIndex"])
            amount = int(entry["uiTokenAmount"]["amount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataStale(f"{label} token amount unreadable") from exc
        amounts[(index, owner)] = amount
    return amounts


def lamport_deltas(tx: dict[str, Any], label: str) -> dict[str, int]:
    """Net lamports per account key. Missing balance arrays mean no movement."""
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        raise DataStale(f"{label} transaction unreadable")
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    if pre is None and post is None:
        return {}
    if not isinstance(pre, list) or not isinstance(post, list):
        raise DataStale(f"{label} lamport balances unreadable")
    keys = account_keys(tx)
    deltas: dict[str, int] = {}
    for index, key in enumerate(keys):
        if index >= len(pre) or index >= len(post):
            break
        try:
            deltas[key] = int(post[index]) - int(pre[index])
        except (TypeError, ValueError) as exc:
            raise DataStale(f"{label} lamport balances unreadable") from exc
    return deltas


async def token_supply_units(rpc: RpcClient, mint: str, label: str) -> int:
    """Total supply in base units. Zero or a null body is unreadable."""
    try:
        result = await rpc.call("getTokenSupply", [mint])
    except RpcError as exc:
        raise DataStale(f"{label} supply unreadable mint={mint}") from exc
    if not isinstance(result, dict):
        raise DataStale(f"{label} supply unreadable mint={mint}")
    try:
        supply = int(result["value"]["amount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataStale(f"{label} supply unreadable mint={mint}") from exc
    if supply <= 0:
        raise DataStale(f"{label} supply unreadable mint={mint}")
    return supply


async def require_transaction(rpc: RpcClient, signature: str, label: str) -> dict[str, Any]:
    """Fetch one transaction. An error or a null body is unreadable."""
    try:
        tx = await rpc.call("getTransaction", [signature, transaction_read_config("jsonParsed")])
    except RpcError as exc:
        raise DataStale(f"{label} transaction unreadable") from exc
    if not isinstance(tx, dict) or not isinstance(tx.get("meta"), dict):
        raise DataStale(f"{label} transaction unreadable")
    return tx


async def signature_page(
    rpc: RpcClient, address: str, before: str | None, limit: int, label: str
) -> list[dict[str, Any]]:
    """One ``getSignaturesForAddress`` page. A null body is unreadable."""
    config: dict[str, Any] = {"limit": limit}
    if before is not None:
        config["before"] = before
    try:
        page = await rpc.call("getSignaturesForAddress", [address, config])
    except RpcError as exc:
        raise DataStale(f"{label} signatures unreadable") from exc
    if not isinstance(page, list):
        raise DataStale(f"{label} signatures unreadable")
    rows: list[dict[str, Any]] = []
    for entry in page:
        if not isinstance(entry, dict) or not isinstance(entry.get("signature"), str):
            raise DataStale(f"{label} signatures unreadable")
        if not isinstance(entry.get("slot"), int):
            raise DataStale(f"{label} signature slot unreadable")
        rows.append(entry)
    return rows
