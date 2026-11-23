"""Concentration, deployer, LP, extension and depth checks (TOOL-003).

Pure functions over the typed reader payloads; thresholds come from
``SafetyThresholds`` (config), never from literals.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pumpgrok.config.settings import SafetyThresholds
from pumpgrok.tools.safety.checks import CheckResult
from pumpgrok.tools.safety.checks.constants import BUNDLE_WINDOW_FAIL_SHARE


def check_lp_status(lp: dict[str, Any]) -> CheckResult:
    """Pass on an open curve; warn once graduated (lock state unwired)."""
    stage = str(lp.get("stage", "unknown"))
    if stage == "bonding_curve":
        return CheckResult(
            id="lp_status",
            status="pass",
            value=stage,
            threshold="bonding_curve",
            weight=Decimal("0.15"),
            reason="token still on the bonding curve; no LP to lock",
        )
    if stage == "graduated":
        return CheckResult(
            id="lp_status",
            status="warn",
            value=stage,
            threshold="bonding_curve",
            weight=Decimal("0.15"),
            reason="graduated to a pool; lock/burn state is unwired in Phase 3",
        )
    return CheckResult(
        id="lp_status",
        status="blind",
        value=stage,
        threshold="bonding_curve",
        weight=Decimal("0.15"),
        reason="liquidity stage could not be determined",
    )


def check_top10_concentration(top: dict[str, Any], thresholds: SafetyThresholds) -> CheckResult:
    """Fail/warn/pass the top-10 supply share against configured pcts."""
    share = Decimal(str(top.get("top10_share", "0"))) * Decimal(100)
    pct = float(share)
    status: Literal["fail", "warn", "pass"]
    if pct >= thresholds.top10_concentration_fail_pct:
        status = "fail"
    elif pct >= thresholds.top10_concentration_warn_pct:
        status = "warn"
    else:
        status = "pass"
    return CheckResult(
        id="top10_concentration",
        status=status,
        value=pct,
        threshold=thresholds.top10_concentration_fail_pct,
        weight=Decimal("0.15"),
        reason=f"top-10 holders own {pct:.2f}% of supply",
    )


def _dump_timing(dep: dict[str, Any]) -> str:
    parts: list[str] = []
    delay_s = dep.get("dev_sell_delay_s")
    delay_slots = dep.get("dev_sell_delay_slots")
    if isinstance(delay_s, int):
        parts.append(f"{delay_s}s")
    if isinstance(delay_slots, int):
        parts.append(f"{delay_slots} slots")
    return " and ".join(parts) if parts else "at launch"


def check_bundler_cluster(bundle: dict[str, Any]) -> CheckResult:
    """Fail when the create-window buy is at least the configured share."""
    share = Decimal(str(bundle.get("share", "0")))
    held = Decimal(str(bundle.get("still_held_share", "0")))
    wallets = int(bundle.get("wallet_count", 0))
    multi = int(bundle.get("multi_signer_txs", 0))
    status: Literal["fail", "pass"] = "fail" if share >= BUNDLE_WINDOW_FAIL_SHARE else "pass"
    pct = share * Decimal(100)
    held_pct = held * Decimal(100)
    return CheckResult(
        id="bundler_cluster",
        status=status,
        value=float(pct),
        threshold=float(BUNDLE_WINDOW_FAIL_SHARE * 100),
        weight=Decimal("0.10"),
        reason=(
            f"{wallets} wallets bought {pct:.2f}% of supply in the create window; "
            f"{multi} multi-signer transactions; they still hold {held_pct:.2f}%"
        ),
    )


def check_deployer_history(dep: dict[str, Any]) -> CheckResult:
    """Fail a launch chain, an instant dev dump, or a known-bad creator."""
    if bool(dep.get("launch_chain")):
        return CheckResult(
            id="deployer_history",
            status="fail",
            value="launch_chain",
            threshold="no launch chain",
            weight=Decimal("0.10"),
            reason=("funder hop created a token in the past 24h and sold it within seconds"),
        )
    if bool(dep.get("dev_sold")):
        units = int(dep.get("dev_sell_units") or 0)
        timing = _dump_timing(dep)
        when = timing if timing == "at launch" else f"{timing} after launch"
        return CheckResult(
            id="deployer_history",
            status="fail",
            value="dev_sell",
            threshold="no immediate dev sell",
            weight=Decimal("0.10"),
            reason=f"creator sold or transferred {units} units {when}",
        )
    if bool(dep.get("is_known_bad")) or int(dep.get("rugs", 0)) > 0:
        return CheckResult(
            id="deployer_history",
            status="fail",
            value=True,
            threshold="no known rugs",
            weight=Decimal("0.10"),
            reason="creator record shows known-bad history",
        )
    seen = int(dep.get("tokens_seen", 0))
    if seen >= 3:
        return CheckResult(
            id="deployer_history",
            status="warn",
            value=seen,
            threshold=3,
            weight=Decimal("0.10"),
            reason=f"creator has launched {seen} tokens in the window; serial-launcher caution",
        )
    return CheckResult(
        id="deployer_history",
        status="pass",
        value=seen,
        threshold=3,
        weight=Decimal("0.10"),
        reason="no known-bad creator history in the window",
    )


def check_token2022_extensions(meta: dict[str, Any]) -> CheckResult:
    """Warn when transfer-hook-style extensions are present."""
    extensions = meta.get("extensions") or []
    count = len(extensions)
    return CheckResult(
        id="token2022_extensions",
        status="warn" if count else "pass",
        value=float(count),
        threshold="none",
        weight=Decimal("0.05"),
        reason=(
            f"{count} token-2022 extensions present; review transfer hooks"
            if count
            else "no token-2022 extensions"
        ),
    )


def check_liquidity_depth(
    liq: dict[str, Any], intended_size_lamports: int, thresholds: SafetyThresholds
) -> CheckResult:
    """Fail/warn/pass the liquidity multiple against configured floors."""
    liquidity = int(liq.get("liquidity_lamports", 0))
    if liquidity <= 0:
        return CheckResult(
            id="liquidity_depth",
            status="fail",
            value=0.0,
            threshold=thresholds.liquidity_multiple_min,
            weight=Decimal("0.15"),
            reason="no measurable liquidity for the intended size",
        )
    if intended_size_lamports <= 0:
        return CheckResult(
            id="liquidity_depth",
            status="pass",
            value=float("inf"),
            threshold=thresholds.liquidity_multiple_min,
            weight=Decimal("0.15"),
            reason="no intended size; depth check vacuous",
        )
    multiple = liquidity / intended_size_lamports
    status: Literal["fail", "warn", "pass"]
    if multiple < thresholds.liquidity_multiple_min:
        status = "fail"
    elif multiple < thresholds.liquidity_multiple_recommended:
        status = "warn"
    else:
        status = "pass"
    source = ""
    if liq.get("depth_source") == "pumpswap_quote_vault":
        source = " from the PumpSwap quote vault"
    return CheckResult(
        id="liquidity_depth",
        status=status,
        value=multiple,
        threshold=thresholds.liquidity_multiple_min,
        weight=Decimal("0.15"),
        reason=f"liquidity is {multiple:.1f}x the intended size{source}",
    )
