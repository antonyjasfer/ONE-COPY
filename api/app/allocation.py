from __future__ import annotations

import hashlib
import hmac
import secrets

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_receipt_code() -> str:
    """Generate an 8-character Crockford Base32 receipt code (40 bits of entropy)."""
    value = secrets.randbits(40)
    chars: list[str] = []
    for shift in range(35, -1, -5):
        chars.append(CROCKFORD_ALPHABET[(value >> shift) & 0x1F])
    return "".join(chars)


def normalize_receipt_code(code: str) -> str:
    """Normalize Crockford Base32 input (case-insensitive, I/L->1, O->0)."""
    cleaned = code.strip().replace("-", "").replace(" ", "").upper()
    return cleaned.translate(str.maketrans({"I": "1", "L": "1", "O": "0"}))


def paired_failure_mode(balance_secret: str, experiment: str, index: int) -> str:
    """Deterministically assign 'reply_lost' or 'request_lost' in balanced pairs.

    Uses HMAC-SHA256(BALANCE_SECRET, f"{experiment}:{pair_index}") so every pair
    (2k, 2k+1) in each (experiment, cohort, variant) gets 1 'reply_lost' and
    1 'request_lost' in identical order across arms.
    """
    pair_index = index // 2
    slot = index % 2
    digest = hmac.new(
        balance_secret.encode("utf-8"),
        f"{experiment}:{pair_index}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    first_is_reply_lost = (digest[0] & 1) == 0
    if slot == 0:
        return "reply_lost" if first_is_reply_lost else "request_lost"
    return "request_lost" if first_is_reply_lost else "reply_lost"
