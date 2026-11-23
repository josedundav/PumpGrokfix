"""SAFE-7 create-slot bundle window.

Buys are the net tokens that non-pool wallets gained in the mint's earliest
signature slot and the next 10 slots. Pool and curve authorities are
excluded. Transactions are read with ``getTransaction``; the RPC client
sends ``maxSupportedTransactionVersion`` 1.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.pda import canonical_pool_address
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError
from pumpgrok.tools.onchain.tx_accounts import (
    is_success,
    require_transaction,
    signature_page,
    signer_count,
    token_owner_deltas,
    token_supply_units,
)
from pumpgrok.tools.safety.checks.constants import (
    BUNDLE_WINDOW_FAIL_SHARE,
    BUNDLE_WINDOW_FOLLOWING_SLOTS,
    MAX_BALANCE_WALLETS,
    MAX_SIGNATURE_PAGES,
    MAX_WINDOW_TRANSACTIONS,
    SIGNATURE_PAGE_LIMIT,
)

_LABEL = "onchain.bundle_window"


async def collect_signatures(rpc: RpcClient, address: str, label: str) -> list[dict[str, Any]]:
    """Newest-first signature rows through the oldest short page.

    Stopping before the oldest page is incomplete history and raises
    :class:`DataStale`.
    """
    collected: list[dict[str, Any]] = []
    before: str | None = None
    for _page in range(MAX_SIGNATURE_PAGES):
        page = await signature_page(rpc, address, before, SIGNATURE_PAGE_LIMIT, label)
        if not page:
            break
        collected.extend(page)
        if len(page) < SIGNATURE_PAGE_LIMIT:
            return collected
        before = str(page[-1]["signature"])
    if not collected:
        raise DataStale(f"{label} has no signatures")
    raise DataStale(f"{label} signature history truncated")


def _window(sigs: list[dict[str, Any]], create_slot: int) -> list[dict[str, Any]]:
    last = create_slot + BUNDLE_WINDOW_FOLLOWING_SLOTS
    return [row for row in sigs if create_slot <= int(row["slot"]) <= last]


def _buy_from_tx(tx: dict[str, Any], mint: str, excluded: set[str]) -> tuple[int, set[str], bool]:
    """Net tokens bought by non-pool wallets, the buyers, and the multi-signer flag."""
    multi = signer_count(tx) > 1
    if not is_success(tx):
        return 0, set(), multi
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        raise DataStale(f"{_LABEL} transaction unreadable")
    deltas = token_owner_deltas(meta, mint, _LABEL)
    non_pool = {owner: delta for owner, delta in deltas.items() if owner not in excluded}
    net = sum(non_pool.values())
    if net <= 0:
        return 0, set(), multi
    buyers = {owner for owner, delta in non_pool.items() if delta > 0}
    return net, buyers, multi


async def _still_held(rpc: RpcClient, mint: str, wallets: set[str]) -> int:
    if not wallets:
        return 0
    if len(wallets) > MAX_BALANCE_WALLETS:
        raise DataStale(f"{_LABEL} too many buyer wallets mint={mint}")
    total = 0
    for wallet in sorted(wallets):
        total += await _wallet_balance(rpc, mint, wallet)
    return total


async def _wallet_balance(rpc: RpcClient, mint: str, wallet: str) -> int:
    try:
        result = await rpc.call(
            "getTokenAccountsByOwner",
            [wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
    except RpcError as exc:
        raise DataStale(f"{_LABEL} holder balance unreadable mint={mint}") from exc
    if not isinstance(result, dict) or not isinstance(result.get("value"), list):
        raise DataStale(f"{_LABEL} holder balance unreadable mint={mint}")
    total = 0
    for row in result["value"]:
        total += _parsed_amount(row, mint)
    return total


def _parsed_amount(row: object, mint: str) -> int:
    account = row.get("account") if isinstance(row, dict) else None
    data = account.get("data") if isinstance(account, dict) else None
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    amount = info.get("tokenAmount") if isinstance(info, dict) else None
    if not isinstance(amount, dict):
        raise DataStale(f"{_LABEL} holder balance unreadable mint={mint}")
    try:
        return int(amount["amount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataStale(f"{_LABEL} holder balance unreadable mint={mint}") from exc


async def run_bundle_window(mint: str, curve_account: str, rpc: RpcClient) -> dict[str, Any]:
    """Sum non-pool buys in the create slot and the next 10 slots."""
    supply = await token_supply_units(rpc, mint, _LABEL)
    sigs = await collect_signatures(rpc, mint, _LABEL)
    create_slot = int(sigs[-1]["slot"])
    window = _window(sigs, create_slot)
    truncated = len(window) > MAX_WINDOW_TRANSACTIONS
    chosen = window[:MAX_WINDOW_TRANSACTIONS]
    excluded = {curve_account, canonical_pool_address(mint)}
    bought, wallets, multi = await _scan(rpc, mint, chosen, excluded)
    share = Decimal(bought) / Decimal(supply)
    if truncated and share < BUNDLE_WINDOW_FAIL_SHARE:
        raise DataStale(f"{_LABEL} create window truncated mint={mint}")
    held = await _still_held(rpc, mint, wallets)
    held_share = min(Decimal(1), Decimal(held) / Decimal(supply))
    return {
        "create_slot": create_slot,
        "wallet_count": len(wallets),
        "share": format(share, "f"),
        "still_held_share": format(held_share, "f"),
        "multi_signer_txs": multi,
        "supply_units": supply,
    }


async def _scan(
    rpc: RpcClient, mint: str, rows: list[dict[str, Any]], excluded: set[str]
) -> tuple[int, set[str], int]:
    bought = 0
    wallets: set[str] = set()
    multi = 0
    for row in rows:
        tx = await require_transaction(rpc, str(row["signature"]), _LABEL)
        units, buyers, flagged = _buy_from_tx(tx, mint, excluded)
        bought += units
        wallets.update(buyers)
        if flagged:
            multi += 1
    return bought, wallets, multi
