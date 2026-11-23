"""Program-derived addresses for pump.fun and PumpSwap (read-only).

Solana addresses here are derived with the canonical bump. Nothing in this
module signs, and the ed25519 check only rejects points that lie on the
curve so a derived address cannot be a keypair.
"""

from __future__ import annotations

import hashlib

from pumpgrok.core.errors import DataStale

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_PUBKEY_LEN = 32
_CURVE_P = 2**255 - 19
_CURVE_D = (-121665 * pow(121666, _CURVE_P - 2, _CURVE_P)) % _CURVE_P
_CURVE_SQRT_M1 = pow(2, (_CURVE_P - 1) // 4, _CURVE_P)

#: pump.fun program. Pool-authority and bonding-curve PDAs live here.
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
#: PumpSwap AMM program. Canonical graduated pools are owned by this program.
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
#: Wrapped SOL, the quote mint of a canonical pump pool.
WSOL_MINT = "So11111111111111111111111111111111111111112"

_PDA_MARKER = b"ProgramDerivedAddress"
_POOL_AUTHORITY_SEED = b"pool-authority"
_POOL_SEED = b"pool"
_CURVE_SEED = b"bonding-curve"
_CANONICAL_POOL_INDEX = 0


def b58encode(raw: bytes) -> str:
    """Encode bytes the way Solana encodes a public key."""
    number = int.from_bytes(raw, "big")
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        digits.append(_ALPHABET[remainder])
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + "".join(reversed(digits))


def b58decode_pubkey(text: str) -> bytes:
    """Decode one base58 public key to 32 bytes, or raise ``DataStale``."""
    try:
        number = 0
        for char in text:
            number = number * 58 + _ALPHABET.index(char)
    except ValueError as exc:
        raise DataStale("address is not base58") from exc
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    raw = b"\x00" * (len(text) - len(text.lstrip("1"))) + body
    if len(raw) > _PUBKEY_LEN:
        raise DataStale("address is longer than 32 bytes")
    return raw.rjust(_PUBKEY_LEN, b"\x00")


def _is_curve_point(data: bytes) -> bool:
    """True when ``data`` is a compressed ed25519 point (not a valid PDA)."""
    if len(data) != _PUBKEY_LEN:
        return False
    coordinate = int.from_bytes(data, "little")
    parity = coordinate >> 255
    y_coord = coordinate & ((1 << 255) - 1)
    if y_coord >= _CURVE_P:
        return False
    y_squared = y_coord * y_coord % _CURVE_P
    numerator = (y_squared - 1) % _CURVE_P
    denominator = (_CURVE_D * y_squared + 1) % _CURVE_P
    x_squared = numerator * pow(denominator, _CURVE_P - 2, _CURVE_P) % _CURVE_P
    if x_squared == 0:
        return parity == 0
    x_coord = pow(x_squared, (_CURVE_P + 3) // 8, _CURVE_P)
    if (x_coord * x_coord - x_squared) % _CURVE_P != 0:
        x_coord = x_coord * _CURVE_SQRT_M1 % _CURVE_P
    if (x_coord * x_coord - x_squared) % _CURVE_P != 0:
        return False
    if (x_coord & 1) != parity:
        x_coord = _CURVE_P - x_coord
    return True


def find_program_address(seeds: list[bytes], program: bytes) -> tuple[bytes, int]:
    """Canonical program address and bump. The bump is the first off-curve byte."""
    prefix = b"".join(seeds)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(prefix + bytes((bump,)) + program + _PDA_MARKER).digest()
        if not _is_curve_point(digest):
            return digest, bump
    raise DataStale("no program address for these seeds")


def bonding_curve_address(mint: str) -> str:
    """Bonding-curve PDA for ``mint`` (``["bonding-curve", mint]``)."""
    curve, _bump = find_program_address(
        [_CURVE_SEED, b58decode_pubkey(mint)],
        b58decode_pubkey(PUMP_PROGRAM),
    )
    return b58encode(curve)


def canonical_pool_address(mint: str) -> str:
    """PumpSwap pool a pump coin graduates into.

    Index 0, created by the pump ``pool-authority`` PDA of ``mint``, quoted
    in wrapped SOL. The account is owned by :data:`PUMP_AMM_PROGRAM`.
    """
    mint_bytes = b58decode_pubkey(mint)
    authority, _auth_bump = find_program_address(
        [_POOL_AUTHORITY_SEED, mint_bytes],
        b58decode_pubkey(PUMP_PROGRAM),
    )
    index = _CANONICAL_POOL_INDEX.to_bytes(2, "little")
    pool, _pool_bump = find_program_address(
        [_POOL_SEED, index, authority, mint_bytes, b58decode_pubkey(WSOL_MINT)],
        b58decode_pubkey(PUMP_AMM_PROGRAM),
    )
    return b58encode(pool)
