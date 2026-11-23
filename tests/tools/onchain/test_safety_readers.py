"""Recorded-fixture tests for the three on-chain safety reader defects.

No live network. Curve bytes and pump log lines were captured from public
accounts; the holder rows use the observed pumpswap vault authority and
exact amounts that sum to the hand-derived 16.16% top-10 share.
"""

from __future__ import annotations

import base64
import json
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from pumpgrok.core.errors import DataStale
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.market.liquidity import LiquidityIn, run_liquidity
from pumpgrok.tools.onchain.curve_layout import decode_curve_account
from pumpgrok.tools.onchain.deployer_history import (
    DeployerHistoryIn,
    count_pump_creates,
    run_deployer_history,
)
from pumpgrok.tools.onchain.lp_status import LpStatusIn, run_lp_status
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.onchain.top_holders import TopHoldersIn, run_top_holders
from pumpgrok.tools.safety.score_report import ScoreReportIn, run_score_report

FIXTURES = Path(__file__).resolve().parent / "fixtures"
START = 1_700_000_000
SYSTEM = "11111111111111111111111111111111"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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


def test_recorded_166_byte_curve_decodes_reserve_prefix() -> None:
    recorded = _load("bonding_curve_166.json")
    raw = base64.b64decode(recorded["account_base64"])
    assert len(raw) == recorded["account_len"] == 166
    state = decode_curve_account(raw)
    assert state.virtual_token_reserves_units == recorded["vtok"]
    assert state.virtual_sol_reserves_lamports == recorded["vsol"]
    assert state.real_token_reserves_units == recorded["rtok"]
    assert state.real_sol_reserves_lamports == recorded["rsol"]
    assert state.total_supply_units == recorded["supply"]
    assert state.complete is False
    assert state.token_mint == ""


async def test_lp_status_and_depth_read_recorded_curve() -> None:
    recorded = _load("bonding_curve_166.json")
    account = {
        "context": {"slot": 7},
        "value": {
            "data": [recorded["account_base64"], "base64"],
            "executable": False,
            "lamports": 1,
            "owner": recorded["owner_program"],
            "rentEpoch": 1,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["method"] == "getAccountInfo"
        assert body["params"][0] == recorded["curve_account"]
        return _response(account)

    rpc = _rpc(handler)
    lp = await run_lp_status(
        LpStatusIn(mint=recorded["mint"], curve_account=recorded["curve_account"]), rpc
    )
    assert lp["stage"] == "bonding_curve"
    assert lp["curve_complete"] is False
    depth = await run_liquidity(
        LiquidityIn(
            mint=recorded["mint"],
            curve_account=recorded["curve_account"],
            order_size_lamports=1_000_000,
        ),
        rpc,
    )
    assert depth["liquidity_lamports"] == recorded["vsol"]
    assert depth["graduated"] is False
    report = await run_score_report(
        ScoreReportIn(
            mint=recorded["mint"],
            curve_account=recorded["curve_account"],
            intended_size_lamports=1_000_000,
            checks=["lp_status", "liquidity_depth"],
        ),
        rpc,
    )
    statuses = {row["id"]: row["status"] for row in report["checks"]}
    assert statuses["lp_status"] == "pass"
    assert statuses["liquidity_depth"] == "pass"
    assert "lp_status" not in report["blind_checks"]
    assert "liquidity_depth" not in report["blind_checks"]


async def test_short_curve_stays_unreadable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(
            {
                "value": {
                    "data": [base64.b64encode(b"\x00" * 40).decode("ascii"), "base64"],
                    "owner": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                }
            }
        )

    rpc = _rpc(handler)
    mint = "6Ax4PsG3Lm789GhDj9Vc9buB9MDbah3K4Zz4XBkCpump"
    curve = "478abPhAcEBk94pDzZXx5GvZBzHsnWcNTnziTkSqxK8H"
    with pytest.raises(DataStale):
        await run_lp_status(LpStatusIn(mint=mint, curve_account=curve), rpc)
    with pytest.raises(DataStale):
        await run_liquidity(LiquidityIn(mint=mint, curve_account=curve), rpc)


def _wallet(index: int) -> str:
    return f"{index + 1}{'W' * 31}"


async def test_top10_excludes_pumpswap_vault() -> None:
    recorded = _load("top10_ex_lp.json")
    decimals = int(recorded["decimals"])
    wallets = [_wallet(i) for i in range(int(recorded["wallet_count"]))]
    largest: list[dict[str, Any]] = [
        {
            "address": recorded["vault"],
            "uiAmount": recorded["vault_ui_amount"],
            "decimals": decimals,
        }
    ]
    largest.extend(
        {"address": wallet, "uiAmount": recorded["wallet_ui_amount"], "decimals": decimals}
        for wallet in wallets
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getTokenLargestAccounts":
            return _response({"value": largest})
        if method == "getTokenSupply":
            return _response({"value": {"amount": recorded["supply_units"], "decimals": decimals}})
        if method == "getMultipleAccounts":
            encoding = params[1].get("encoding")
            if encoding == "jsonParsed":
                rows = []
                for address in params[0]:
                    authority = recorded["pool"] if address == recorded["vault"] else address
                    rows.append(
                        {
                            "data": {
                                "parsed": {
                                    "type": "account",
                                    "info": {"owner": authority, "mint": recorded["mint"]},
                                }
                            }
                        }
                    )
                return _response({"value": rows})
            rows = []
            for authority in params[0]:
                program = recorded["pool_program"] if authority == recorded["pool"] else SYSTEM
                rows.append({"owner": program, "data": ["", "base64"], "lamports": 1})
            return _response({"value": rows})
        raise AssertionError(method)

    out = await run_top_holders(TopHoldersIn(mint=recorded["mint"]), _rpc(handler))
    assert out["top10_share"] == Decimal(recorded["expected_top10_share"])
    assert recorded["vault"] in out["excluded"]
    assert all(row["address"] != recorded["vault"] for row in out["holders"])
    assert len(out["holders"]) == int(recorded["wallet_count"])


async def test_bonding_curve_vault_is_excluded() -> None:
    mint = "6Ax4PsG3Lm789GhDj9Vc9buB9MDbah3K4Zz4XBkCpump"
    vault = "D8dkp3VWvxGL44LAierctLDG8YGppqmg2ZChAeKhu5aL"
    curve = "478abPhAcEBk94pDzZXx5GvZBzHsnWcNTnziTkSqxK8H"
    wallet = _wallet(0)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getTokenLargestAccounts":
            return _response(
                {
                    "value": [
                        {"address": vault, "uiAmount": 500.0, "decimals": 6},
                        {"address": wallet, "uiAmount": 10.0, "decimals": 6},
                    ]
                }
            )
        if method == "getTokenSupply":
            return _response({"value": {"amount": "1000000000", "decimals": 6}})
        if method == "getMultipleAccounts" and params[1].get("encoding") == "jsonParsed":
            return _response(
                {
                    "value": [
                        {"data": {"parsed": {"type": "account", "info": {"owner": curve}}}},
                        {"data": {"parsed": {"type": "account", "info": {"owner": wallet}}}},
                    ]
                }
            )
        if method == "getMultipleAccounts":
            programs = {
                curve: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                wallet: SYSTEM,
            }
            return _response(
                {"value": [{"owner": programs[addr], "lamports": 1} for addr in params[0]]}
            )
        raise AssertionError(method)

    out = await run_top_holders(TopHoldersIn(mint=mint), _rpc(handler))
    assert vault in out["excluded"]
    assert [row["address"] for row in out["holders"]] == [wallet]
    assert out["top10_share"] == Decimal("0.01")


async def test_unreadable_holder_authority_is_blind() -> None:
    mint = "6Ax4PsG3Lm789GhDj9Vc9buB9MDbah3K4Zz4XBkCpump"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getTokenLargestAccounts":
            return _response({"value": [{"address": "A" * 32, "uiAmount": 1.0, "decimals": 6}]})
        if method == "getMultipleAccounts":
            return _response({"value": [None]})
        raise AssertionError(method)

    with pytest.raises(DataStale):
        await run_top_holders(TopHoldersIn(mint=mint), _rpc(handler))


def test_recorded_logs_count_one_create_and_no_sells() -> None:
    recorded = _load("deployer_logs.json")
    assert count_pump_creates(recorded["create_logs"]) == 1
    assert count_pump_creates(recorded["sell_logs"]) == 0
    ata_only = [
        "Program ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL invoke [1]",
        "Program log: Create",
        "Program ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL success",
    ]
    assert count_pump_creates(ata_only) == 0


async def test_deployer_history_counts_creates_not_signature_burst() -> None:
    recorded = _load("deployer_logs.json")
    creator = "4x5B41eE2ocZ3rJivpa6G43byU4TDr4XvKPN9LR6pAsh"
    signatures = ["create" if i == 0 else f"s{i:03d}" for i in range(100)]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getSignaturesForAddress":
            assert params[1]["limit"] == 100
            return _response(
                [
                    {"signature": sig, "slot": i, "blockTime": START + (i % 8)}
                    for i, sig in enumerate(signatures)
                ]
            )
        if method == "getTransaction":
            assert params[1]["maxSupportedTransactionVersion"] == 1
            logs = recorded["create_logs"] if params[0] == "create" else recorded["sell_logs"]
            return _response({"meta": {"err": None, "logMessages": logs}})
        raise AssertionError(method)

    out = await run_deployer_history(DeployerHistoryIn(creator=creator), _rpc(handler))
    assert out["tokens_seen"] == 1
    assert out["recent_signatures"][0] == "create"
    assert len(out["recent_signatures"]) == 10


async def test_failed_create_is_not_a_launch() -> None:
    recorded = _load("deployer_logs.json")
    creator = "4x5B41eE2ocZ3rJivpa6G43byU4TDr4XvKPN9LR6pAsh"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getSignaturesForAddress":
            return _response([{"signature": "failed", "slot": 1, "blockTime": START}])
        if method == "getTransaction":
            return _response(
                {
                    "meta": {
                        "err": {"InstructionError": [0, "Custom"]},
                        "logMessages": recorded["create_logs"],
                    }
                }
            )
        raise AssertionError(method)

    out = await run_deployer_history(DeployerHistoryIn(creator=creator), _rpc(handler))
    assert out["tokens_seen"] == 0


async def test_unreadable_transaction_fails_closed() -> None:
    creator = "4x5B41eE2ocZ3rJivpa6G43byU4TDr4XvKPN9LR6pAsh"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getSignaturesForAddress":
            return _response([{"signature": "missing", "slot": 1, "blockTime": START}])
        if method == "getTransaction":
            return _response(None)
        raise AssertionError(method)

    with pytest.raises(DataStale):
        await run_deployer_history(DeployerHistoryIn(creator=creator), _rpc(handler))
