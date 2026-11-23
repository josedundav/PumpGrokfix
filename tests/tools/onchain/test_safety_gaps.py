"""Deterministic fixtures for the safety-scorer gaps. No network.

A 42% create-slot bundle, a dev sell 5 seconds after launch, a two-hop
launch chain, and a graduated mint whose pool vault holds 99.5% of supply.
"""

from __future__ import annotations

import base64
import json
import random
from decimal import Decimal
from typing import Any

import httpx
import pytest

from pumpgrok.core.errors import DataStale
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.onchain.bundle_window import run_bundle_window
from pumpgrok.tools.onchain.lp_status import LpStatusIn, run_lp_status
from pumpgrok.tools.onchain.pda import (
    PUMP_AMM_PROGRAM,
    PUMP_PROGRAM,
    WSOL_MINT,
    b58decode_pubkey,
    b58encode,
    bonding_curve_address,
    canonical_pool_address,
)
from pumpgrok.tools.onchain.pumpswap_pool import read_canonical_pool
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.checks.constants import BUNDLE_WINDOW_FAIL_SHARE
from pumpgrok.tools.safety.checks.readers import check_bundler_cluster
from pumpgrok.tools.safety.score_report import ScoreReportIn, run_score_report

MINT = "7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"
POOL = canonical_pool_address(MINT)
CURVE = bonding_curve_address(MINT)
CREATOR = b58encode(bytes([3]) * 32)
W1 = b58encode(bytes([4]) * 32)
W2 = b58encode(bytes([5]) * 32)
W3 = b58encode(bytes([6]) * 32)
W4 = b58encode(bytes([7]) * 32)
FUNDER = b58encode(bytes([8]) * 32)
HOP = b58encode(bytes([9]) * 32)
OTHER = b58encode(bytes([10]) * 32)
SYSTEM = "11111111111111111111111111111111"
SUPPLY = 100_000_000
BUY = 14_000_000
START = 1_700_000_000

_CREATE_LOGS = [
    f"Program {PUMP_PROGRAM} invoke [1]",
    "Program log: Instruction: CreateV2",
    f"Program {PUMP_PROGRAM} success",
]
_SELL_LOGS = [
    f"Program {PUMP_PROGRAM} invoke [1]",
    "Program log: Instruction: Sell",
    f"Program {PUMP_PROGRAM} success",
]


def _rpc(handler: Any) -> RpcClient:
    return RpcClient(
        ["https://rpc.invalid"],
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        FixtureClock(start=START),
        rng=random.Random(4),
        max_attempts_per_endpoint=1,
    )


def _response(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _pool_bytes(base_vault: str, quote_vault: str) -> bytes:
    raw = bytearray(243)
    raw[43:75] = b58decode_pubkey(MINT)
    raw[75:107] = b58decode_pubkey(WSOL_MINT)
    raw[139:171] = b58decode_pubkey(base_vault)
    raw[171:203] = b58decode_pubkey(quote_vault)
    return bytes(raw)


def _account(data: bytes, owner: str) -> dict[str, Any]:
    return {
        "value": {
            "data": [base64.b64encode(data).decode("ascii"), "base64"],
            "executable": False,
            "lamports": 1,
            "owner": owner,
        }
    }


def _tx(
    keys: list[str],
    signers: int,
    balances: list[tuple[str, int, int]],
    *,
    logs: list[str] | None = None,
    lamports: list[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    pre = [
        {"accountIndex": i, "mint": MINT, "owner": owner, "uiTokenAmount": {"amount": str(before)}}
        for i, (owner, before, _after) in enumerate(balances)
    ]
    post = [
        {"accountIndex": i, "mint": MINT, "owner": owner, "uiTokenAmount": {"amount": str(after)}}
        for i, (owner, _before, after) in enumerate(balances)
    ]
    meta: dict[str, Any] = {
        "err": None,
        "logMessages": logs or [],
        "preTokenBalances": pre,
        "postTokenBalances": post,
    }
    if lamports is not None:
        meta["preBalances"] = [before for before, _after in lamports]
        meta["postBalances"] = [after for _before, after in lamports]
    return {
        "meta": meta,
        "transaction": {
            "message": {
                "header": {"numRequiredSignatures": signers},
                "accountKeys": keys,
            }
        },
    }


def _sig(signature: str, slot: int, when: int) -> dict[str, Any]:
    return {"signature": signature, "slot": slot, "blockTime": when}


def test_bundle_floor_stays_at_25_percent() -> None:
    assert Decimal("0.25") == BUNDLE_WINDOW_FAIL_SHARE
    fail = check_bundler_cluster(
        {"share": "0.25", "still_held_share": "0.25", "wallet_count": 2, "multi_signer_txs": 1}
    )
    ok = check_bundler_cluster(
        {"share": "0.249", "still_held_share": "0", "wallet_count": 1, "multi_signer_txs": 0}
    )
    assert fail.status == "fail"
    assert ok.status == "pass"


async def test_create_slot_bundle_of_42_percent_fails() -> None:
    late = _tx([W4, CURVE], 1, [(W4, 0, 20_000_000), (CURVE, SUPPLY, SUPPLY - 20_000_000)])
    multi = _tx(
        [W1, W2, CURVE],
        2,
        [(W1, 0, BUY), (W2, 0, BUY), (CURVE, SUPPLY, SUPPLY - 2 * BUY)],
    )
    single = _tx(
        [W3, CURVE],
        1,
        [(W3, 0, BUY), (CURVE, SUPPLY - 2 * BUY, SUPPLY - 3 * BUY)],
    )
    txs = {"late": late, "multi": multi, "single": single}
    held = {W1: BUY, W2: BUY, W3: BUY}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getTokenSupply":
            return _response({"value": {"amount": str(SUPPLY), "decimals": 6}})
        if method == "getSignaturesForAddress":
            return _response(
                [_sig("late", 90, START), _sig("multi", 50, START), _sig("single", 50, START)]
            )
        if method == "getTransaction":
            assert params[1]["maxSupportedTransactionVersion"] == 1
            seen.append(params[0])
            return _response(txs[params[0]])
        if method == "getTokenAccountsByOwner":
            amount = held[params[0]]
            return _response(
                {
                    "value": [
                        {
                            "account": {
                                "data": {
                                    "parsed": {"info": {"tokenAmount": {"amount": str(amount)}}}
                                }
                            }
                        }
                    ]
                }
            )
        raise AssertionError(method)

    out = await run_bundle_window(MINT, CURVE, _rpc(handler))
    assert out["share"] == "0.42"
    assert out["wallet_count"] == 3
    assert out["multi_signer_txs"] == 1
    assert out["still_held_share"] == "0.42"
    assert out["create_slot"] == 50
    assert "late" not in seen
    report = await run_score_report(
        ScoreReportIn(
            mint=MINT,
            curve_account=CURVE,
            checks=["bundler_cluster"],
        ),
        _rpc(handler),
    )
    row = next(item for item in report["checks"] if item["id"] == "bundler_cluster")
    assert row["status"] == "fail"
    assert "42.00%" in row["reason"]
    assert "3 wallets" in row["reason"]
    assert "1 multi-signer" in row["reason"]
    assert report["hard_veto"] is True
    assert report["verdict_recommendation"] == "KILL"


async def test_dev_sell_five_seconds_after_launch_fails() -> None:
    create = _tx(
        [CREATOR, CURVE],
        1,
        [(CREATOR, 0, 50_000), (CURVE, SUPPLY, SUPPLY - 50_000)],
        logs=_CREATE_LOGS,
        lamports=[(1_000_000_000, 500_000_000), (0, 500_000_000)],
    )
    sell = _tx(
        [CREATOR, CURVE],
        1,
        [(CREATOR, 50_000, 0), (CURVE, SUPPLY - 50_000, SUPPLY)],
        logs=_SELL_LOGS,
        lamports=[(500_000_000, 1_000_000_000), (500_000_000, 0)],
    )
    report = await _dev_report(create, sell, sell_slot=22, sell_time=START + 5)
    row = next(item for item in report["checks"] if item["id"] == "deployer_history")
    assert row["status"] == "fail"
    assert row["value"] == "dev_sell"
    assert "50000" in row["reason"]
    assert "5s" in row["reason"]
    assert "12 slots" in row["reason"]
    assert report["verdict_recommendation"] == "KILL"


async def test_dev_sell_outside_both_windows_passes() -> None:
    create = _tx(
        [CREATOR, CURVE],
        1,
        [(CREATOR, 0, 50_000), (CURVE, SUPPLY, SUPPLY - 50_000)],
        logs=_CREATE_LOGS,
    )
    sell = _tx(
        [CREATOR, CURVE],
        1,
        [(CREATOR, 50_000, 0), (CURVE, SUPPLY - 50_000, SUPPLY)],
        logs=_SELL_LOGS,
    )
    report = await _dev_report(create, sell, sell_slot=410, sell_time=START + 120)
    row = next(item for item in report["checks"] if item["id"] == "deployer_history")
    assert row["status"] == "pass"
    assert report["verdict_recommendation"] != "KILL"


async def _dev_report(
    create: dict[str, Any], sell: dict[str, Any], *, sell_slot: int, sell_time: int
) -> dict[str, Any]:
    txs = {"create": create, "sell": sell}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getSignaturesForAddress":
            return _response([_sig("sell", sell_slot, sell_time), _sig("create", 10, START)])
        if method == "getTransaction":
            assert params[1]["maxSupportedTransactionVersion"] == 1
            return _response(txs[params[0]])
        raise AssertionError(method)

    return await run_score_report(
        ScoreReportIn(mint=MINT, curve_account=CURVE, creator=CREATOR, checks=["deployer_history"]),
        _rpc(handler),
    )


def _lamport_tx(keys: list[str], before: list[int], after: list[int]) -> dict[str, Any]:
    return {
        "meta": {
            "err": None,
            "logMessages": [],
            "preBalances": before,
            "postBalances": after,
            "preTokenBalances": [],
            "postTokenBalances": [],
        },
        "transaction": {"message": {"header": {"numRequiredSignatures": 1}, "accountKeys": keys}},
    }


def _other_mint_tx(owner: str, before: int, after: int, logs: list[str]) -> dict[str, Any]:
    def row(amount: int) -> dict[str, Any]:
        return {
            "accountIndex": 0,
            "mint": OTHER,
            "owner": owner,
            "uiTokenAmount": {"amount": str(amount)},
        }

    return {
        "meta": {
            "err": None,
            "logMessages": logs,
            "preTokenBalances": [row(before)],
            "postTokenBalances": [row(after)],
        },
        "transaction": {
            "message": {"header": {"numRequiredSignatures": 1}, "accountKeys": [owner]}
        },
    }


async def test_funder_two_hops_back_fails_as_a_launch_chain() -> None:
    """B funded A, A funded the dev, and B dumped a launch four seconds later."""
    created = START
    txs: dict[str, dict[str, Any]] = {
        "scored": _tx(
            [CREATOR, CURVE],
            1,
            [(CREATOR, 0, 1_000), (CURVE, SUPPLY, SUPPLY - 1_000)],
            logs=_CREATE_LOGS,
        ),
        "a-funds-dev": _lamport_tx(
            [CREATOR, FUNDER],
            [1_000_000_000, 3_000_000_000],
            [2_000_000_000, 2_000_000_000],
        ),
        "b-funds-a": _lamport_tx(
            [FUNDER, HOP],
            [1_000_000_000, 5_000_000_000],
            [3_000_000_000, 3_000_000_000],
        ),
        "b-create": _other_mint_tx(HOP, 0, 80_000, _CREATE_LOGS),
        "b-sell": _other_mint_tx(HOP, 80_000, 0, _SELL_LOGS),
    }
    pages = {
        MINT: [_sig("scored", 10, created)],
        CREATOR: [_sig("scored", 10, created), _sig("a-funds-dev", 9, created - 30)],
        FUNDER: [
            _sig("a-funds-dev", 9, created - 30),
            _sig("b-funds-a", 8, created - 40),
        ],
        HOP: [
            _sig("b-funds-a", 8, created - 40),
            _sig("b-sell", 4, created - 996),
            _sig("b-create", 3, created - 1000),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getSignaturesForAddress":
            return _response(pages[params[0]])
        if method == "getTransaction":
            assert params[1]["maxSupportedTransactionVersion"] == 1
            return _response(txs[params[0]])
        raise AssertionError(method)

    report = await run_score_report(
        ScoreReportIn(mint=MINT, curve_account=CURVE, creator=CREATOR, checks=["deployer_history"]),
        _rpc(handler),
    )
    row = next(item for item in report["checks"] if item["id"] == "deployer_history")
    assert row["status"] == "fail"
    assert row["value"] == "launch_chain"
    assert "within seconds" in row["reason"]
    assert report["verdict_recommendation"] == "KILL"


async def test_null_transaction_is_blind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getTokenSupply":
            return _response({"value": {"amount": str(SUPPLY), "decimals": 6}})
        if method == "getSignaturesForAddress":
            return _response([_sig("missing", 10, START)])
        if method == "getTransaction":
            return _response(None)
        raise AssertionError(method)

    with pytest.raises(DataStale):
        await run_bundle_window(MINT, CURVE, _rpc(handler))


def _graduated_accounts(params: list[Any], base_vault: str) -> dict[str, Any]:
    encoding = params[1].get("encoding") if len(params) > 1 else ""
    if encoding == "jsonParsed":
        rows = [
            {
                "data": {
                    "parsed": {
                        "type": "account",
                        "info": {"owner": POOL if address == base_vault else address},
                    }
                }
            }
            for address in params[0]
        ]
        return {"value": rows}
    rows = [
        {"owner": PUMP_AMM_PROGRAM if address == POOL else SYSTEM, "lamports": 1}
        for address in params[0]
    ]
    return {"value": rows}


def _graduated_handler(
    base_vault: str,
    quote_vault: str,
    supply: int,
    base_units: int,
    quote_lamports: int,
    wallet_units: int,
) -> Any:
    accounts = {
        CURVE: _account(b"\x00" * 166, PUMP_PROGRAM),
        POOL: _account(_pool_bytes(base_vault, quote_vault), PUMP_AMM_PROGRAM),
    }
    balances = {
        base_vault: {"value": {"amount": str(base_units), "decimals": 6}},
        quote_vault: {"value": {"amount": str(quote_lamports), "decimals": 9}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getAccountInfo":
            return _response(accounts[params[0]])
        if method == "getTokenAccountBalance":
            return _response(balances[params[0]])
        if method == "getTokenSupply":
            return _response({"value": {"amount": str(supply), "decimals": 6}})
        if method == "getTokenLargestAccounts":
            return _response(_largest(base_vault, base_units, wallet_units))
        if method == "getMultipleAccounts":
            return _response(_graduated_accounts(params, base_vault))
        raise AssertionError(method)

    return handler


def _largest(base_vault: str, base_units: int, wallet_units: int) -> dict[str, Any]:
    return {
        "value": [
            {"address": base_vault, "uiAmount": base_units / 1_000_000, "decimals": 6},
            {"address": W1, "uiAmount": wallet_units / 1_000_000, "decimals": 6},
        ]
    }


async def test_graduated_pool_vault_above_99_percent_is_not_a_holder() -> None:
    base_vault = b58encode(bytes([11]) * 32)
    quote_vault = b58encode(bytes([12]) * 32)
    supply = 1_000_000_000_000_000
    base_units = 995_000_000_000_000
    quote_lamports = 50_000_000_000
    wallet_units = supply - base_units
    assert Decimal(base_units) / Decimal(supply) > Decimal("0.99")
    handler = _graduated_handler(
        base_vault, quote_vault, supply, base_units, quote_lamports, wallet_units
    )

    pool = await read_canonical_pool(MINT, _rpc(handler))
    assert pool.pool == POOL
    assert pool.base_units == base_units
    assert pool.quote_lamports == quote_lamports
    lp = await run_lp_status(LpStatusIn(mint=MINT, curve_account=CURVE), _rpc(handler))
    assert lp["stage"] == "graduated"
    assert lp["checked"] == ["pumpswap_pool"]
    report = await run_score_report(
        ScoreReportIn(
            mint=MINT,
            curve_account=CURVE,
            intended_size_lamports=1_000_000_000,
            checks=["lp_status", "liquidity_depth", "top10_concentration"],
        ),
        _rpc(handler),
    )
    statuses = {row["id"]: row for row in report["checks"]}
    assert statuses["lp_status"]["status"] == "warn"
    assert statuses["liquidity_depth"]["status"] == "pass"
    assert statuses["liquidity_depth"]["value"] >= 10
    assert "PumpSwap quote vault" in statuses["liquidity_depth"]["reason"]
    assert Decimal(str(statuses["top10_concentration"]["value"])) < Decimal("2")
    assert "lp_status" not in report["blind_checks"]
    assert "liquidity_depth" not in report["blind_checks"]
    assert "top10_concentration" not in report["blind_checks"]
    assert report["verdict_recommendation"] != "KILL"


async def test_missing_pool_is_blind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["method"] == "getAccountInfo"
        assert body["params"][0] == POOL
        return _response({"value": None})

    with pytest.raises(DataStale):
        await read_canonical_pool(MINT, _rpc(handler))
