#!/usr/bin/env python3
"""resolver.py — map a captured trade-post alert to an exact options contract.

One module of the Robinhood copy-trader. Pure parsing plus ONE MCP lookup;
no network, no credentials, no orders. NEVER silently guesses expiry or
strike: anything uncertain returns an Ambiguous.

MCP interface (the real client implements this; code against exactly this):
    mcp.find_option_contracts(underlying: str, expiry: str /*YYYY-MM-DD*/,
                              strike: float, option_type: str /*"call"|"put"*/)
        -> list[dict]  # {"contract_symbol","expiry","strike","option_type"}

Per-trader expiry defaults are supplied explicitly or through the shared safe
configuration at call time. Importing this module never reads user files.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, date
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/Los_Angeles")

# Supported NYSE holiday dates, 2026-2027. Keep in sync with config.py.
# https://www.nyse.com/trade/hours-calendars (verified 2026-09-21).
MARKET_HOLIDAYS = frozenset([
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
])

# Parsing intentionally accepts a small, single-contract grammar. Unrecognized
# numeric material or contradictory intent needs manual review, never guessing.
CONTRACT_RE = re.compile(
    r"\$([A-Z]{1,5})\s+([0-9]+(?:\.[0-9]{1,3})?)\s*(calls?|puts?|[cp])\b", re.IGNORECASE
)
ZERO_DTE_RE = re.compile(r"(?i)\b0\s*dte\b")
MD_EXPIRY_RE = re.compile(
    r"(?i)(?<![\w./])(?:exp(?:iry|iration)?\s*:?\s*)?([0-9]{1,2})/([0-9]{1,2})(?![\w/.])"
)
NUMBER = r"[+-]?(?:[0-9]+\.[0-9]+|\.[0-9]+|[0-9]+)"
PRICE_RE = re.compile(r"(?<![\w.+-])" + NUMBER + r"(?![\w.])")
NOW_PRICE_RE = re.compile(r"(?i)\bnow\s*(?:@\s*|\$\s*)?(" + NUMBER + r")(?![\w.])")
QUANTITY_RE = re.compile(
    r"(?i)\b(?:qty\s*[:=]?\s*[0-9]+|x[0-9]+|[0-9]+\s*(?:contracts?|lots?)|[0-9]+x)(?![\w.])"
)
PERCENT_RE = re.compile(r"(?<![\w.])" + NUMBER + r"\s*%")
NONFINITE_RE = re.compile(r"(?i)\b(?:nan|inf(?:inity)?)\b")
UNSUPPORTED_INTENT_RE = re.compile(
    r"(?i)\b(?:no|not|never|don['’]?t|didn['’]?t|won['’]?t|wouldn['’]?t|"
    r"avoid|cancel(?:led|ed)?|watch(?:ing|list)?|wait(?:ing)?|if|unless|maybe|might|"
    r"sell(?:ing)?|sold|exit(?:ed|ing)?|trim(?:med|ming)?|clos(?:e|ed|ing)|"
    r"short(?:ing)?|roll(?:ed|ing)?|spreads?|stop(?:ped)?|targets?|"
    r"scratch(?:ed)?|missed|hypothetical|example)\b"
)


@dataclass
class ResolvedContract:
    underlying: str          # "SPY"
    expiry: str              # "YYYY-MM-DD"
    strike: float
    option_type: str         # "call" | "put"
    contract_symbol: str     # OCC symbol, e.g. "SPY   260923P00759000"
    premium: float | None    # trader's disclosed entry premium (None if undisclosed)
    inferred_expiry: bool    # True if expiry came from trader default, not post text


@dataclass
class Ambiguous:
    reason: str              # human-readable, e.g. "no explicit expiry and no trader default"
    candidates: list         # candidate contracts (may be empty)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def trading_day(ts_str: str) -> date | None:
    """Market-tz date of ts if it is a trading day, else None.

    Mirrors build_dashboard_data.py: naive (offset-less) or unparseable
    timestamps fail closed; weekends and NYSE holidays are not trading days.
    """
    try:
        dt = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    day = dt.astimezone(MARKET_TZ).date()
    if day.year not in (2026, 2027):
        return None  # Calendar coverage is explicit; do not guess unknown holidays.
    if day.weekday() >= 5 or day in MARKET_HOLIDAYS:
        return None
    return day


def _market_date(ts_str: str) -> date | None:
    """Market-tz date of ts, or None for naive/unparseable timestamps."""
    try:
        dt = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    day = dt.astimezone(MARKET_TZ).date()
    return day if day.year in (2026, 2027) else None


def occ_symbol(underlying: str, expiry: str, strike: float, option_type: str) -> str:
    """Standard 21-char OCC option symbol.

    Format: root (uppercase, space-padded to 6 chars) + YYMMDD + C/P +
    strike x 1000 zero-padded to 8 digits.
    E.g. SPY, 2026-09-23, 759, "put" -> "SPY   260923P00759000".
    The padding is significant: the symbol is exactly 21 characters.
    """
    root = underlying.upper()[:6].ljust(6)
    yymmdd = expiry[2:].replace("-", "")
    cp = "C" if option_type == "call" else "P"
    strike8 = f"{int(round(strike * 1000)):08d}"
    return f"{root}{yymmdd}{cp}{strike8}"


def _side_to_option_type(side: str) -> str:
    s = side.lower()
    return "call" if s.startswith("c") else "put"


def _entry_premium(text: str, side_end: int) -> float | None | Ambiguous:
    """Parse one entry price, after removing explicitly typed non-price fields.

    A number is never taken from a date, DTE, quantity, percentage, or current
    price. Decimal bare prices and explicitly marked integer prices are
    supported; unexplained integers, malformed numbers, and multiple prices
    require manual review.
    """
    tail = text[side_end:]
    if any(char in tail for char in "−–—﹣＋﹢"):
        return Ambiguous("unsupported numeric sign or range", [])
    for pattern in (MD_EXPIRY_RE, ZERO_DTE_RE, NOW_PRICE_RE, QUANTITY_RE, PERCENT_RE):
        tail = pattern.sub(" ", tail)
    if NONFINITE_RE.search(tail):
        return Ambiguous("entry premium must be finite and positive", [])
    matches = list(PRICE_RE.finditer(tail))
    if len(matches) > 1:
        return Ambiguous("multiple possible entry premiums", [])
    residue = PRICE_RE.sub(" ", tail)
    allowed_words = {"entry", "premium", "at", "buy", "buying", "bought", "bto", "in"}
    if any(word.lower() not in allowed_words for word in re.findall(r"[a-zA-Z]+", residue)):
        return Ambiguous("unsupported entry text requires manual review", [])
    if "?" in residue:
        return Ambiguous("question is not an unambiguous trade instruction", [])
    # Reject scientific notation, comma-separated prices, unsupported DTE/date
    # syntax and a numeric suffix/prefix that was only partially recognized.
    if any(c.isnumeric() for c in residue) or re.search(r"[.,/+-]\s*[.,/+-]", residue):
        return Ambiguous("unsupported or malformed numeric field", [])
    if not matches:
        if re.search(r"(?i)(?:@|\$|\b(?:entry|premium)\s*[:=])\s*$", tail):
            return Ambiguous("entry price marker has no price", [])
        return None
    match = matches[0]
    token = match.group(0)
    before = tail[:match.start()]
    after = tail[match.end():]
    # Do not accept a substring of 1,234.56, - 1.5, .5.6, 1/2, or a range.
    if re.search(r"[.,/+-]\s*$", before) or re.match(r"\s*[./+-]", after):
        return Ambiguous("unsupported or malformed entry premium", [])
    if "." not in token and not (
        re.search(r"(?i)(?:@|\$|\b(?:entry|premium|at)\s*[:=]?)\s*$", before)
        or re.match(r"(?i)\s*(?:entry|premium)\b", after)
    ):
        return Ambiguous("unmarked integer could be quantity rather than premium", [])
    value = float(token)
    if not math.isfinite(value) or value <= 0 or token.startswith(("+", "-")):
        return Ambiguous("entry premium must be finite, positive and unsigned", [])
    return value


def _explicit_expiry(text: str, posted_at_iso: str) -> tuple[str, str] | Ambiguous | None:
    """Resolve exactly one explicit expiry; never select the first of several."""
    zeros = list(ZERO_DTE_RE.finditer(text))
    dates = list(MD_EXPIRY_RE.finditer(text))
    if len(zeros) + len(dates) > 1:
        return Ambiguous("multiple expiry indications require manual review", [])
    if zeros:
        tday = trading_day(posted_at_iso)
        if tday is None:
            return Ambiguous("cannot establish trading day for 0DTE", [])
        return tday.isoformat(), "explicit 0DTE"
    if dates:
        m = dates[0]
        post_day = _market_date(posted_at_iso)
        if post_day is None:
            return Ambiguous("cannot establish post year for M/D expiry", [])
        try:
            exp = date(post_day.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return Ambiguous("invalid calendar date in expiry", [])
        if exp < post_day:
            return Ambiguous("expiry is before post date; refusing to guess next year", [])
        if exp.weekday() >= 5 or exp in MARKET_HOLIDAYS:
            return Ambiguous("expiry is not a trading day", [])
        return exp.isoformat(), "explicit M/D"
    return None


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------

def resolve(post_text: str, handle: str, posted_at_iso: str, mcp, *,
            defaults: dict[str, str | None] | None = None) -> ResolvedContract | Ambiguous:
    """Resolve a supported alert and independently verify the returned identity.

    ``defaults`` permits explicit caller-owned trader settings and isolated
    tests. Otherwise the shared configuration is consulted at call time.
    Raw provider errors are never included in the returned reason.
    """
    if not isinstance(post_text, str) or len(post_text) > 10000:
        return Ambiguous("post text must be a bounded string", [])
    if not isinstance(handle, str):
        return Ambiguous("trader handle must be a string", [])
    text = post_text
    if not text.isascii() or any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        return Ambiguous("unsupported post characters require manual review", [])
    if UNSUPPORTED_INTENT_RE.search(text):
        return Ambiguous("unsupported or contradictory trade intent", [])
    contracts = list(CONTRACT_RE.finditer(text))
    if len(contracts) != 1:
        return Ambiguous("expected exactly one ticker/strike/side contract", [])
    m = contracts[0]
    outside = text[:m.start()] + " " + text[m.end():]
    if re.search(r"(?i)\$[a-z]|\b(?:calls?|puts?)\b|[0-9]\s*[cp]\b", outside):
        return Ambiguous("additional contract or side indication", [])
    prefix = text[:m.start()]
    for pattern in (MD_EXPIRY_RE, ZERO_DTE_RE, QUANTITY_RE, PERCENT_RE):
        prefix = pattern.sub(" ", prefix)
    if any(word.lower() not in {"buy", "buying", "bought", "bto", "in", "entry"}
           for word in re.findall(r"[a-zA-Z]+", prefix)) or "?" in prefix:
        return Ambiguous("unsupported entry prefix requires manual review", [])
    if any(c.isnumeric() for c in prefix):
        return Ambiguous("unexplained number before contract", [])
    underlying = m.group(1).upper()
    strike = float(m.group(2))
    if not math.isfinite(strike) or strike <= 0 or strike >= 100000:
        return Ambiguous("strike is not representable as an OCC contract", [])
    option_type = _side_to_option_type(m.group(3))

    inferred_expiry = False
    explicit = _explicit_expiry(text, posted_at_iso)
    if isinstance(explicit, Ambiguous):
        return explicit
    if explicit is not None:
        expiry, _kind = explicit
    else:
        if defaults is None:
            # config has no user-file reads unless explicitly configured.
            from config import default_expiry_for
            default = default_expiry_for(handle)
        else:
            default = {k.lstrip("@").lower(): v for k, v in defaults.items()}.get(
                handle.lstrip("@").lower())
        if default is None:
            return Ambiguous("no explicit expiry and no configured trader default", [])
        if not isinstance(default, str) or default.lower().replace(" ", "") != "0dte":
            return Ambiguous("unsupported trader default_expiry", [])
        tday = trading_day(posted_at_iso)
        if tday is None:
            return Ambiguous("cannot establish trading day for inferred 0DTE", [])
        expiry = tday.isoformat()
        inferred_expiry = True

    premium = _entry_premium(text, m.end())
    if isinstance(premium, Ambiguous):
        return premium
    expected_symbol = occ_symbol(underlying, expiry, strike, option_type)
    try:
        results = mcp.find_option_contracts(underlying, expiry, strike, option_type)
    except Exception:  # Provider messages are untrusted data, not notifications.
        return Ambiguous("contract lookup failed", [])
    if not isinstance(results, list):
        return Ambiguous("contract lookup returned an invalid response", [])

    def identity_matches(contract) -> bool:
        if not isinstance(contract, dict):
            return False
        raw_strike = contract.get("strike")
        if isinstance(raw_strike, bool):
            return False
        try:
            found_strike = float(raw_strike)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(found_strike) or found_strike != strike:
            return False
        if (contract.get("expiry") != expiry
                or contract.get("option_type") != option_type
                or contract.get("contract_symbol") != expected_symbol):
            return False
        for field in ("underlying", "underlying_symbol", "ticker"):
            if field in contract and contract[field] != underlying:
                return False
        # Some providers additionally return 'symbol' as the option symbol.
        if "symbol" in contract and contract["symbol"] not in (underlying, expected_symbol):
            return False
        return True

    exact = [c for c in results if identity_matches(c)]
    if len(exact) != 1:
        return Ambiguous("contract lookup did not return exactly one verified OCC identity", [])
    return ResolvedContract(
        underlying=underlying, expiry=expiry, strike=strike,
        option_type=option_type, contract_symbol=expected_symbol,
        premium=premium, inferred_expiry=inferred_expiry,
    )
