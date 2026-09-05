#!/usr/bin/env python3
"""Check every gate a live order must pass, one at a time.

    python live_preflight.py

Read-only. Places no order, signs nothing, and needs no running bot. Each gate
is reported separately so a refusal names its own cause instead of arriving as
one opaque "order rejected".
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config          # noqa: E402  (loads .env)
import timer           # noqa: E402
import market_discovery  # noqa: E402
import orderbook       # noqa: E402

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def line(status: str, name: str, detail: str = "") -> None:
    print(f"[{status}] {name:<34} {detail}")


def main() -> int:
    failures = 0
    print("=" * 74)
    print("LIVE PREFLIGHT - every gate an order must clear")
    print("=" * 74)

    # 0. the master switch -------------------------------------------------
    # Checked first because it is the only gate that produces NO refusal line
    # at all: with phase 2 parked main_bot never enters the order path, so the
    # log shows a healthy "Running | ... | idle | phase 2 parked" every 30s and
    # nothing else. Every other gate below announces itself when it refuses.
    if not config.PHASE2_ENABLED:
        line(BAD, "PHASE2_ENABLED",
             "off (default) - the order path is never entered; set PHASE2_ENABLED=1")
        failures += 1
    else:
        line(OK, "PHASE2_ENABLED", "on - phase 2 may submit orders")

    # Orders are confined to a window inside each 5-minute round, so "nothing
    # happening" is expected for most of it. State the window explicitly.
    trade_window_s = config.TRADE_LAST_SECONDS - config.MIN_SECONDS_TO_EXPIRY
    if trade_window_s <= 0:
        line(BAD, "trade window",
             f"TRADE_LAST_SECONDS={config.TRADE_LAST_SECONDS} <= "
             f"MIN_SECONDS_TO_EXPIRY={config.MIN_SECONDS_TO_EXPIRY:.0f} - no window exists")
        failures += 1
    else:
        attempts = int(trade_window_s // config.TRADE_INTERVAL_SECONDS)
        left = timer.seconds_left(timer.unix())
        inside = config.MIN_SECONDS_TO_EXPIRY <= left <= config.TRADE_LAST_SECONDS
        line(OK, "trade window",
             f"T-{config.TRADE_LAST_SECONDS}..T-{config.MIN_SECONDS_TO_EXPIRY:.0f} "
             f"({trade_window_s:.0f}s of every 300s, <={attempts} attempts) | "
             f"now T-{left} -> {'INSIDE' if inside else 'outside, idle is normal'}")

    # 1. credentials -------------------------------------------------------
    missing = [k for k in ("POLY_PRIVATE_KEY", "POLY_FUNDER", "POLY_SIGNATURE_TYPE")
               if not (os.environ.get(k) or "").strip()]
    if missing:
        line(BAD, "credentials", f"missing: {', '.join(missing)}")
        failures += 1
    else:
        line(OK, "credentials", "all three present (values not read)")

    # 2. clock - LIVE is fail-closed --------------------------------------
    ok, detail, drift = timer.check_clock(
        config.CLOB_HOST, config.CLOCK_MAX_DRIFT_SECONDS)
    if ok:
        line(OK, "clock", f"{detail} (limit {config.CLOCK_MAX_DRIFT_SECONDS}s)")
    else:
        line(BAD, "clock", f"{detail} - LIVE refuses to trade outside the limit")
        failures += 1

    # 3. market discovery --------------------------------------------------
    window = timer.window_start(timer.unix())
    tokens = market_discovery.get_tokens_for_current_round(window)
    if not tokens:
        line(BAD, "market discovery", "no market for the current round")
        return failures + 1
    line(OK, "market discovery", str(tokens.get("slug")))
    cid = tokens["condition_id"]

    # 4. the matching-delay flag ------------------------------------------
    try:
        import http_pool
        info = http_pool.get(f"{config.CLOB_HOST}/clob-markets/{cid}",
                             timeout=10).json()
        itode = bool(info.get("itode"))
        assumed = float(config.ASSUMED_MATCH_DELAY_SECONDS)
        remaining = timer.seconds_left(timer.unix())
        if not itode:
            line(OK, "matching delay", "venue reports no taker delay")
        elif assumed <= 0:
            line(BAD, "matching delay",
                 "itode=true and ASSUMED_MATCH_DELAY_SECONDS=0 -> every order refused")
            failures += 1
        elif remaining <= assumed:
            line(WARN, "matching delay",
                 f"{remaining:.0f}s left is inside the assumed {assumed:.0f}s window "
                 f"- refused until the next round")
        else:
            line(OK, "matching delay",
                 f"itode=true, {remaining:.0f}s left > assumed {assumed:.0f}s")
    except Exception as exc:
        line(BAD, "matching delay", f"{type(exc).__name__}: {str(exc)[:44]}")
        failures += 1

    # 5. private fill stream ----------------------------------------------
    user_ws = (os.environ.get("USER_WS") or "on").lower()
    if user_ws == "off":
        line(BAD, "private fill stream",
             "USER_WS=off - _execution_ready() refuses every live order")
        failures += 1
    else:
        line(WARN, "private fill stream",
             "USER_WS=on; readiness can only be confirmed by a running bot")

    # 6. tradeable book ----------------------------------------------------
    for label, token in (("UP", tokens["up_token_id"]),
                         ("DOWN", tokens["down_token_id"])):
        try:
            bids, asks = orderbook.get_orderbook(token)
            if not asks:
                line(WARN, f"book {label}", "no asks - nothing to buy this instant")
                continue
            ask = float(asks[0]["price"])
            inside = config.MIN_BUY_PRICE <= ask <= config.MAX_BUY_PRICE
            line(OK if inside else WARN, f"book {label}",
                 f"ask {ask:.3f} "
                 f"{'inside' if inside else 'OUTSIDE'} "
                 f"{config.MIN_BUY_PRICE}-{config.MAX_BUY_PRICE}")
        except Exception as exc:
            line(BAD, f"book {label}", f"{type(exc).__name__}: {str(exc)[:40]}")
            failures += 1

    # 7. signal inputs -----------------------------------------------------
    import main_bot
    cl_now = main_bot.current_chainlink_twap()
    line(WARN if cl_now is None else OK, "chainlink TWAP",
         "no live value (needs a running feed)" if cl_now is None else f"${cl_now:,.2f}")
    print()
    print("=" * 74)
    print(f"{'ALL GATES PASS' if failures == 0 else f'{failures} BLOCKING FAILURE(S)'}")
    print("WARN lines are point-in-time, not permanent - re-run mid-round.")
    print("=" * 74)
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
