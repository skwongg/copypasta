# Trading Lessons - Running Log

This chat is an ongoing lessons log for investing/trading. Add a new entry whenever we spot something worth remembering.

---

## 2026-09-18 — Quad witching: option prices can decouple from SPY intraday, then snap at EOD

**What happened:** On quad witching Friday (Sep 18, 2026), SPY moved a bunch mid-day but 0DTE contract prices barely reacted. Into EOD, 760Ps swung from ~0.35 to 1.00 in a huge move.

**Lesson:** On witching days, intraday option pricing is not cleanly explained by underlying price action alone. Expect:
- Mid-day pinning / chop around big strikes (dealers hedging massive expiring gamma keep SPY pinned)
- Contract prices looking "stuck" or unresponsive to SPY moves (MMs widen / defend, gamma exposure distorts delta)
- EOD volatility explosion as hedges roll off / expire and pinning breaks — 0DTE can 2-3x in minutes

**What to do differently:**
- Reduce size on 0DTE during quad witching, especially mid-day when price action lies
- Don't assume "SPY moved, my contract should have moved" — on these days it often doesn't until it does all at once
- If trading, favor defined-risk / smaller size, and be extra careful holding into the last 30-60 min — that's when the real move happens
- Next quad witching dates to watch: third Fridays of Mar / Jun / Sep / Dec

**Source:** Silas observation, Sep 18 2026

---

## 2026-09-23 — VIX Wednesday expiry: the mini quad-witching

**What happened:** Wednesday VIX expiry behaved like a smaller quad-witching Friday — pinned, unresponsive intraday option pricing with snap potential. VIX was sitting near ~14.50 (deep complacency) while the 10Y was at 5.00%.

**The mechanics (researched):** VIX derivatives don't expire Friday like equity options — they expire on **Wednesday mornings** (a.m. settlement), on the Wednesday 30 days before the next SPX monthly expiry. With VIX weeklies (VIXW) listed, there's effectively a VIX expiry *every* Wednesday.
- Settlement is a Special Opening Quotation (SOQ) built from **SPX option opening prints** Wednesday morning — not Tuesday's VIX close. The SOQ routinely gaps ±2–5 points from the prior day's close.
- Consequence: anyone holding expiring VIX futures/options must be flat or rolled by **Tuesday's close**. Tuesday-into-Wednesday brings forced closing/rolling flows, and Wednesday's open is mechanically special because dealers hedge around the exact SPX strips that feed the settlement.
- Sources: CBOE VIX weeklies spec (cdn.cboe.com), CFTC filing on VX settlement (cftc.gov), McMillan on VIX settlement gaps (optionstrategist.com)

**Silas's watch rule (heuristic — forward-test, don't treat as proven):**
- If VIX is pinned low near **~14.50** heading into a Wednesday expiry, that Wednesday has the potential to act like a quad-witching day: mid-day pinning, option prices decoupled from SPY moves, then an EOD snap.
- Always log the macro context with it: 10Y at **5.00%** on Sep 23, 2026 — high rates + low VIX = complacency sitting on top of real macro risk. That's the kindling.

**What to do differently:**
- Treat low-VIX Wednesdays as mini-witching days: smaller size on 0DTE, don't trust mid-day price action, respect the last 30–60 min snap risk — same playbook as Lesson 1, lighter weight
- Check the VIX expiry calendar at the start of each week; monthly VIX expiries (bigger flows) deserve more respect than weekly ones
- When logging a Wednesday session, record three numbers: VIX level into the expiry, 10Y yield, and whether the day pinned or snapped — that's how this rule gets validated or killed

**Source:** Silas observation + web research, Sep 23 2026
