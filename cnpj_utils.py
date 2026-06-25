"""CNPJ parsing, validation and formatting.

Supports both the legacy 14-digit numeric CNPJ and the alphanumeric format that
Receita Federal begins issuing in July 2026:

    AA.AAA.AAA/AAAA-DV
    - the first 12 positions are alphanumeric (0-9, A-Z uppercase)
    - the last 2 positions (DV) remain numeric, computed with modulo 11

The check-digit maths is the classic CNPJ algorithm with one twist: each of the
12/13 base characters contributes ``ord(ch) - ord('0')`` (so '0'-'9' -> 0-9 and
'A'-'Z' -> 17-42). Because digits map to their own value, the very same routine
validates a legacy all-numeric CNPJ. Reference example that must validate:
``12.ABC.345/01DE-35``.
"""

import re

BASE_LEN = 12
FULL_LEN = 14
_VALID_FULL = re.compile(r"^[0-9A-Z]{12}[0-9]{2}$")
_ALNUM = re.compile(r"[^0-9A-Za-z]")


def _char_value(ch):
    """Map a base character to its numeric weight value (ord - '0')."""
    return ord(ch) - 48          # '0'->0 .. '9'->9 ; 'A'->17 .. 'Z'->42


def _weight(i, n):
    """Weight for position i (0-based, left to right) of an n-length base.
    Rightmost weight is 2, increasing leftwards through 9 then wrapping."""
    return 2 + ((n - 1 - i) % 8)


def _dv_digit(base_chars):
    """One modulo-11 check digit for the given sequence of base characters."""
    n = len(base_chars)
    total = sum(_char_value(ch) * _weight(i, n) for i, ch in enumerate(base_chars))
    rem = total % 11
    return 0 if rem < 2 else 11 - rem


def normalize_cnpj(raw):
    """Strip formatting (dots, slash, hyphen, spaces) and upper-case letters.
    Keeps letters so alphanumeric CNPJs survive. Returns '' if nothing usable."""
    if not raw:
        return ""
    return _ALNUM.sub("", str(raw)).upper()


def is_alphanumeric(cnpj):
    """True if the (normalized) CNPJ uses any letter in its base."""
    c = normalize_cnpj(cnpj)
    return bool(re.search(r"[A-Z]", c[:BASE_LEN]))


def compute_dv(base12):
    """Return the two check-digit characters for a 12-char base, or None."""
    base12 = normalize_cnpj(base12)
    if len(base12) != BASE_LEN or not re.match(r"^[0-9A-Z]{12}$", base12):
        return None
    d1 = _dv_digit(base12)
    d2 = _dv_digit(base12 + str(d1))
    return f"{d1}{d2}"


def is_valid_cnpj(raw):
    """Validate length, character set and both modulo-11 check digits.
    Works for legacy numeric and 2026 alphanumeric CNPJs."""
    c = normalize_cnpj(raw)
    if len(c) != FULL_LEN or not _VALID_FULL.match(c):
        return False
    if len(set(c)) == 1:                 # reject 00000000000000 and the like
        return False
    expected = compute_dv(c[:BASE_LEN])
    return expected is not None and c[BASE_LEN:] == expected


def clean_cnpj(raw):
    """Return the canonical 14-char CNPJ if valid, else None.

    A CNPJ is only returned when it passes full check-digit validation, so
    invalid numbers are rejected before they ever reach the fetch queue."""
    c = normalize_cnpj(raw)
    return c if is_valid_cnpj(c) else None


def format_cnpj(raw):
    """Pretty-print as AA.AAA.AAA/AAAA-DV (works for both formats)."""
    c = normalize_cnpj(raw)
    if len(c) != FULL_LEN:
        return raw or ""
    return f"{c[0:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:14]}"


def root8(raw):
    """The 8-character establishment root (shared by a company's branches)."""
    c = normalize_cnpj(raw)
    return c[:8] if len(c) == FULL_LEN else None
