#!/usr/bin/env python3
"""resolver.py — map a captured trade-post alert to an exact options contract.

One module of the Robinhood copy-trader. Pure parsing plus ONE MCP lookup;
no network, no credentials, no orders. NEVER silently guesses expiry or
strike: anything uncertain returns an Ambiguous.

MCP interface (the real client implements this; code against exactly this):
    mcp.find_option_contracts(underlying: str, expiry: str /*YYYY-MM-DD*/,
                              strike: float, option_type: str /*"call"|"put"*/)
        -> list[dict]  # {"contract_symbol","expiry","strike","option_type"}

Per-trader expiry defaults come from ~/workspace/trade-watch/trader_config.json
(CassyTrades -> "0DTE", clintoptions -> null, capricekayem -> null).
Trading-day / holiday logic mirrors build_dashboard_data.py (not imported).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, date
from zoneinfo import ZoneInfo

CONFIG_PATH = os.path.expanduser("~/workspace/trade-watch/trader_config.json")
MARKET_TZ = ZoneInfo("America/Los_Angeles")

# NYSE holidays (options don't trade); 2026-2027. Keep in sync with
# build_dashboard_data.py.
MARKET_HOLIDAYS = frozenset([
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 4, 2),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
])

# $TICKER STRIKE SIDE, e.g. "$SPY 759 PUTS", "$QQQ 734c". Side may be a word
# (calls/puts) or a single letter glued to the strike (c/p), case-insensitive.
CONTRACT_RE = re.compile(
    r"\$([A-Z]{1,5})\s+(\d+(?:\.\d+)?)\s*(calls?|puts?|[cp])\b", re.IGNORECASE
)
ZERO_DTE_RE = re.compile(r"(?i)\b0\s*dte\b")
# Explicit M/D expiry, optionally with "exp" prefix: "9/18", "09/18", "exp 9/18".
MD_EXPIRY_RE = re.compile(r"(?i)(?:\bexp\w*\s*)?(\d{1,2})/(\d{1,2})\b")
# Current-price clause, e.g. "now @ 2.20" — this is NOT the entry premium.
NOW_PRICE_RE = re.compile(r"(?i)\bnow\s*(?:@\s*)?\d+(?:\.\d+)?")
BARE_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


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

def _load_trader_defaults() -> dict[str, str | None]:
    """handle(lowercased) -> default_expiry ("0DTE" / None). Missing file -> {}."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return {
        k.lower(): (v or {}).get("default_expiry")
        for k, v in cfg.items()
        if not k.startswith("_") and isinstance(v, dict)
    }


_TRADER_DEFAULTS: dict[str, str | None] | None = None


def trader_defaults() -> dict[str, str | None]:
    global _TRADER_DEFAULTS
    if _TRADER_DEFAULTS is None:
        _TRADER_DEFAULTS = _load_trader_defaults()
    return _TRADER_DEFAULTS


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
    return dt.astimezone(MARKET_TZ).date()


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


def _entry_premium(text: str, side_end: int) -> float | None:
    """Trader's stated entry premium: the first price token after the side word.

    "now @ 2.20" clauses describe the CURRENT price, not the entry, so they
    are stripped before scanning. No price token -> None (never proxied).
    """
    tail = NOW_PRICE_RE.sub("", text[side_end:])
    m = BARE_NUM_RE.search(tail)
    return float(m.group(0)) if m else None


def _explicit_expiry(text: str, posted_at_iso: str) -> tuple[str, str] | Ambiguous:
    """Return (expiry "YYYY-MM-DD", kind) for an explicit expiry in the text,
    else an Ambiguous if an explicit pattern is present but unusable."""
    if ZERO_DTE_RE.search(text):
        tday = trading_day(posted_at_iso)
        if tday is None:
            return Ambiguous(
                reason="cannot establish trading day for 0DTE "
                       "(posted_at missing, naive, unparseable, or not a trading day)",
                candidates=[],
            )
        return tday.isoformat(), "explicit 0DTE"
    m = MD_EXPIRY_RE.search(text)
    if m:
        post_day = _market_date(posted_at_iso)
        if post_day is None:
            return Ambiguous(
                reason="cannot establish post year for M/D expiry "
                       "(posted_at missing, naive, or unparseable)",
                candidates=[],
            )
        month, day = int(m.group(1)), int(m.group(2))
        try:
            exp = date(post_day.year, month, day)
        except ValueError:
            return Ambiguous(reason=f"invalid calendar date in expiry: {m.group(0)}",
                             candidates=[])
        if exp < post_day:
            # Day-trading context: a past M/D is almost certainly this year's
            # date; never guess next year.
            return Ambiguous(reason=f"expiry {exp.isoformat()} is before the post date; "
                                    "refusing to guess next year",
                             candidates=[])
        if exp.weekday() >= 5 or exp in MARKET_HOLIDAYS:
            return Ambiguous(reason=f"expiry {exp.isoformat()} is not a trading day",
                             candidates=[])
        return exp.isoformat(), f"explicit {m.group(0)}"
    return None  # no explicit expiry pattern at all


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------

def resolve(post_text: str, handle: str, posted_at_iso: str, mcp) -> ResolvedContract | Ambiguous:
    """Resolve a trade-post alert to one exact options contract."""
    text = post_text or ""

    m = CONTRACT_RE.search(text)
    if m is None:
        return Ambiguous(
            reason="could not parse ticker/strike/side from post text "
                   '(expected like "$SPY 759 PUTS" or "$QQQ 734c")',
            candidates=[],
        )
    underlying = m.group(1).upper()
    strike = float(m.group(2))
    option_type = _side_to_option_type(m.group(3))

    # --- expiry: explicit beats trader default; never guess ---
    inferred_expiry = False
    explicit = _explicit_expiry(text, posted_at_iso)
    if isinstance(explicit, Ambiguous):
        return explicit
    if explicit is not None:
        expiry, _kind = explicit
    else:
        default = trader_defaults().get((handle or "").lower())
        if default is None:
            return Ambiguous(
                reason=f"no explicit expiry in post and no default_expiry "
                       f"for trader {handle!r}",
                candidates=[],
            )
        if default.lower().replace(" ", "") != "0dte":
            return Ambiguous(
                reason=f"unsupported default_expiry {default!r} for trader {handle!r}",
                candidates=[],
            )
        tday = trading_day(posted_at_iso)
        if tday is None:
            return Ambiguous(
                reason="cannot establish trading day for inferred 0DTE "
                       "(posted_at missing, naive, unparseable, or not a trading day)",
                candidates=[],
            )
        expiry = tday.isoformat()
        inferred_expiry = True

    premium = _entry_premium(text, m.end())

    # --- resolution: exactly one exact match, or Ambiguous ---
    try:
        results = mcp.find_option_contracts(underlying, expiry, strike, option_type) or []
    except Exception as e:  # noqa: BLE001 - MCP failure must not resolve silently
        return Ambiguous(reason=f"contract lookup failed: {e}", candidates=[])

    def _strike_eq(c) -> bool:
        try:
            return float(c.get("strike")) == strike
        except (TypeError, ValueError):
            return False

    exact = [
        c for c in results
        if isinstance(c, dict)
        and c.get("expiry") == expiry
        and _strike_eq(c)
        and c.get("option_type") == option_type
    ]
    if len(exact) == 1:
        c = exact[0]
        symbol = c.get("contract_symbol") or occ_symbol(underlying, expiry, strike, option_type)
        return ResolvedContract(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            contract_symbol=symbol,
            premium=premium,
            inferred_expiry=inferred_expiry,
        )
    if not exact:
        return Ambiguous(reason="no contract matched (expiry, strike, option_type)",
                         candidates=list(results))
    return Ambiguous(reason=f"{len(exact)} contracts matched (expiry, strike, option_type); "
                            "refusing to pick closest",
                     candidates=exact)


# --------------------------------------------------------------------------
# self-test (fake MCP, no network)
# --------------------------------------------------------------------------

def _one_contract_mcp(underlying, expiry, strike, option_type):
    """Fake MCP: returns exactly the requested contract (with OCC symbol)."""
    cp = "C" if option_type == "call" else "P"
    return [{
        "contract_symbol": occ_symbol(underlying, expiry, strike, option_type),
        "expiry": expiry,
        "strike": strike,
        "option_type": option_type,
    }]


class _FakeMCP:
    def __init__(self, fn):
        self._fn = fn
        self.calls = []

    def find_option_contracts(self, underlying, expiry, strike, option_type):
        self.calls.append((underlying, expiry, strike, option_type))
        return self._fn(underlying, expiry, strike, option_type)


def _run_self_tests() -> bool:
    WED = "2026-09-23T08:00:00-07:00"   # Wednesday, a trading day
    SAT = "2026-09-26T08:00:00-07:00"   # Saturday
    cases = []
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        cases.append((name, status, detail))
        print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))

    # 1. CassyTrades "$SPY 759 PUTS 1.85" on a Wednesday -> 0DTE inferred, premium 1.85
    r = resolve("$SPY 759 PUTS 1.85", "CassyTrades", WED, _FakeMCP(_one_contract_mcp))
    check("cassy 0DTE inferred", isinstance(r, ResolvedContract)
          and r.expiry == "2026-09-23" and r.strike == 759.0
          and r.option_type == "put" and r.premium == 1.85
          and r.inferred_expiry is True
          and r.contract_symbol == "SPY   260923P00759000"
          and len(r.contract_symbol) == 21,
          repr(r))

    # 2. Clint "$QQQ 734c 0.56 0DTE" -> explicit 0DTE, trailing-c side, premium 0.56
    r = resolve("$QQQ 734c 0.56 0DTE", "clintoptions", WED, _FakeMCP(_one_contract_mcp))
    check("clint explicit 0DTE", isinstance(r, ResolvedContract)
          and r.expiry == "2026-09-23" and r.strike == 734.0
          and r.option_type == "call" and r.premium == 0.56
          and r.inferred_expiry is False
          and r.contract_symbol == "QQQ   260923C00734000",
          repr(r))

    # 3. "now @ X" is the current price, not the entry premium
    r = resolve("$SPY 759 PUTS 1.85 now @ 2.20", "CassyTrades", WED,
               _FakeMCP(_one_contract_mcp))
    check("now @ not entry premium", isinstance(r, ResolvedContract) and r.premium == 1.85,
          repr(r))

    # 3b. only a "now @ X" price -> no entry premium disclosed
    r = resolve("$SPY 759 PUTS now @ 2.20", "CassyTrades", WED, _FakeMCP(_one_contract_mcp))
    check("now @ only -> premium None", isinstance(r, ResolvedContract) and r.premium is None,
          repr(r))

    # 4. unpriced post -> premium None (still resolves)
    r = resolve("$SPY 759 PUTS", "CassyTrades", WED, _FakeMCP(_one_contract_mcp))
    check("unpriced -> premium None", isinstance(r, ResolvedContract) and r.premium is None,
          repr(r))

    # 5. weekend post -> Ambiguous (0DTE needs a trading day)
    r = resolve("$SPY 759 PUTS 1.85 0DTE", "CassyTrades", SAT, _FakeMCP(_one_contract_mcp))
    check("weekend post -> Ambiguous", isinstance(r, Ambiguous), repr(r))

    # 6. multiple candidates -> Ambiguous with candidates listed
    def two(underlying, expiry, strike, option_type):
        return [
            {"contract_symbol": occ_symbol(underlying, expiry, strike, option_type) + "A",
             "expiry": expiry, "strike": strike, "option_type": option_type},
            {"contract_symbol": occ_symbol(underlying, expiry, strike, option_type) + "B",
             "expiry": expiry, "strike": strike, "option_type": option_type},
        ]
    r = resolve("$SPY 759 PUTS 1.85 0DTE", "clintoptions", WED, _FakeMCP(two))
    check("2 candidates -> Ambiguous", isinstance(r, Ambiguous) and len(r.candidates) == 2,
          repr(r))

    # 6b. zero candidates -> Ambiguous
    r = resolve("$SPY 759 PUTS 1.85 0DTE", "clintoptions", WED,
               _FakeMCP(lambda *a: []))
    check("0 candidates -> Ambiguous", isinstance(r, Ambiguous), repr(r))

    # 7. capricekayem has no default and no explicit expiry -> Ambiguous
    r = resolve("$SPY 759 PUTS 1.85", "capricekayem", WED, _FakeMCP(_one_contract_mcp))
    check("capricekayem no-expiry -> Ambiguous",
          isinstance(r, Ambiguous) and "no explicit expiry" in r.reason, repr(r))

    # 8. past M/D expiry -> Ambiguous (never guess next year)
    r = resolve("$SPY 759 PUTS 1.85 exp 9/18", "clintoptions", WED,
               _FakeMCP(_one_contract_mcp))
    check("past M/D -> Ambiguous", isinstance(r, Ambiguous), repr(r))

    # 8b. future M/D expiry -> resolves explicitly
    r = resolve("$SPY 759 PUTS 1.85 exp 9/25", "clintoptions", WED,
               _FakeMCP(_one_contract_mcp))
    check("future M/D explicit", isinstance(r, ResolvedContract)
          and r.expiry == "2026-09-25" and r.inferred_expiry is False, repr(r))

    # 9. naive posted_at -> Ambiguous (fail closed)
    r = resolve("$SPY 759 PUTS 1.85", "CassyTrades", "2026-09-23T08:00:00",
               _FakeMCP(_one_contract_mcp))
    check("naive posted_at -> Ambiguous", isinstance(r, Ambiguous), repr(r))

    # 9b. missing posted_at -> Ambiguous
    r = resolve("$SPY 759 PUTS 1.85", "CassyTrades", "", _FakeMCP(_one_contract_mcp))
    check("missing posted_at -> Ambiguous", isinstance(r, Ambiguous), repr(r))

    # 10. unparsable contract (no side) -> Ambiguous, never guess side
    r = resolve("$SPY 759 1.85", "CassyTrades", WED, _FakeMCP(_one_contract_mcp))
    check("no side -> Ambiguous", isinstance(r, Ambiguous), repr(r))

    # 11. MCP lookup error -> Ambiguous, not a crash
    def boom(*a):
        raise RuntimeError("mcp down")
    r = resolve("$SPY 759 PUTS 1.85", "CassyTrades", WED, _FakeMCP(boom))
    check("mcp error -> Ambiguous", isinstance(r, Ambiguous), repr(r))

    print(f"\n{sum(1 for _, s, _ in cases if s == 'PASS')}/{len(cases)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if _run_self_tests() else 1)
