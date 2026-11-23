"""SAFE-8 instant dev dump and two-hop funding chain.

Create counting stays on pump ``Create`` and ``CreateV2`` only. A creator
sell or transfer of its launch buy inside the instant window fails. A funder
up to two hops back that launched a token in the past day and sold it within
seconds fails as a launch chain.
"""

from __future__ import annotations

from typing import Any

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.bundle_window import collect_signatures
from pumpgrok.tools.onchain.deployer_history import count_pump_creates
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.onchain.tx_accounts import (
    first_signer,
    is_success,
    lamport_deltas,
    require_transaction,
    token_owner_deltas,
)
from pumpgrok.tools.safety.checks.constants import (
    DEV_SELL_WINDOW_SECONDS,
    DEV_SELL_WINDOW_SLOTS,
    FUNDER_LOOKBACK_SECONDS,
    FUNDER_MAX_HOPS,
    FUNDER_MIN_LAMPORTS,
    INSTANT_SELL_SECONDS,
    LAUNCH_CHAIN_LOOKBACK_SECONDS,
    MAX_FUNDER_SIGNATURES,
    MAX_FUNDERS_PER_HOP,
)

_LABEL = "onchain.launch_behavior"


def _block_time(row: dict[str, Any]) -> int | None:
    stamp = row.get("blockTime")
    return stamp if isinstance(stamp, int) else None


async def _origin(rpc: RpcClient, mint: str) -> dict[str, Any]:
    sigs = await collect_signatures(rpc, mint, _LABEL)
    oldest = sigs[-1]
    tx = await require_transaction(rpc, str(oldest["signature"]), _LABEL)
    return {
        "slot": int(oldest["slot"]),
        "time": _block_time(oldest),
        "signature": str(oldest["signature"]),
        "discovered": first_signer(tx),
    }


def _in_dump_window(row: dict[str, Any], origin: dict[str, Any]) -> bool:
    slot_ok = origin["slot"] <= int(row["slot"]) <= origin["slot"] + DEV_SELL_WINDOW_SLOTS
    start = origin["time"]
    stamp = _block_time(row)
    time_ok = (
        isinstance(start, int)
        and stamp is not None
        and start <= stamp <= start + DEV_SELL_WINDOW_SECONDS
    )
    return slot_ok or time_ok


async def _dev_sell(
    rpc: RpcClient, mint: str, creator: str, origin: dict[str, Any]
) -> dict[str, Any]:
    sigs = await collect_signatures(rpc, creator, _LABEL)
    pre_buy = await _creator_prebuy(rpc, mint, creator, origin)
    sold, delay_s, delay_slots = await _creator_dump(rpc, mint, creator, origin, sigs)
    return {
        "dev_sold": pre_buy and sold > 0,
        "dev_sell_units": sold if pre_buy else 0,
        "dev_sell_delay_s": delay_s if pre_buy and sold > 0 else None,
        "dev_sell_delay_slots": delay_slots if pre_buy and sold > 0 else None,
    }


async def _creator_prebuy(rpc: RpcClient, mint: str, creator: str, origin: dict[str, Any]) -> bool:
    tx = await require_transaction(rpc, str(origin["signature"]), _LABEL)
    if not is_success(tx):
        return False
    meta = tx["meta"]
    if not isinstance(meta, dict):
        raise DataStale(f"{_LABEL} transaction unreadable")
    return token_owner_deltas(meta, mint, _LABEL).get(creator, 0) > 0


async def _creator_dump(
    rpc: RpcClient,
    mint: str,
    creator: str,
    origin: dict[str, Any],
    sigs: list[dict[str, Any]],
) -> tuple[int, int | None, int | None]:
    sold = 0
    delay_s: int | None = None
    delay_slots: int | None = None
    for row in sigs:
        if str(row["signature"]) == origin["signature"] or not _in_dump_window(row, origin):
            continue
        tx = await require_transaction(rpc, str(row["signature"]), _LABEL)
        if not is_success(tx):
            continue
        meta = tx["meta"]
        if not isinstance(meta, dict):
            raise DataStale(f"{_LABEL} transaction unreadable")
        delta = token_owner_deltas(meta, mint, _LABEL).get(creator, 0)
        if delta >= 0:
            continue
        sold += -delta
        delay_s, delay_slots = _first_delay(origin, row, delay_s, delay_slots)
    return sold, delay_s, delay_slots


def _first_delay(
    origin: dict[str, Any],
    row: dict[str, Any],
    delay_s: int | None,
    delay_slots: int | None,
) -> tuple[int | None, int | None]:
    if delay_slots is None:
        delay_slots = int(row["slot"]) - int(origin["slot"])
    stamp = _block_time(row)
    if delay_s is None and isinstance(origin["time"], int) and stamp is not None:
        delay_s = stamp - int(origin["time"])
    return delay_s, delay_slots


def _largest_funder(deltas: dict[str, int], wallet: str) -> str | None:
    gained = deltas.get(wallet, 0)
    if gained < FUNDER_MIN_LAMPORTS:
        return None
    best: str | None = None
    best_spent = 0
    for owner, delta in deltas.items():
        spent = -delta
        if owner != wallet and spent >= FUNDER_MIN_LAMPORTS and spent > best_spent:
            best, best_spent = owner, spent
    return best


async def _direct_funders(rpc: RpcClient, wallet: str, create_time: int) -> list[str]:
    sigs = await collect_signatures(rpc, wallet, _LABEL)
    ranked: dict[str, int] = {}
    for row in sigs:
        stamp = _block_time(row)
        if stamp is None or not (create_time - FUNDER_LOOKBACK_SECONDS <= stamp < create_time):
            continue
        funder, gained = await _funder_of(rpc, wallet, str(row["signature"]))
        if funder is not None:
            ranked[funder] = ranked.get(funder, 0) + gained
    ordered = sorted(ranked, key=lambda owner: ranked[owner], reverse=True)
    return ordered[:MAX_FUNDERS_PER_HOP]


async def _funder_of(rpc: RpcClient, wallet: str, signature: str) -> tuple[str | None, int]:
    tx = await require_transaction(rpc, signature, _LABEL)
    if not is_success(tx):
        return None, 0
    deltas = lamport_deltas(tx, _LABEL)
    funder = _largest_funder(deltas, wallet)
    if funder is None:
        return None, 0
    return funder, deltas.get(wallet, 0)


async def _sold_quickly(
    rpc: RpcClient, funder: str, mint: str, launched_at: int, sigs: list[dict[str, Any]]
) -> bool:
    checked = 0
    for row in sigs:
        stamp = _block_time(row)
        if stamp is None or not (launched_at < stamp <= launched_at + INSTANT_SELL_SECONDS):
            continue
        checked += 1
        if checked > MAX_FUNDER_SIGNATURES:
            raise DataStale(f"{_LABEL} funder history truncated")
        tx = await require_transaction(rpc, str(row["signature"]), _LABEL)
        if not is_success(tx) or not isinstance(tx.get("meta"), dict):
            continue
        if token_owner_deltas(tx["meta"], mint, _LABEL).get(funder, 0) < 0:
            return True
    return False


def _created_mint(tx: dict[str, Any]) -> str | None:
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        return None
    for side in ("postTokenBalances", "preTokenBalances"):
        rows = meta.get(side)
        if not isinstance(rows, list):
            continue
        for entry in rows:
            if isinstance(entry, dict) and isinstance(entry.get("mint"), str):
                return str(entry["mint"])
    return None


async def _is_launch_chain(rpc: RpcClient, funder: str, create_time: int) -> bool:
    sigs = await collect_signatures(rpc, funder, _LABEL)
    checked = 0
    for row in sigs:
        stamp = _block_time(row)
        if stamp is None:
            continue
        if not (create_time - LAUNCH_CHAIN_LOOKBACK_SECONDS <= stamp < create_time):
            continue
        checked += 1
        if checked > MAX_FUNDER_SIGNATURES:
            raise DataStale(f"{_LABEL} funder history truncated")
        tx = await require_transaction(rpc, str(row["signature"]), _LABEL)
        if not is_success(tx) or not isinstance(tx.get("meta"), dict):
            continue
        logs = tx["meta"].get("logMessages")
        if not isinstance(logs, list) or count_pump_creates(logs) <= 0:
            continue
        launched = _created_mint(tx)
        if launched is not None and await _sold_quickly(rpc, funder, launched, stamp, sigs):
            return True
    return False


async def _any_launch_chain(rpc: RpcClient, creator: str, create_time: int) -> bool:
    frontier = [creator]
    seen = {creator}
    for _hop in range(FUNDER_MAX_HOPS):
        nxt: list[str] = []
        for wallet in frontier:
            for funder in await _direct_funders(rpc, wallet, create_time):
                if funder in seen:
                    continue
                seen.add(funder)
                if await _is_launch_chain(rpc, funder, create_time):
                    return True
                nxt.append(funder)
        frontier = nxt
        if not frontier:
            break
    return False


async def run_launch_behavior(mint: str, creator: str | None, rpc: RpcClient) -> dict[str, Any]:
    """Dev-sell and launch-chain facts for ``mint``.

    ``creator`` is the caller-supplied dev when present, otherwise the first
    signer of the mint's earliest transaction.
    """
    origin = await _origin(rpc, mint)
    who = creator or origin["discovered"]
    if not isinstance(who, str) or not who:
        return _empty(None)
    if not isinstance(origin["time"], int):
        raise DataStale(f"{_LABEL} create time unreadable")
    dump = await _dev_sell(rpc, mint, who, origin)
    chain = await _any_launch_chain(rpc, who, int(origin["time"]))
    return {"creator": who, "launch_chain": chain, **dump}


def _empty(creator: str | None) -> dict[str, Any]:
    return {
        "creator": creator,
        "dev_sold": False,
        "dev_sell_units": 0,
        "dev_sell_delay_s": None,
        "dev_sell_delay_slots": None,
        "launch_chain": False,
    }
