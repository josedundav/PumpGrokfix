"""CLI wiring for `pumpgrok safety score`. No RPC."""

from __future__ import annotations

import argparse
import json

import pytest

from pumpgrok.cli.dispatch import main
from pumpgrok.cli.errors import UsageError
from pumpgrok.cli.safety import safety_verb
from pumpgrok.config.settings import load_settings

MINT = "7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"


def test_safety_score_wires_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}

    def fake_call(verb: str, index: int, fields: dict[str, object], check: object = None) -> object:
        del index, check
        captured["verb"] = verb
        captured["fields"] = fields

        async def use(_tools: list[object]) -> int:
            return 0

        return use

    def fake_run(verb: str, seat: str, settings: object, caps: tuple[str, ...], use: object) -> int:
        del verb, settings, use
        captured["seat"] = seat
        captured["caps"] = caps
        return 0

    monkeypatch.setattr("pumpgrok.cli.safety._call_use", fake_call)
    monkeypatch.setattr("pumpgrok.cli.safety._run_wired", fake_run)
    code = main(["safety", "score", MINT, "--size-lamports", "2000000000"])
    assert code == 0
    assert captured["verb"] == "safety score"
    assert captured["seat"] == "rug"
    assert captured["caps"] == ("safety.score_report",)
    assert captured["fields"] == {"mint": MINT, "intended_size_lamports": 2000000000}
    assert capsys.readouterr().err == ""


def test_safety_score_forwards_a_known_creator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_call(verb: str, index: int, fields: dict[str, object], check: object = None) -> object:
        del verb, index, check
        captured["fields"] = fields

        async def use(_tools: list[object]) -> int:
            return 0

        return use

    monkeypatch.setattr("pumpgrok.cli.safety._call_use", fake_call)
    monkeypatch.setattr("pumpgrok.cli.safety._run_wired", lambda *_a, **_k: 0)
    creator = "So11111111111111111111111111111111111111112"
    code = main(["safety", "score", MINT, "--creator", creator])
    assert code == 0
    assert captured["fields"] == {
        "mint": MINT,
        "intended_size_lamports": 0,
        "creator": creator,
    }


def test_unknown_safety_command_is_a_usage_error() -> None:
    args = argparse.Namespace(cmd="nope", mint=MINT, size_lamports=0, creator=None)
    with pytest.raises(UsageError):
        safety_verb(args, load_settings())


def test_safety_score_rejects_a_negative_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pumpgrok.cli.safety._run_wired", lambda *_a, **_k: 0)
    code = main(["safety", "score", MINT, "--size-lamports", "-1"])
    assert code == 1


def test_safety_score_help_lists_the_size_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["safety", "score", "--help"])
    assert excinfo.value.code == 0
    assert "--size-lamports" in capsys.readouterr().out


def test_score_report_json_shape_is_what_the_cli_prints() -> None:
    """The verb prints the tool output. This pins the per-check keys."""
    report = {
        "score": 75,
        "verdict_recommendation": "KILL",
        "checks": [{"id": "bundler_cluster", "status": "fail", "reason": "42.00%"}],
    }
    text = json.dumps(report)
    parsed = json.loads(text)
    assert parsed["checks"][0]["id"] == "bundler_cluster"
    assert parsed["verdict_recommendation"] == "KILL"
