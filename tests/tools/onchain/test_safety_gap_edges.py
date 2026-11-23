"""Error paths for the safety-reader gaps. No network."""

from __future__ import annotations

import base64
import json
import random
from decimal import Decimal
from typing import Any

import httpx
import pytest

from pumpgrok.config.settings import SafetyThresholds
from pumpgrok.core.errors import DataStale
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.onchain.bundle_window import collect_signatures
from pumpgrok.tools.onchain.curve_layout import encode_curve_fixture
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
from pumpgrok.tools.onchain.pumpswap_pool import read_canonical_pool, vaults_from_pool_data
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.onchain.tx_accounts import (
    account_keys,
    first_signer,
    lamport_deltas,
    require_transaction,
    signature_page,
    signer_count,
    token_owner_deltas,
    token_supply_units,
)
from pumpgrok.tools.safety.checks.readers import (
    check_deployer_history,
    check_liquidity_depth,
    check_lp_status,
    check_top10_concentration,
)
from pumpgrok.tools.safety.score_report import ScoreReportIn, _curve_account

MINT = "7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"
LABEL = "edge"


def _rpc(handler: Any) -> RpcClient:
    return RpcClient(
        ["https://rpc.invalid"],
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        FixtureClock(start=1_700_000_000),
        rng=random.Random(1),
        max_attempts_per_endpoint=1,
    )


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _err() -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}})


def test_bad_addresses_are_unreadable() -> None:
    with pytest.raises(DataStale):
        b58decode_pubkey("0OIl")
    with pytest.raises(DataStale):
        b58decode_pubkey("1" * 50)
    assert _curve_account(ScoreReportIn(mint=MINT)) == bonding_curve_address(MINT)


def test_parsed_keys_signers_and_loaded_addresses() -> None:
    tx = {
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": "A", "signer": True}, {"pubkey": "B", "signer": False}]
            }
        },
        "meta": {"loadedAddresses": {"writable": ["C"], "readonly": ["", "D"]}},
    }
    assert account_keys(tx) == ["A", "B", "C", "D"]
    assert signer_count(tx) == 1
    assert first_signer(tx) == "A"
    assert signer_count({}) == 0
    assert first_signer({"transaction": {"message": {}}}) is None


def test_malformed_balances_are_unreadable() -> None:
    with pytest.raises(DataStale):
        token_owner_deltas({"preTokenBalances": "nope"}, MINT, LABEL)
    with pytest.raises(DataStale):
        token_owner_deltas(
            {"preTokenBalances": [{"mint": MINT, "owner": "", "accountIndex": 0}]},
            MINT,
            LABEL,
        )
    with pytest.raises(DataStale):
        token_owner_deltas(
            {"preTokenBalances": [{"mint": MINT, "owner": "A", "accountIndex": 0}]},
            MINT,
            LABEL,
        )
    assert lamport_deltas({"meta": {}}, LABEL) == {}
    with pytest.raises(DataStale):
        lamport_deltas({"meta": {"preBalances": "x", "postBalances": []}}, LABEL)
    bad = {
        "meta": {"preBalances": ["x"], "postBalances": [1]},
        "transaction": {"message": {"accountKeys": ["A"]}},
    }
    with pytest.raises(DataStale):
        lamport_deltas(bad, LABEL)
    short = {
        "meta": {"preBalances": [2], "postBalances": [1]},
        "transaction": {"message": {"accountKeys": ["A", "B"]}},
    }
    assert lamport_deltas(short, LABEL) == {"A": -1}


def test_pool_bytes_must_be_the_canonical_pair() -> None:
    with pytest.raises(DataStale):
        vaults_from_pool_data(b"\x00" * 10, MINT)
    raw = bytearray(243)
    raw[43:75] = b58decode_pubkey(MINT)
    raw[75:107] = b58decode_pubkey(MINT)
    with pytest.raises(DataStale):
        vaults_from_pool_data(bytes(raw), MINT)


def test_check_edges_keep_existing_floors() -> None:
    thresholds = SafetyThresholds()
    assert check_lp_status({"stage": "mystery"}).status == "blind"
    warn = check_top10_concentration({"top10_share": "0.22"}, thresholds)
    assert warn.status == "warn"
    assert check_deployer_history({"is_known_bad": True, "rugs": 0}).status == "fail"
    assert check_deployer_history({"tokens_seen": 4}).status == "warn"
    assert check_deployer_history({"dev_sold": True}).reason.endswith("at launch")
    empty = check_liquidity_depth({"liquidity_lamports": 0}, 1, thresholds)
    assert empty.status == "fail"
    vacuous = check_liquidity_depth({"liquidity_lamports": 10}, 0, thresholds)
    assert vacuous.status == "pass"
    mid = check_liquidity_depth({"liquidity_lamports": 15}, 1, thresholds)
    assert mid.status == "warn"


async def test_supply_transaction_and_signature_failures() -> None:
    async def run(result: Any, call: Any) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            if result == "error":
                return _err()
            return _ok(result)

        with pytest.raises(DataStale):
            await call(_rpc(handler))

    await run("error", lambda rpc: token_supply_units(rpc, MINT, LABEL))
    await run(None, lambda rpc: token_supply_units(rpc, MINT, LABEL))
    await run({"value": {"amount": "0"}}, lambda rpc: token_supply_units(rpc, MINT, LABEL))
    await run({"value": {}}, lambda rpc: token_supply_units(rpc, MINT, LABEL))
    await run("error", lambda rpc: require_transaction(rpc, "sig", LABEL))
    await run(None, lambda rpc: require_transaction(rpc, "sig", LABEL))
    await run({"meta": None}, lambda rpc: require_transaction(rpc, "sig", LABEL))
    await run("error", lambda rpc: signature_page(rpc, MINT, "before", 1, LABEL))
    await run(None, lambda rpc: signature_page(rpc, MINT, None, 1, LABEL))
    await run([{"signature": 1, "slot": 1}], lambda rpc: signature_page(rpc, MINT, None, 1, LABEL))
    await run(
        [{"signature": "s", "slot": "nope"}],
        lambda rpc: signature_page(rpc, MINT, None, 1, LABEL),
    )


async def test_empty_and_truncated_signature_history() -> None:
    def empty(_request: httpx.Request) -> httpx.Response:
        return _ok([])

    with pytest.raises(DataStale):
        await collect_signatures(_rpc(empty), MINT, LABEL)

    page = [{"signature": f"s{i}", "slot": i} for i in range(1000)]

    def full(_request: httpx.Request) -> httpx.Response:
        return _ok(page)

    with pytest.raises(DataStale):
        await collect_signatures(_rpc(full), MINT, LABEL)


async def test_pool_read_failures() -> None:
    pool = canonical_pool_address(MINT)

    def missing(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body["method"] == "getAccountInfo":
            value = {"owner": "11111111111111111111111111111111", "data": ["", "base64"]}
            return _ok({"value": value})
        return _err()

    with pytest.raises(DataStale):
        await read_canonical_pool(MINT, _rpc(missing))

    def rpc_down(_request: httpx.Request) -> httpx.Response:
        return _err()

    with pytest.raises(DataStale):
        await read_canonical_pool(MINT, _rpc(rpc_down))

    raw = bytearray(243)
    raw[43:75] = b58decode_pubkey(MINT)
    raw[75:107] = b58decode_pubkey(WSOL_MINT)
    vault = b58encode(bytes([4]) * 32)
    raw[139:171] = b58decode_pubkey(vault)
    raw[171:203] = b58decode_pubkey(vault)
    encoded = base64.b64encode(bytes(raw)).decode("ascii")

    def bad_vault(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body["method"] == "getAccountInfo" and body["params"][0] == pool:
            return _ok({"value": {"owner": PUMP_AMM_PROGRAM, "data": [encoded, "base64"]}})
        if body["method"] == "getTokenAccountBalance":
            return _ok({"value": None})
        return _err()

    with pytest.raises(DataStale):
        await read_canonical_pool(MINT, _rpc(bad_vault))


async def test_complete_curve_reads_the_pool() -> None:
    curve = encode_curve_fixture(
        virtual_sol_reserves_lamports=30_000_000_000,
        virtual_token_reserves_units=1_000_000_000,
        real_sol_reserves_lamports=1,
        real_token_reserves_units=1,
        total_supply_units=1_000_000_000,
        complete=True,
    )
    pool = canonical_pool_address(MINT)
    vault = b58encode(bytes([5]) * 32)
    raw = bytearray(243)
    raw[43:75] = b58decode_pubkey(MINT)
    raw[75:107] = b58decode_pubkey(WSOL_MINT)
    raw[139:171] = b58decode_pubkey(vault)
    raw[171:203] = b58decode_pubkey(vault)
    encoded = base64.b64encode(bytes(raw)).decode("ascii")
    curve_b64 = base64.b64encode(curve).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method, params = body["method"], body["params"]
        if method == "getAccountInfo" and params[0] != pool:
            return _ok({"value": {"owner": PUMP_PROGRAM, "data": [curve_b64, "base64"]}})
        if method == "getAccountInfo":
            return _ok({"value": {"owner": PUMP_AMM_PROGRAM, "data": [encoded, "base64"]}})
        if method == "getTokenAccountBalance":
            return _ok({"value": {"amount": "10", "decimals": 9}})
        return _err()

    out = await run_lp_status(
        LpStatusIn(mint=MINT, curve_account=bonding_curve_address(MINT)),
        _rpc(handler),
    )
    assert out["stage"] == "graduated"
    assert out["curve_complete"] is True


def test_quote_depth_multiple_uses_the_ten_x_floor() -> None:
    thresholds = SafetyThresholds()
    assert thresholds.liquidity_multiple_min == 10.0
    row = check_liquidity_depth(
        {"liquidity_lamports": 20, "depth_source": "pumpswap_quote_vault"},
        1,
        thresholds,
    )
    assert row.status == "pass"
    assert "PumpSwap quote vault" in row.reason
    assert Decimal(str(row.value)) == Decimal(20)
