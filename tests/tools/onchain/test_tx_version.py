"""Version-1 transaction reads fail closed when the body cannot be decoded.

No live network. A version-1 payload is decoded. The RPC error that version 0
produces, and a null result, are DataStale rather than an empty success.
"""

from __future__ import annotations

import json
import random
from typing import Any

import httpx
import pytest

from pumpgrok.core.errors import DataStale
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.market.trades import TradesIn, run_trades
from pumpgrok.tools.onchain.deployer_history import DeployerHistoryIn, run_deployer_history
from pumpgrok.tools.onchain.large_transfers import LargeTransfersIn, run_large_transfers
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.score_report import ScoreReportIn, run_score_report

START = 1_700_000_000
MINT = "So11111111111111111111111111111111111111112"
WALLET = "7" * 44
OTHER = "6" * 44
CREATOR = "9" * 44
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_VERSION_ERROR = {
    "code": -32015,
    "message": "Transaction version (1) is not supported by the requesting client.",
}


def _rpc(handler: Any) -> RpcClient:
    return RpcClient(
        ["https://rpc.invalid"],
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        FixtureClock(start=START),
        rng=random.Random(3),
        max_attempts_per_endpoint=1,
    )


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _version_error() -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": _VERSION_ERROR})


def _assert_version(params: list[Any]) -> None:
    assert params[1]["maxSupportedTransactionVersion"] == 1


def _versioned_sell() -> dict[str, Any]:
    return {
        "version": 1,
        "blockTime": START,
        "meta": {
            "err": None,
            "preBalances": [1000, 0],
            "postBalances": [1010, 0],
            "preTokenBalances": [
                {"accountIndex": 0, "mint": MINT, "uiTokenAmount": {"amount": "1000000"}},
            ],
            "postTokenBalances": [
                {"accountIndex": 0, "mint": MINT, "uiTokenAmount": {"amount": "0"}},
                {"accountIndex": 1, "mint": MINT, "uiTokenAmount": {"amount": "1000000"}},
            ],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}, {"pubkey": OTHER}]}},
    }


def _versioned_buy() -> dict[str, Any]:
    return {
        "version": 1,
        "blockTime": START,
        "meta": {
            "err": None,
            "preBalances": [100, 0],
            "postBalances": [90, 0],
            "preTokenBalances": [],
            "postTokenBalances": [
                {"accountIndex": 1, "mint": MINT, "uiTokenAmount": {"amount": "1000000"}},
            ],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}, {"pubkey": OTHER}]}},
    }


def _create_tx() -> dict[str, Any]:
    return {
        "version": 1,
        "meta": {
            "err": None,
            "logMessages": [
                f"Program {PUMP} invoke [1]",
                "Program log: Instruction: CreateV2",
                f"Program {PUMP} success",
            ],
        },
    }


async def test_version_1_transaction_counts_as_one_launch() -> None:
    seen: list[list[Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getSignaturesForAddress":
            return _ok([{"signature": "launch", "slot": 1, "blockTime": START}])
        if method == "getTransaction":
            seen.append(params)
            return _ok(_create_tx())
        raise AssertionError(method)

    out = await run_deployer_history(DeployerHistoryIn(creator=CREATOR), _rpc(handler))
    _assert_version(seen[0])
    assert out["tokens_seen"] == 1


async def test_unsupported_transaction_version_blinds_deployer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / ".planning" / "STATE.md"
    record.parent.mkdir(parents=True)
    record.write_text("paid_rpc_provider: test-provider\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getSignaturesForAddress":
            return _ok([{"signature": "v1", "slot": 1, "blockTime": START}])
        if method == "getTransaction":
            return _version_error()
        raise AssertionError(method)

    rpc = _rpc(handler)
    with pytest.raises(DataStale):
        await run_deployer_history(DeployerHistoryIn(creator=CREATOR), rpc)
    report = await run_score_report(
        ScoreReportIn(
            mint=MINT,
            curve_account=WALLET,
            creator=CREATOR,
            checks=["deployer_history"],
        ),
        rpc,
    )
    row = next(item for item in report["checks"] if item["id"] == "deployer_history")
    assert row["status"] == "blind"
    assert report["verdict_recommendation"] == "KILL"


async def test_version_1_sell_is_a_large_transfer() -> None:
    seen: list[list[Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getTokenSupply":
            return _ok({"value": {"amount": "100000000", "decimals": 6}})
        if method == "getSignaturesForAddress":
            return _ok([{"signature": "sell", "slot": 1, "blockTime": START}])
        if method == "getTransaction":
            seen.append(params)
            return _ok(_versioned_sell())
        raise AssertionError(method)

    out = await run_large_transfers(LargeTransfersIn(mint=MINT, wallets=[WALLET]), _rpc(handler))
    _assert_version(seen[0])
    assert len(out["transfers"]) == 1
    assert out["transfers"][0]["kind"] == "sell"


async def test_undecodable_transfer_does_not_report_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getTokenSupply":
            return _ok({"value": {"amount": "100000000", "decimals": 6}})
        if method == "getSignaturesForAddress":
            return _ok([{"signature": "missing", "slot": 1, "blockTime": START}])
        if method == "getTransaction":
            return _ok(None)
        raise AssertionError(method)

    with pytest.raises(DataStale):
        await run_large_transfers(LargeTransfersIn(mint=MINT, wallets=[WALLET]), _rpc(handler))


async def test_version_1_buy_is_a_trade() -> None:
    seen: list[list[Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getSignaturesForAddress":
            return _ok([{"signature": "buy", "slot": 1, "blockTime": START}])
        if method == "getTransaction":
            seen.append(params)
            return _ok(_versioned_buy())
        raise AssertionError(method)

    out = await run_trades(TradesIn(mint=MINT), _rpc(handler))
    _assert_version(seen[0])
    assert len(out["trades"]) == 1
    assert out["trades"][0]["side"] == "buy"
    assert out["trades"][0]["signature"] == "buy"


async def test_unsupported_transaction_version_does_not_yield_empty_trades() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getSignaturesForAddress":
            return _ok([{"signature": "v1", "slot": 1, "blockTime": START}])
        if method == "getTransaction":
            return _version_error()
        raise AssertionError(method)

    with pytest.raises(DataStale):
        await run_trades(TradesIn(mint=MINT), _rpc(handler))
