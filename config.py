"""Runtime configuration with fail-fast validation."""
import math
import os
import re
import stat
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


_ENV_PATH = Path(__file__).resolve().parent / ".env"
if _ENV_PATH.exists():
    if _ENV_PATH.is_symlink() or not _ENV_PATH.is_file():
        raise PermissionError(".env must be a regular, non-symlink file")
    # POSIX makes private-file permissions directly inspectable.  Windows
    # operators must apply the ACL documented in SECURITY.md.
    if os.name != "nt" and stat.S_IMODE(_ENV_PATH.stat().st_mode) & 0o077:
        raise PermissionError(".env contains live credentials and must have mode 0600")
load_dotenv(_ENV_PATH, override=False, encoding="utf-8")


def _env_text(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _env_float(name: str, default: str) -> float:
    raw = _env_text(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a number") from exc


def _env_int(name: str, default: str | None = None) -> int | None:
    raw = _env_text(name, default)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_bool(name: str, default: bool | None = False) -> bool | None:
    raw = _env_text(name)
    if raw in (None, ""):
        return default
    value = raw.lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be one of 1/0, true/false, yes/no, on/off")

SYMBOL = "BTCUSDT"
# $2.50 is the largest size whose bad-case drawdown fits the paper balance.
# The engine sizes UP to the 5-share venue minimum, so a fill above BET_SIZE/5
# costs more than BET_SIZE - MAX_ROUND_EXPOSURE caps the real cash.
BET_SIZE = _env_float("BET_SIZE", "2.50")
# Phase 2 trades inside the final TRADE_LAST_SECONDS of each round.
TRADE_LAST_SECONDS = _env_int("TRADE_LAST_SECONDS", "120")
if TRADE_LAST_SECONDS is None:
    raise ValueError("TRADE_LAST_SECONDS cannot be empty")
TRADE_INTERVAL_SECONDS = _env_float("TRADE_INTERVAL_SECONDS", "6")
MAX_BUY_PRICE = _env_float("MAX_BUY_PRICE", "0.90")
MIN_BUY_PRICE = _env_float("MIN_BUY_PRICE", "0.20")
BTC_STALE_AFTER = _env_float("BTC_STALE_AFTER", "3.0")
# How long the book we hold may have been in our hands. For a REST read
# this is the request round trip, so it stays near zero.
ORDERBOOK_MAX_AGE_SECONDS = _env_float("ORDERBOOK_MAX_AGE_SECONDS", "8.0")
# How long the venue may have left the book UNCHANGED before we stop
# believing it. This is not freshness: a quiet market legitimately goes
# minutes without a single change, and measuring staleness from the last
# change refused perfectly current books. Measured on btc-updown-5m, gaps
# of 33s between changes are ordinary. The bound exists only to catch a
# venue serving a frozen or cached book.
ORDERBOOK_MAX_QUIET_SECONDS = _env_float("ORDERBOOK_MAX_QUIET_SECONDS", "900.0")
# A book timestamp ahead of our clock by more than this means the clock
# or the timestamp unit is wrong, never a real book.
ORDERBOOK_FUTURE_TOLERANCE_SECONDS = _env_float(
    "ORDERBOOK_FUTURE_TOLERANCE_SECONDS", "5.0")
MAX_ALLOWED_SPREAD = _env_float("MAX_ALLOWED_SPREAD", "0.25")
CLOCK_MAX_DRIFT_SECONDS = _env_float("CLOCK_MAX_DRIFT_SECONDS", "2.0")
PAPER_LATENCY_MS = _env_float("PAPER_LATENCY_MS", "150")
TWAP_STALE_AFTER = _env_float("TWAP_STALE_AFTER", "10.0")
# No order inside the final minute. Measured over 16 fills: 31.2% won against
# a 69.6% break-even, z = -3.29 - and it has a mechanism, not just a p-value.
# In the last minute the book goes one-sided (the winning leg keeps only bids,
# the losing leg only asks), so the fills still available are the ones the
# market is content to sell. That is adverse selection, and no signal fixes it.
# Trading T-120..T-60 was +1.4 per $100 over the same period; the damage was
# entirely in the tail.
MIN_SECONDS_TO_EXPIRY = _env_float("MIN_SECONDS_TO_EXPIRY", "60.0")

# Last-minute loss trim. Off by default. When on, at most two extra FOKs of
# the 0.80-0.88 favorite are allowed between T-START and T-CUTOFF, and only
# when that path is still a hole while the other path is already green.
# START is independent of MIN_SECONDS_TO_EXPIRY so a 0 last-minute floor
# still loads, and so enabling trim does not reopen T-20..T-0.
LATE_TRIM_ENABLED = bool(_env_bool("LATE_TRIM_ENABLED", False))
LATE_TRIM_START_SECONDS = _env_float("LATE_TRIM_START_SECONDS", "60.0")
LATE_TRIM_CUTOFF_SECONDS = _env_float("LATE_TRIM_CUTOFF_SECONDS", "20.0")
LATE_TRIM_ASK_MIN = _env_float("LATE_TRIM_ASK_MIN", "0.80")
LATE_TRIM_ASK_MAX = _env_float("LATE_TRIM_ASK_MAX", "0.88")
LATE_TRIM_MAX_CLIPS = _env_int("LATE_TRIM_MAX_CLIPS", "2") or 2
LATE_TRIM_CLIP_MULT = _env_float("LATE_TRIM_CLIP_MULT", "1")
LATE_TRIM_INTERVAL_SECONDS = _env_float("LATE_TRIM_INTERVAL_SECONDS", "12.0")

# Phase 2 keeps book and Chainlink votes as diagnostics, while fresh Binance
# SIG PRICE is the sole order-side authority.  It remains off by default;
# enable it explicitly to run the experimental path.
PHASE2_ENABLED = bool(_env_bool("PHASE2_ENABLED", False))
# PAPER may deliberately follow a later, verified SIG PRICE reversal even
# after the first outcome token has filled.  LIVE keeps the complement-leg
# block unconditionally: two independent venue orders are not an atomic pair.
# Off by default so existing paper runs preserve their one-leg-per-round risk
# contract unless the experiment is selected explicitly.
PAPER_ALLOW_SIGNAL_FLIPS = bool(_env_bool("PAPER_ALLOW_SIGNAL_FLIPS", False))
if PAPER_ALLOW_SIGNAL_FLIPS and not PHASE2_ENABLED:
    raise ValueError("PAPER_ALLOW_SIGNAL_FLIPS requires PHASE2_ENABLED=1")

# Permit the complement leg in LIVE after a verified signal reversal, the way
# PAPER_ALLOW_SIGNAL_FLIPS does for paper. This is a risk decision, not a bug
# fix: two independent venue orders are not an atomic pair, so a reversal can
# leave the account holding one leg at a price the second leg never matched -
# in PAPER that costs nothing, in LIVE it is real money on an unhedged side.
# Off by default; enabling it is choosing that exposure knowingly.
LIVE_ALLOW_SIGNAL_FLIPS = bool(_env_bool("LIVE_ALLOW_SIGNAL_FLIPS", False))
if LIVE_ALLOW_SIGNAL_FLIPS and not PHASE2_ENABLED:
    raise ValueError("LIVE_ALLOW_SIGNAL_FLIPS requires PHASE2_ENABLED=1")

# Give SIG BOOK and SIG CHAINLINK their own orders instead of leaving them as
# diagnostics. Each non-neutral signal trades its own side, so a round where
# they disagree buys BOTH legs on purpose. Measured on 1,957 logged decisions
# that is 26.1% of them, and a simultaneous pair costs the overround: at the
# 1.0100 sum observed live, ~-$0.22 per $5 pair whichever way BTC settles.
# The complement guard exists to prevent exactly that, so this switch stands
# it down for signal-driven legs and cannot be combined with a lock that would
# refuse them. Off by default; PAPER only.
# How early the NEXT round's books are discovered and pre-subscribed. The
# websocket needs time to subscribe and receive a first snapshot before the
# boundary, or the opening seconds of the new round trade against an empty
# book. Raising this costs one extra gamma-api call per round, no more.
ROUND_PREPARE_LEAD_SECONDS = _env_float("ROUND_PREPARE_LEAD_SECONDS", "30")
if not 5.0 <= ROUND_PREPARE_LEAD_SECONDS <= 280.0:
    raise ValueError("ROUND_PREPARE_LEAD_SECONDS must be between 5 and 280")
# Rotation poll interval away from a boundary. Near one the loop polls every
# second regardless: the opening print may only be latched in the first 5s of
# a round, so a rotation that lands 6s late costs the entire round. Mid-round
# there is nothing to gain and gamma-api rate-limits, hence the slower default.
ROUND_POLL_SECONDS = _env_float("ROUND_POLL_SECONDS", "5")
if not 0.5 <= ROUND_POLL_SECONDS <= 30.0:
    raise ValueError("ROUND_POLL_SECONDS must be between 0.5 and 30")

# Phase 2 normally refuses to trade a round unless all four boundary inputs are
# present: both Binance values and both Chainlink values. But SIG PRICE alone
# owns the order side, and SIG CHAINLINK is either a diagnostic or - under
# PHASE2_MULTI_SIGNAL - a leg of its own that can simply abstain. Requiring its
# inputs to trade cancels rounds that SIG PRICE could have handled: one missed
# one-second TWAP observation kills five minutes of trading. With this on, only
# missing BINANCE inputs cancel the round; Chainlink abstains like SIG BOOK
# already does on a one-sided book. Off by default so the stricter original
# contract is what you get unless the looser one is chosen deliberately.
# Polymarket enables a taker matching delay on these markets (`itode: true` on
# /clob-markets) but the endpoint states only THAT a delay exists, never how
# long. An order submitted without knowing it can match after the round has
# already resolved, so the live path refuses outright by default.
#
# Set this to the delay you are willing to assume, in seconds, and live orders
# are permitted outside that many seconds from the round end - refused inside
# it, where an unknown delay could span resolution. 0 keeps the hard refusal.
#
# Choose generously: if the real delay exceeds this, orders sent just outside
# the window can still match after expiry, which is exactly the failure the
# refusal exists to prevent. It costs only the tail of a 300s round.
ASSUMED_MATCH_DELAY_SECONDS = _env_float("ASSUMED_MATCH_DELAY_SECONDS", "0")
if (not math.isfinite(ASSUMED_MATCH_DELAY_SECONDS)
        or not 0 <= ASSUMED_MATCH_DELAY_SECONDS <= 120):
    raise ValueError("ASSUMED_MATCH_DELAY_SECONDS must be between 0 and 120")

# The opening print is latched from a websocket trade stamped in the first 5
# seconds of the round. A socket mid-reconnect across the boundary never
# receives it and the whole round is lost - measured at about one round in
# five. After this many seconds the bot asks Binance REST for a trade from the
# SAME 5-second interval, which recovers the identical value rather than
# substituting a later price. Give the socket a real chance first; retrying too
# early just spends an API call on a print that was about to arrive.
BOUNDARY_BACKFILL_AFTER = _env_float("BOUNDARY_BACKFILL_AFTER", "15")
if not math.isfinite(BOUNDARY_BACKFILL_AFTER) or not 5 <= BOUNDARY_BACKFILL_AFTER <= 120:
    raise ValueError("BOUNDARY_BACKFILL_AFTER must be between 5 and 120 seconds")

# A round already underway when the bot starts is a degraded round: its
# opening print may only be recoverable from REST, the book has moved, and
# part of its trading window is already gone. With this on the bot observes
# that round without trading it and begins at the next clean boundary.
SKIP_JOINED_ROUND = bool(_env_bool("SKIP_JOINED_ROUND", False))

PHASE2_PARTIAL_SIGNALS = bool(_env_bool("PHASE2_PARTIAL_SIGNALS", False))

# Order side follows the DISSENTING signal when the three disagree, instead of
# SIG PRICE unconditionally. Measured over 275 archived rounds this scored -5.83
# per $100 against -1.90 for SIG PRICE alone, so it is off by default and is
# selected deliberately. Requires PHASE2_MULTI_SIGNAL off: the minority rule
# picks ONE side, and multi-signal exists to buy several.
SIGNAL_MINORITY_RULE = bool(_env_bool("SIGNAL_MINORITY_RULE", False))

PHASE2_MULTI_SIGNAL = bool(_env_bool("PHASE2_MULTI_SIGNAL", False))
if PHASE2_MULTI_SIGNAL and not PHASE2_ENABLED:
    raise ValueError("PHASE2_MULTI_SIGNAL requires PHASE2_ENABLED=1")
if SIGNAL_MINORITY_RULE and PHASE2_MULTI_SIGNAL:
    raise ValueError(
        "SIGNAL_MINORITY_RULE selects a single dissenting side; it cannot be "
        "combined with PHASE2_MULTI_SIGNAL, which buys one leg per signal")
if PHASE2_MULTI_SIGNAL and PAPER_ALLOW_SIGNAL_FLIPS:
    raise ValueError(
        "PHASE2_MULTI_SIGNAL and PAPER_ALLOW_SIGNAL_FLIPS both relax the "
        "complement guard by different rules; enable exactly one")
if PHASE2_MULTI_SIGNAL and LIVE_ALLOW_SIGNAL_FLIPS:
    raise ValueError(
        "PHASE2_MULTI_SIGNAL and LIVE_ALLOW_SIGNAL_FLIPS both relax the "
        "complement guard by different rules; enable exactly one")

# The order path refuses any submission outside the round's execution
# interval. With only phase 2 active, that is exactly TRADE_LAST_SECONDS.
EXECUTION_WINDOW_SECONDS = TRADE_LAST_SECONDS


# Polymarket takes shares * theta * p * (1-p) from a taker. For a fixed
# notional that is at most notional * theta, which is all a cap needs.
TAKER_FEE_RATE = 0.07
VENUE_MIN_SHARES = 5.0

# ---- paired-leg profit lock ------------------------------------------------
# Holding both legs of one market redeems for exactly $1.00 per matched pair at
# settlement, whichever way BTC goes. Buying the complement is therefore worth
# doing only when both entry prices plus both fees come to less than $1.00;
# above that the pair is a guaranteed loss, which is what the complement guard
# normally exists to prevent. Off by default: it deliberately relaxes that
# guard, so it has to be switched on knowingly.
PAIR_LOCK_ENABLED = bool(_env_bool("PAIR_LOCK_ENABLED", False))
# Headroom held back from $1.00. A quote is not a fill: the book can move a
# tick between the check and the FOK, the venue can change theta, and the
# broker rounds up to VENUE_MIN_SHARES. Without a margin, a pair measured at
# exactly break-even settles as a small loss.
PAIR_LOCK_MIN_EDGE = _env_float("PAIR_LOCK_MIN_EDGE", "0.02")
if not math.isfinite(PAIR_LOCK_MIN_EDGE) or not 0.0 <= PAIR_LOCK_MIN_EDGE < 1.0:
    raise ValueError("PAIR_LOCK_MIN_EDGE must be in [0, 1)")


def pair_lock_permits(entry_price, entry_fee_per_share,
                      ask) -> tuple[bool, float]:
    """Would buying the complement at ``ask`` lock a profit on the pair?

    Returns ``(permitted, locked_per_pair)``. The fee already paid on the held
    leg is counted deliberately: this answers "is the finished round position
    profitable", not "is this marginal order cheap". Sunk-cost reasoning would
    let the bot complete a pair that still loses money overall, and the whole
    point of the lock is that the outcome stops depending on BTC.
    """
    try:
        p1 = float(entry_price)
        f1 = float(entry_fee_per_share)
        p2 = float(ask)
    except (TypeError, ValueError):
        return False, 0.0
    if not all(math.isfinite(v) for v in (p1, f1, p2)):
        return False, 0.0
    if not 0.0 < p1 < 1.0 or not 0.0 < p2 < 1.0 or f1 < 0.0:
        return False, 0.0
    f2 = TAKER_FEE_RATE * p2 * (1.0 - p2)
    locked = 1.0 - (p1 + f1 + p2 + f2)
    return locked >= PAIR_LOCK_MIN_EDGE, locked


def entry_cost_ceiling(cap_price: float) -> float:
    """The most one entry can take out of the account at this price cap.

    BUGFIX: main_bot used to charge MAX_ROUND_EXPOSURE exactly BET_SIZE per
    entry. The broker sizes UP to the venue's 5-share minimum, so any fill
    above BET_SIZE/5 costs more than BET_SIZE and the fee is on top. Measured
    on a real paper run the tracker was 22% low overall and 76% low on one
    round, which made the cap nominal rather than real. A limit has to use an
    upper bound, so this returns one.
    """
    notional = max(float(BET_SIZE), VENUE_MIN_SHARES * float(cap_price))
    return notional * (1.0 + TAKER_FEE_RATE)


def _round_entry_budget() -> float:
    """Worst-case CASH for one round.

    Phase 2 stops at MIN_SECONDS_TO_EXPIRY, so its budget is the window it can
    actually reach, not the whole tail of the round. Parking phase 2 leaves a
    floor of one entry so MAX_ROUND_EXPOSURE >= BET_SIZE still holds.
    """
    if not PHASE2_ENABLED:
        return entry_cost_ceiling(MAX_BUY_PRICE)
    entries = math.ceil(
        max(0.0, TRADE_LAST_SECONDS - MIN_SECONDS_TO_EXPIRY)
        / max(TRADE_INTERVAL_SECONDS, 1))
    budget = entries * entry_cost_ceiling(MAX_BUY_PRICE)
    return budget if budget > 0 else entry_cost_ceiling(MAX_BUY_PRICE)


MAX_ROUND_EXPOSURE = _env_float("MAX_ROUND_EXPOSURE", str(_round_entry_budget()))

# ---- stop loss --------------------------------------------------------------
# Sell a held leg once its BID reaches STOP_LOSS_PRICE. This is a client-side
# trigger, not an order type: the CLOB has only FAK/FOK/GTC/GTD, so nothing
# resting on the book can act as a stop. A resting sell fills when price RISES,
# which is a take-profit; a stop has to be watched and then crossed.
#
# Measured over 275 archived rounds: 14.1% of legs that eventually WON traded
# at or below 0.25 first, dipping as low as 0.04 with 143s still to run. A stop
# therefore cuts roughly one winner in seven. On both-leg rounds the exit was
# worth +$392 (t=+3.44) because it was selling a structurally dead second leg;
# on single-leg rounds the same test came out at -0.58. Whether it pays depends
# entirely on the haircut actually paid on the way out, which no archived run
# recorded, so this ships OFF and instrumented.
STOP_LOSS_ENABLED = bool(_env_bool("STOP_LOSS_ENABLED", False))
STOP_LOSS_PRICE = _env_float("STOP_LOSS_PRICE", "0.25")
# The absolute worst price the exit may accept while walking the book down.
# Setting this to the trigger price makes the stop a pure limit that simply
# does not fill in a thin book; lowering it buys certainty of exit with price.
# At a haircut beyond 0.19 the measured benefit inverts, so a floor far below
# the trigger is choosing execution over expectancy - deliberately.
STOP_LOSS_FLOOR_PRICE = _env_float("STOP_LOSS_FLOOR_PRICE", "0.05")
# Do not arm before this many seconds remain. The winners that recovered from
# under 0.25 did so at 93-209s left; a stop armed round-wide cut 13 of them
# against 5 when it only armed inside 120s.
STOP_LOSS_ARM_SECONDS = _env_float("STOP_LOSS_ARM_SECONDS", "120")
# Stop placing exits this close to expiry. The trader studied here stopped
# trading entirely at T-30 and placed nothing in the final 30 seconds.
STOP_LOSS_EXIT_CUTOFF_SECONDS = _env_float("STOP_LOSS_EXIT_CUTOFF_SECONDS", "20")
STOP_LOSS_POLL_SECONDS = _env_float("STOP_LOSS_POLL_SECONDS", "1.0")
if not 0.0 < STOP_LOSS_PRICE < 1.0:
    raise ValueError("STOP_LOSS_PRICE must be strictly between 0 and 1")
if not 0.0 < STOP_LOSS_FLOOR_PRICE <= STOP_LOSS_PRICE:
    raise ValueError(
        "STOP_LOSS_FLOOR_PRICE must be in (0, STOP_LOSS_PRICE]: a floor above "
        "the trigger could never fill")
if not 0.0 <= STOP_LOSS_EXIT_CUTOFF_SECONDS < STOP_LOSS_ARM_SECONDS <= 300.0:
    raise ValueError(
        "need 0 <= STOP_LOSS_EXIT_CUTOFF_SECONDS < STOP_LOSS_ARM_SECONDS <= 300")
if not 0.2 <= STOP_LOSS_POLL_SECONDS <= 30.0:
    raise ValueError("STOP_LOSS_POLL_SECONDS must be between 0.2 and 30")

# ---- take profit ------------------------------------------------------------
# Mirror of the stop loss on the other side: whichever leg's BID reaches
# TAKE_PROFIT_PRICE first is sold. A round settling to $1.00 pays every share
# in full, so selling at 0.98 locks that outcome minus ~2c of headroom for
# late-round reversal risk. Runs on its own task at the same cadence and reuses
# the same _LiveExitBroker as the stop loss - the FAK exit contract is
# identical, only the direction of the trigger inverts.
#
# min_price on the exit equals TAKE_PROFIT_PRICE. This is deliberate: a fill
# below 0.98 defeats the reason for the trigger, so the order silently fills
# nothing if the book moves down between the check and the FAK. Widen with
# TAKE_PROFIT_FLOOR_PRICE only when you want to accept slippage explicitly.
TAKE_PROFIT_ENABLED = bool(_env_bool("TAKE_PROFIT_ENABLED", False))
TAKE_PROFIT_PRICE = _env_float("TAKE_PROFIT_PRICE", "0.98")
TAKE_PROFIT_FLOOR_PRICE = _env_float("TAKE_PROFIT_FLOOR_PRICE",
                                     str(TAKE_PROFIT_PRICE))
TAKE_PROFIT_POLL_SECONDS = _env_float("TAKE_PROFIT_POLL_SECONDS", "1.0")
if not 0.0 < TAKE_PROFIT_PRICE < 1.0:
    raise ValueError("TAKE_PROFIT_PRICE must be strictly between 0 and 1")
if not 0.0 < TAKE_PROFIT_FLOOR_PRICE <= TAKE_PROFIT_PRICE:
    raise ValueError(
        "TAKE_PROFIT_FLOOR_PRICE must be in (0, TAKE_PROFIT_PRICE]: a floor "
        "above the trigger could never fill")
if not 0.2 <= TAKE_PROFIT_POLL_SECONDS <= 30.0:
    raise ValueError("TAKE_PROFIT_POLL_SECONDS must be between 0.2 and 30")
# Stops sell into the losing side (bid falling); TP sells into the winning
# side (bid rising). They cannot both fire on the same leg meaningfully - a
# leg's bid can't simultaneously be below STOP_LOSS_PRICE and above
# TAKE_PROFIT_PRICE - so the guard here is only that the operator did not
# accidentally invert the two.
if (STOP_LOSS_ENABLED and TAKE_PROFIT_ENABLED
        and STOP_LOSS_PRICE >= TAKE_PROFIT_PRICE):
    raise ValueError(
        "STOP_LOSS_PRICE must stay below TAKE_PROFIT_PRICE when both are on")

# ---- cheap hedge (reversal insurance on a large position) ------------------
# Buys the UNDERDOG (0.10-0.15 band) ONCE per round when the held side has
# accumulated at least MIN_HELD_COST. Sizes the hedge so, if the round
# reverses and the underdog wins, total round loss is capped near LOSS_CAP.
# Never insures a small position: on <MIN_HELD_COST the premium eats too
# much of the win.
#
# Independent of LATE_TRIM. That module fires later (T-60..T-20), buys the
# FAVORITE (0.80-0.88), closes a hole that already exists. This module fires
# earlier (T-180..T-60), buys the UNDERDOG (0.10-0.15), pays a small premium
# for a large payout on reversal.
CHEAP_HEDGE_ENABLED = bool(_env_bool("CHEAP_HEDGE_ENABLED", False))
CHEAP_HEDGE_MIN_HELD_COST = _env_float("CHEAP_HEDGE_MIN_HELD_COST", "15.0")
CHEAP_HEDGE_ASK_MIN = _env_float("CHEAP_HEDGE_ASK_MIN", "0.10")
CHEAP_HEDGE_ASK_MAX = _env_float("CHEAP_HEDGE_ASK_MAX", "0.15")
CHEAP_HEDGE_START_SECONDS = _env_float("CHEAP_HEDGE_START_SECONDS", "180")
CHEAP_HEDGE_CUTOFF_SECONDS = _env_float("CHEAP_HEDGE_CUTOFF_SECONDS", "60")
CHEAP_HEDGE_LOSS_CAP = _env_float("CHEAP_HEDGE_LOSS_CAP", "10.0")
CHEAP_HEDGE_MAX_HEDGE_COST = _env_float("CHEAP_HEDGE_MAX_HEDGE_COST", "3.5")
CHEAP_HEDGE_REQUIRE_STRONG_SIGNAL = bool(_env_bool(
    "CHEAP_HEDGE_REQUIRE_STRONG_SIGNAL", True))
CHEAP_HEDGE_POLL_SECONDS = _env_float("CHEAP_HEDGE_POLL_SECONDS", "1.0")

if not (math.isfinite(CHEAP_HEDGE_ASK_MIN)
        and math.isfinite(CHEAP_HEDGE_ASK_MAX)
        and 0.0 < CHEAP_HEDGE_ASK_MIN < CHEAP_HEDGE_ASK_MAX < 1.0):
    raise ValueError(
        "CHEAP_HEDGE_ASK_MIN must be below CHEAP_HEDGE_ASK_MAX in (0, 1)")
if not (math.isfinite(CHEAP_HEDGE_START_SECONDS)
        and math.isfinite(CHEAP_HEDGE_CUTOFF_SECONDS)
        and 0.0 <= CHEAP_HEDGE_CUTOFF_SECONDS < CHEAP_HEDGE_START_SECONDS
        and CHEAP_HEDGE_START_SECONDS <= 300.0):
    raise ValueError(
        "need 0 <= CHEAP_HEDGE_CUTOFF_SECONDS < "
        "CHEAP_HEDGE_START_SECONDS <= 300")
if (not math.isfinite(CHEAP_HEDGE_MIN_HELD_COST)
        or CHEAP_HEDGE_MIN_HELD_COST < 0.0):
    raise ValueError("CHEAP_HEDGE_MIN_HELD_COST must be non-negative")
if not math.isfinite(CHEAP_HEDGE_LOSS_CAP) or CHEAP_HEDGE_LOSS_CAP < 0.0:
    raise ValueError("CHEAP_HEDGE_LOSS_CAP must be non-negative")
if (not math.isfinite(CHEAP_HEDGE_MAX_HEDGE_COST)
        or CHEAP_HEDGE_MAX_HEDGE_COST <= 0.0):
    raise ValueError("CHEAP_HEDGE_MAX_HEDGE_COST must be positive")
if not 0.2 <= CHEAP_HEDGE_POLL_SECONDS <= 30.0:
    raise ValueError("CHEAP_HEDGE_POLL_SECONDS must be between 0.2 and 30")
# The hedge buy caps its own limit at CHEAP_HEDGE_ASK_MAX, and the entry
# path elsewhere caps at MAX_BUY_PRICE. Both are enforced on the venue side,
# so a max above MAX_BUY_PRICE would silently be re-capped - warn instead.
if CHEAP_HEDGE_ENABLED and CHEAP_HEDGE_ASK_MAX > MAX_BUY_PRICE:
    raise ValueError(
        "CHEAP_HEDGE_ASK_MAX cannot exceed MAX_BUY_PRICE; raise "
        "MAX_BUY_PRICE or lower CHEAP_HEDGE_ASK_MAX before enabling")

CANCEL_OPEN_BEFORE_TRADE = bool(_env_bool("CANCEL_OPEN_BEFORE_TRADE", False))
ALLOW_GLOBAL_CANCEL_ALL = bool(_env_bool("ALLOW_GLOBAL_CANCEL_ALL", False))
ALLOW_CUSTOM_CLOB_HOST = bool(_env_bool("ALLOW_CUSTOM_CLOB_HOST", False))
CLOB_HOST = _env_text("CLOB_HOST", "https://clob.polymarket.com")
CHAIN_ID = 137
_VALID_TICKS = ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001")
TICK_SIZE = _env_text("TICK_SIZE") or None
if TICK_SIZE is not None and TICK_SIZE not in _VALID_TICKS:
    raise ValueError(f"TICK_SIZE must be one of {_VALID_TICKS}, got {TICK_SIZE!r}")
NEG_RISK = _env_bool("NEG_RISK", None)
UP_TOKEN_ID = _env_text("UP_TOKEN_ID") or None
DOWN_TOKEN_ID = _env_text("DOWN_TOKEN_ID") or None
ORDERBOOK_TOKEN_ID = _env_text("ORDERBOOK_TOKEN_ID") or None
POLY_FUNDER = _env_text("POLY_FUNDER") or None
POLY_SIGNATURE_TYPE = _env_int("POLY_SIGNATURE_TYPE")


def _finite_positive(name, value):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number, got {value!r}")


_finite_positive("BET_SIZE", BET_SIZE)
if Decimal(str(BET_SIZE)).quantize(Decimal("0.01")) != Decimal(str(BET_SIZE)):
    raise ValueError("BET_SIZE must be expressed in whole pUSD cents")
if not 1 <= TRADE_LAST_SECONDS <= 300:
    raise ValueError("TRADE_LAST_SECONDS must be between 1 and 300")
if (not math.isfinite(TRADE_INTERVAL_SECONDS)
        or not 1 <= TRADE_INTERVAL_SECONDS <= TRADE_LAST_SECONDS):
    raise ValueError("TRADE_INTERVAL_SECONDS must be in [1, TRADE_LAST_SECONDS]")
if not math.isfinite(MAX_BUY_PRICE) or not 0 < MAX_BUY_PRICE < 1:
    raise ValueError("MAX_BUY_PRICE must be strictly between 0 and 1")
if not math.isfinite(MIN_BUY_PRICE) or not 0 < MIN_BUY_PRICE < 1:
    raise ValueError("MIN_BUY_PRICE must be strictly between 0 and 1")
if not MIN_BUY_PRICE < MAX_BUY_PRICE:
    raise ValueError("MIN_BUY_PRICE must be below MAX_BUY_PRICE")
_finite_positive("BTC_STALE_AFTER", BTC_STALE_AFTER)
_finite_positive("ORDERBOOK_MAX_AGE_SECONDS", ORDERBOOK_MAX_AGE_SECONDS)
_finite_positive("ORDERBOOK_MAX_QUIET_SECONDS", ORDERBOOK_MAX_QUIET_SECONDS)
_finite_positive("ORDERBOOK_FUTURE_TOLERANCE_SECONDS",
                 ORDERBOOK_FUTURE_TOLERANCE_SECONDS)
if ORDERBOOK_FUTURE_TOLERANCE_SECONDS < CLOCK_MAX_DRIFT_SECONDS:
    raise ValueError(
        "ORDERBOOK_FUTURE_TOLERANCE_SECONDS must be >= "
        "CLOCK_MAX_DRIFT_SECONDS, or a clock inside its allowed drift "
        "would still refuse every book as future-dated")
if not math.isfinite(MAX_ALLOWED_SPREAD) or not 0 < MAX_ALLOWED_SPREAD <= 1:
    raise ValueError("MAX_ALLOWED_SPREAD must be in (0, 1]")
_finite_positive("CLOCK_MAX_DRIFT_SECONDS", CLOCK_MAX_DRIFT_SECONDS)
if not math.isfinite(PAPER_LATENCY_MS) or PAPER_LATENCY_MS < 0:
    raise ValueError("PAPER_LATENCY_MS must be finite and non-negative")
_finite_positive("TWAP_STALE_AFTER", TWAP_STALE_AFTER)
if (not math.isfinite(MIN_SECONDS_TO_EXPIRY)
        or not 0 <= MIN_SECONDS_TO_EXPIRY < TRADE_LAST_SECONDS):
    raise ValueError("MIN_SECONDS_TO_EXPIRY must be non-negative and below TRADE_LAST_SECONDS")
if LATE_TRIM_MAX_CLIPS not in (1, 2):
    raise ValueError("LATE_TRIM_MAX_CLIPS must be 1 or 2")
if not math.isfinite(LATE_TRIM_CLIP_MULT) or not 1.0 <= LATE_TRIM_CLIP_MULT <= 2.0:
    raise ValueError("LATE_TRIM_CLIP_MULT must be in [1, 2]")
if (not math.isfinite(LATE_TRIM_INTERVAL_SECONDS)
        or not 5.0 <= LATE_TRIM_INTERVAL_SECONDS <= 30.0):
    raise ValueError("LATE_TRIM_INTERVAL_SECONDS must be between 5 and 30")
if (not math.isfinite(LATE_TRIM_CUTOFF_SECONDS)
        or not math.isfinite(LATE_TRIM_START_SECONDS)
        or not 0.0 <= LATE_TRIM_CUTOFF_SECONDS < LATE_TRIM_START_SECONDS
        or not LATE_TRIM_START_SECONDS < TRADE_LAST_SECONDS):
    raise ValueError(
        "LATE_TRIM_CUTOFF_SECONDS must be in [0, LATE_TRIM_START_SECONDS) "
        "and LATE_TRIM_START_SECONDS must be below TRADE_LAST_SECONDS")
if (not math.isfinite(LATE_TRIM_ASK_MIN) or not math.isfinite(LATE_TRIM_ASK_MAX)
        or not 0.0 < LATE_TRIM_ASK_MIN < LATE_TRIM_ASK_MAX < 1.0):
    raise ValueError("LATE_TRIM_ASK_MIN must be below LATE_TRIM_ASK_MAX in (0, 1)")
# The 0.80-0.88 band is the product default. Operators may run a tighter
# MAX_BUY_PRICE while trim is off; only fail when the flag would actually
# submit an order the account ceiling cannot cover.
if LATE_TRIM_ENABLED:
    if LATE_TRIM_ASK_MAX - 1e-12 > MAX_BUY_PRICE:
        raise ValueError(
            "LATE_TRIM_ASK_MAX cannot exceed MAX_BUY_PRICE; raise MAX_BUY_PRICE "
            "or lower LATE_TRIM_ASK_MAX before enabling late trim")
    if LATE_TRIM_ASK_MIN + 1e-12 < MIN_BUY_PRICE:
        raise ValueError("LATE_TRIM_ASK_MIN cannot be below MIN_BUY_PRICE")
if not math.isfinite(MAX_ROUND_EXPOSURE) or MAX_ROUND_EXPOSURE < BET_SIZE:
    raise ValueError("MAX_ROUND_EXPOSURE must be finite and at least BET_SIZE")
if POLY_SIGNATURE_TYPE not in (None, 0, 1, 2, 3):
    raise ValueError("POLY_SIGNATURE_TYPE must be 0, 1, 2, or 3")
# py-clob-client-v2's L1 API-key derivation does not currently bind a type-3
# (POLY_1271 deposit-wallet) key to the funder. Upstream issue #70 remains
# open; allowing it here produces orders that the CLOB rejects as the wrong
# signer. Fail before any credential or order request instead.
if POLY_SIGNATURE_TYPE == 3:
    raise ValueError(
        "POLY_SIGNATURE_TYPE=3 is blocked: py-clob-client-v2 cannot currently "
        "derive a funder-bound POLY_1271 API key (upstream issue #70)"
    )
if POLY_SIGNATURE_TYPE in (1, 2, 3) and not POLY_FUNDER:
    raise ValueError("proxy signature types 1/2/3 require POLY_FUNDER")
if POLY_FUNDER and POLY_SIGNATURE_TYPE is None:
    raise ValueError("POLY_FUNDER requires an explicit POLY_SIGNATURE_TYPE")
if POLY_FUNDER and not re.fullmatch(r"0x[0-9A-Fa-f]{40}", POLY_FUNDER):
    raise ValueError("POLY_FUNDER must be a 20-byte 0x-prefixed Ethereum address")
for _name, _token in (
        ("UP_TOKEN_ID", UP_TOKEN_ID),
        ("DOWN_TOKEN_ID", DOWN_TOKEN_ID),
        ("ORDERBOOK_TOKEN_ID", ORDERBOOK_TOKEN_ID)):
    if _token is not None and (not re.fullmatch(r"[0-9]{1,78}", _token)
                               or int(_token) <= 0):
        raise ValueError(f"{_name} must be a positive decimal uint256 token id")
if CANCEL_OPEN_BEFORE_TRADE and not ALLOW_GLOBAL_CANCEL_ALL:
    raise ValueError(
        "CANCEL_OPEN_BEFORE_TRADE uses the wallet-wide cancel-all endpoint; "
        "set ALLOW_GLOBAL_CANCEL_ALL=1 only for a dedicated bot wallet"
    )
try:
    _clob_url = urlsplit(CLOB_HOST)
    _clob_port = _clob_url.port
except (TypeError, ValueError) as exc:
    raise ValueError("CLOB_HOST must be a valid HTTPS origin") from exc
if (_clob_url.scheme.lower() != "https" or not _clob_url.hostname
        or _clob_url.username is not None or _clob_url.password is not None
        or _clob_url.query or _clob_url.fragment
        or _clob_url.path not in ("", "/")):
    raise ValueError(
        "CLOB_HOST must be an HTTPS origin without credentials, path, query, or fragment"
    )
_official_clob = (
    _clob_url.hostname.lower() == "clob.polymarket.com"
    and _clob_port in (None, 443)
)
if not _official_clob and not ALLOW_CUSTOM_CLOB_HOST:
    raise ValueError(
        "custom CLOB_HOST is blocked because it receives authenticated requests; "
        "set ALLOW_CUSTOM_CLOB_HOST=1 only for an endpoint you control"
    )
# Store an origin without a trailing slash so every SDK endpoint is joined in a
# single, predictable way.
CLOB_HOST = f"https://{_clob_url.hostname}"
if _clob_port not in (None, 443):
    CLOB_HOST += f":{_clob_port}"
