"""Program-address vectors. No network."""

from __future__ import annotations

from pumpgrok.tools.onchain.pda import (
    b58decode_pubkey,
    b58encode,
    bonding_curve_address,
    canonical_pool_address,
    find_program_address,
)


def test_published_program_addresses() -> None:
    system = b58decode_pubkey("11111111111111111111111111111111")
    hello, hello_bump = find_program_address([b"helloWorld"], system)
    assert b58encode(hello) == "46GZzzetjCURsdFPb7rcnspbEMnCBXe9kpjrsZAkKb6X"
    assert hello_bump == 254
    loader = b58decode_pubkey("BPFLoader1111111111111111111111111111111111")
    empty, empty_bump = find_program_address([b""], loader)
    assert b58encode(empty) == "EXWkUCz3YJU9TDVk39ogA4TwoVsUi75ZDhH6yT7acPgQ"
    assert empty_bump == 255


def test_readme_pool_is_the_canonical_pool() -> None:
    """The PumpSwap README example pool is the index-0 pool of that mint."""
    mint = "7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"
    assert canonical_pool_address(mint) == "GseMAnNDvntR5uFePZ51yZBXzNSn7GdFPkfHwfr6d77J"
    assert bonding_curve_address(mint) == "3MUkKMbuornHohtAtzrToSzqkj1gEEhQqYVz8sZnmQg1"
