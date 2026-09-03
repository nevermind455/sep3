#!/usr/bin/env python3
"""Record what every signal said and what the market charged - without trading.

    python signal_journal.py record            # append samples, runs until Ctrl+C
    python signal_journal.py resolve           # fill in winners for closed rounds
    python signal_journal.py analyze           # accuracy and edge per signal

Why this exists
---------------
A signal can be measured without paying to act on it. Every round publishes the
Chainlink 60s TWAP, Binance spot, and both legs' order books; 50-odd minutes
later Polymarket publishes the winner. That is a complete observation of "what
did the signal say, what did the market charge, who won" at zero fees and zero
risk, for every round, whether or not a trade happens.

Trading to learn the same thing costs `0.07 x (1 - price)` of stake per fill
and samples perhaps eight times an hour. This samples every round, on every
signal at once, for nothing.

Nothing here places, prices, or influences an order. It opens its own feeds and
writes its own file, so it is safe to run beside a live bot.

The recorded columns are raw observations, never derived sides: rules are
applied in `analyze`, so a new rule can be tested against history already
collected instead of needing a fresh run.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

import market_discovery                      # noqa: E402
import orderbook                             # noqa: E402
import timer                                 # noqa: E402
from chainlink_strike import ChainlinkStrike  # noqa: E402

JOURNAL = ROOT / "signal_journal.csv"
WINNERS = ROOT / "signal_journal_winners.json"
BINANCE_SPOT = "https://api.binance.com/api/v3/ticker/price"
# A sample may only claim to be a round's strike if it landed this
# close to the boundary.
BOUNDARY_GRACE = 5.0

FIELDS = ["wall", "window", "secs_left", "cl_strike", "cl_now",
          "bn_strike", "bn_now", "up_ask", "up_bid", "dn_ask", "dn_bid",
          "up_bid_vol", "up_ask_vol"]


# --------------------------------------------------------------- recording ---
def _binance_spot(session) -> float | None:
    try:
        r = session.get(BINANCE_SPOT, params={"symbol": "BTCUSDT"}, timeout=6)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def _book(token) -> tuple[float | None, float | None, float, float]:
    """Best ask, best bid, and total volume each side. None when absent."""
    try:
        bids, asks = orderbook.get_orderbook(token)
    except Exception:
        return None, None, 0.0, 0.0
    ask = float(asks[0]["price"]) if asks else None
    bid = float(bids[0]["price"]) if bids else None
    bid_vol = sum(float(x["size"]) for x in (bids or ()))
    ask_vol = sum(float(x["size"]) for x in (asks or ()))
    return ask, bid, bid_vol, ask_vol


async def record(interval: float) -> int:
    import requests

    session = requests.Session()
    strike = ChainlinkStrike()
    strike.start()
    print(f"[JOURNAL] recording every {interval:.0f}s into {JOURNAL.name}")
    print("[JOURNAL] the first round is skipped: a boundary TWAP needs a "
          "connection that predates the boundary")

    bn_strike: dict[int, float] = {}
    new = not JOURNAL.exists()
    try:
        with JOURNAL.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                writer.writeheader()
            while True:
                now = timer.unix()
                window = timer.window_start(now)
                secs_left = window + 300 - now
                tokens = market_discovery.get_tokens_for_current_round(window)
                spot = await asyncio.to_thread(_binance_spot, session)
                # BUGFIX: this used to be an unconditional setdefault, so
                # starting the recorder mid-round recorded a MID-ROUND price
                # as that round's strike and every binance-signal call for
                # the round compared against the wrong reference. Only a
                # sample taken within BOUNDARY_GRACE of the open may claim to
                # be the strike; otherwise the field stays blank and analyze
                # skips the round rather than scoring it against a fiction.
                if spot is not None and secs_left >= 300 - BOUNDARY_GRACE:
                    bn_strike.setdefault(window, spot)
                # One entry per 5 minutes accumulates forever on a long run.
                for stale in [w for w in bn_strike if w < window - 3600]:
                    bn_strike.pop(stale, None)
                if tokens:
                    up_ask, up_bid, up_bv, up_av = await asyncio.to_thread(
                        _book, tokens["up_token_id"])
                    dn_ask, dn_bid, _bv, _av = await asyncio.to_thread(
                        _book, tokens["down_token_id"])
                    cl_strike = strike.strike_for(window)
                    cl_now = strike.current_value()
                    writer.writerow({
                        "wall": f"{now:.3f}", "window": window,
                        "secs_left": f"{secs_left:.1f}",
                        "cl_strike": "" if cl_strike is None else f"{cl_strike}",
                        "cl_now": "" if cl_now is None else f"{cl_now}",
                        "bn_strike": bn_strike.get(window, ""),
                        "bn_now": "" if spot is None else spot,
                        "up_ask": "" if up_ask is None else up_ask,
                        "up_bid": "" if up_bid is None else up_bid,
                        "dn_ask": "" if dn_ask is None else dn_ask,
                        "dn_bid": "" if dn_bid is None else dn_bid,
                        "up_bid_vol": f"{up_bv:.2f}", "up_ask_vol": f"{up_av:.2f}",
                    })
                    fh.flush()
                await asyncio.sleep(max(1.0, interval))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[JOURNAL] stopped")
    finally:
        await strike.stop()
    return 0


# --------------------------------------------------------------- resolving ---
def resolve() -> int:
    """Ask Polymarket who won each recorded round.

    BUGFIX: this used to call `market_discovery.get_btc_5m_tokens(window)`,
    which refuses any window before the current one by design (the guard that
    stops the ORDER path touching an already-resolving market). Every past
    round therefore came back None and the function reported "0 resolved, N
    still pending" forever while never issuing a single request. The real
    resolver lives in journal_resolve.py, which fetches the slug directly and
    keeps every identity check while dropping only the tradeability flags.
    """
    import journal_resolve
    return journal_resolve.main([])


def _rows() -> list[dict]:
    with JOURNAL.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------- analysing ---
def _f(row, key):
    v = row.get(key)
    if v in (None, ""):
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _sides(row) -> dict[str, str | None]:
    """Apply the rules here, not at record time, so they can change."""
    cl_s, cl_n = _f(row, "cl_strike"), _f(row, "cl_now")
    bn_s, bn_n = _f(row, "bn_strike"), _f(row, "bn_now")
    bid_v, ask_v = _f(row, "up_bid_vol"), _f(row, "up_ask_vol")
    book = None
    if bid_v and ask_v:                     # abstain on a one-sided book
        book = "UP" if bid_v >= ask_v else "DOWN"
    return {
        "chainlink": None if None in (cl_s, cl_n) else ("UP" if cl_n >= cl_s else "DOWN"),
        "binance": None if None in (bn_s, bn_n) else ("UP" if bn_n >= bn_s else "DOWN"),
        "book": book,
    }


def analyze() -> int:
    if not JOURNAL.exists():
        raise SystemExit(f"no journal at {JOURNAL}")
    winners = json.loads(WINNERS.read_text()) if WINNERS.exists() else {}
    rows = [r for r in _rows() if winners.get(str(r["window"])) in ("UP", "DOWN")]
    if not rows:
        raise SystemExit("no resolved samples yet - run `resolve` once rounds close")

    print(f"{len(rows)} resolved samples across "
          f"{len({r['window'] for r in rows})} rounds\n")

    print("=" * 74)
    print("SIGNAL ACCURACY, AND WHAT THE MARKET CHARGED FOR THE SAME CALL")
    print("=" * 74)
    print(f"{'signal':>11}{'calls':>8}{'accuracy':>10}{'mkt implied':>13}{'edge':>9}")
    for name in ("binance", "chainlink", "book"):
        calls, correct, implied = 0, 0, 0.0
        for r in rows:
            side = _sides(r)[name]
            ask = _f(r, "up_ask") if side == "UP" else _f(r, "dn_ask")
            if side is None or ask is None:
                continue
            calls += 1
            correct += 1 if side == winners[str(r["window"])] else 0
            implied += ask
        if not calls:
            continue
        acc, imp = correct / calls * 100, implied / calls * 100
        print(f"{name:>11}{calls:>8}{acc:>9.1f}%{imp:>12.1f}%{acc - imp:>+8.1f}")
    print("\n  edge = how much better the signal was than the price you'd pay")
    print("  to act on it. Positive is the only number that can make money.")

    print("\n" + "=" * 74)
    print("BY TIME REMAINING IN THE ROUND")
    print("=" * 74)
    print(f"{'window':>14}{'signal':>11}{'calls':>8}{'accuracy':>10}{'implied':>10}{'edge':>9}")
    for lo, hi, label in ((240, 301, "300-240s"), (180, 240, "240-180s"),
                          (120, 180, "180-120s"), (60, 120, "120-60s"),
                          (0, 60, "60-0s")):
        bucket = [r for r in rows if lo <= (_f(r, "secs_left") or -1) < hi]
        for name in ("binance", "chainlink", "book"):
            calls, correct, implied = 0, 0, 0.0
            for r in bucket:
                side = _sides(r)[name]
                ask = _f(r, "up_ask") if side == "UP" else _f(r, "dn_ask")
                if side is None or ask is None:
                    continue
                calls += 1
                correct += 1 if side == winners[str(r["window"])] else 0
                implied += ask
            if calls < 10:
                continue
            acc, imp = correct / calls * 100, implied / calls * 100
            print(f"{label:>14}{name:>11}{calls:>8}{acc:>9.1f}%{imp:>9.1f}%{acc - imp:>+8.1f}")

    print("\n" + "=" * 74)
    print("HOW BIG A SAMPLE BEFORE ANY OF THIS MEANS ANYTHING")
    print("=" * 74)
    # BUGFIX: this used to be 50/sqrt(len(rows)) - the number of SAMPLES.
    # Every sample inside one 5-minute round settles on the SAME outcome, so
    # they are not independent observations. At a 2s cadence there are ~150
    # rows per round, which understated the error bar by sqrt(150) ~ 12x and
    # told you a 3-point edge was readable after about seven rounds. The
    # round is the unit of evidence; the sample is not.
    n_rows = len(rows)
    n_rounds = len({r["window"] for r in rows})
    se = 50 / math.sqrt(n_rounds) if n_rounds else 0.0
    naive = 50 / math.sqrt(n_rows) if n_rows else 0.0
    print(f"  samples so far        {n_rows}")
    print(f"  ROUNDS so far         {n_rounds}   <- this is the sample size")
    print(f"  1 SE on a win rate    +/-{se:.1f} points")
    print(f"  an edge is readable at roughly 2 SE, so ~{2 * se:.1f} points here.")
    print(f"  for a 3-point edge you need about {int((2 * 50 / 3) ** 2):,} ROUNDS.")
    if n_rows and n_rounds and n_rows > n_rounds:
        print(f"\n  (counting the {n_rows} samples instead of the {n_rounds} rounds")
        print(f"   would claim +/-{naive:.1f} points - {se / naive:.0f}x too confident.)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("record", "resolve", "analyze"))
    ap.add_argument("--interval", type=float, default=12.0,
                    help="seconds between samples while recording (default 12)")
    args = ap.parse_args()
    if args.mode == "record":
        return asyncio.run(record(args.interval))
    if args.mode == "resolve":
        return resolve()
    return analyze()


if __name__ == "__main__":
    sys.exit(main())
