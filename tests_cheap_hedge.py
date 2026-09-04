"""Unit tests for cheap_hedge.evaluate_cheap_hedge.

Every trigger branch has its own test. The math sanity check exercises the
loss-cap sizing and the max-hedge-cost clamp - both are the levers an
operator will tune, so a regression on either is worth catching loudly.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cheap_hedge import evaluate_cheap_hedge  # noqa: E402

FAILED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILED
    prefix = "  ok  " if cond else "  FAIL"
    if not cond:
        FAILED += 1
    tail = f" - {detail}" if detail else ""
    print(f"{prefix} {label}{tail}")


BASE = dict(
    enabled=True,
    remaining=120.0,          # inside default 60-180 window
    start=180.0,
    cutoff=60.0,
    up_shares=0.0, up_cost=0.0,
    down_shares=40.0, down_cost=24.0,     # held DOWN, $24 cost
    up_ask=0.12, down_ask=0.85,           # UP is the cheap underdog
    ask_min=0.10, ask_max=0.15,
    min_held_cost=15.0,
    loss_cap=10.0,
    max_hedge_cost=3.50,
    price_side="DOWN",
    book_side="DOWN",
    chainlink_side="DOWN",
    require_strong_signal=True,
    already_hedged=False,
)


def t_happy_path_fires_buy_on_underdog():
    d = evaluate_cheap_hedge(**BASE)
    check("action == buy", d["action"] == "buy", str(d))
    check("side is the UP underdog (not held DOWN)", d["side"] == "UP",
          str(d))
    check("held side reported as DOWN", d["held_side"] == "DOWN", str(d))
    check("ask reported as underdog ask", d["ask"] == 0.12, str(d))
    # target recovery = held_cost - loss_cap = 24 - 10 = 14
    # shares_needed = 14 / (1 - 0.12) = 15.909
    # hedge_cost = 15.909 * 0.12 = 1.909
    check("hedge_cost sized to cap loss at ~$10",
          abs(d["amount"] - 1.909) < 0.01, str(d["amount"]))
    check("shares match hedge_cost / ask",
          abs(d["shares"] - d["amount"] / 0.12) < 1e-6, str(d))
    check("max_price is ASK_MAX (never above)",
          d["max_price"] == 0.15, str(d))


def t_off_when_disabled():
    d = evaluate_cheap_hedge(**{**BASE, "enabled": False})
    check("disabled short-circuits to skip",
          d["action"] == "skip" and d["reason"] == "off", str(d))


def t_outside_window_before_start():
    d = evaluate_cheap_hedge(**{**BASE, "remaining": 200.0})
    check("remaining > start refuses",
          d["action"] == "skip" and "outside window" in d["reason"],
          str(d))


def t_outside_window_after_cutoff():
    d = evaluate_cheap_hedge(**{**BASE, "remaining": 30.0})
    check("remaining <= cutoff refuses",
          d["action"] == "skip" and "outside window" in d["reason"],
          str(d))


def t_already_hedged_short_circuits():
    d = evaluate_cheap_hedge(**{**BASE, "already_hedged": True})
    check("already hedged skips",
          d["action"] == "skip" and "already hedged" in d["reason"],
          str(d))


def t_small_position_refused():
    # $5 held, below default $15 min
    d = evaluate_cheap_hedge(**{**BASE, "down_cost": 5.0})
    check("held cost < min refuses",
          d["action"] == "skip" and "held cost" in d["reason"],
          str(d))


def t_no_clear_held_side():
    d = evaluate_cheap_hedge(**{**BASE, "up_cost": 24.0})   # both equal
    check("equal-cost pair refuses",
          d["action"] == "skip" and "no clear held side" in d["reason"],
          str(d))


def t_underdog_ask_missing():
    d = evaluate_cheap_hedge(**{**BASE, "up_ask": None})
    check("no underdog ask refuses",
          d["action"] == "skip" and "no ask on underdog" in d["reason"],
          str(d))


def t_underdog_ask_out_of_band_low():
    d = evaluate_cheap_hedge(**{**BASE, "up_ask": 0.05})
    check("underdog ask below band refuses",
          d["action"] == "skip" and "outside" in d["reason"],
          str(d))


def t_underdog_ask_out_of_band_high():
    d = evaluate_cheap_hedge(**{**BASE, "up_ask": 0.30})
    check("underdog ask above band refuses",
          d["action"] == "skip" and "outside" in d["reason"],
          str(d))


def t_signal_flipped_refuses():
    # Held side is DOWN. price_side has already flipped to UP.
    d = evaluate_cheap_hedge(**{**BASE, "price_side": "UP"})
    check("price signal flip refuses",
          d["action"] == "skip" and "SIG_PRICE=UP" in d["reason"],
          str(d))


def t_signal_neutral_does_not_refuse():
    # A neutral (None) signal is not a disagreement; it just did not vote.
    d = evaluate_cheap_hedge(**{**BASE, "price_side": None})
    check("neutral signal does not block",
          d["action"] == "buy", str(d))


def t_require_strong_signal_disabled_ignores_flip():
    d = evaluate_cheap_hedge(**{
        **BASE, "require_strong_signal": False, "price_side": "UP"
    })
    check("with require_strong_signal off, a flipped signal is ignored",
          d["action"] == "buy", str(d))


def t_held_cost_already_within_cap():
    # held cost 8 <= loss cap 10; nothing to insure
    d = evaluate_cheap_hedge(**{**BASE, "down_cost": 8.0,
                                "min_held_cost": 0.0})
    check("held cost within loss cap refuses",
          d["action"] == "skip" and "loss cap" in d["reason"],
          str(d))


def t_hedge_cost_clamped_by_max():
    # Very high held cost; uncapped hedge would exceed MAX_HEDGE_COST.
    d = evaluate_cheap_hedge(**{**BASE, "down_cost": 60.0, "loss_cap": 5.0})
    # uncapped: (60-5)/(1-0.12) = 62.5; hedge_cost = 62.5*0.12 = 7.5
    # capped at MAX_HEDGE_COST=3.50
    check("hedge_cost capped at max_hedge_cost",
          abs(d["amount"] - 3.50) < 1e-9, str(d["amount"]))
    check("hedge still fires despite the clamp",
          d["action"] == "buy", str(d))
    check("uncapped cost reported for observability",
          d["hedge_cost_uncapped"] > d["amount"], str(d))


def t_underdog_priced_at_or_above_1():
    d = evaluate_cheap_hedge(**{**BASE, "up_ask": 0.999})
    # 0.999 is above ASK_MAX so band guard fires first, not the >=1 branch.
    check("prices above band refuse before reaching >=1 branch",
          d["action"] == "skip" and "outside" in d["reason"],
          str(d))


def t_side_selection_flips_when_up_is_the_held_side():
    d = evaluate_cheap_hedge(**{
        **BASE,
        "up_shares": 40.0, "up_cost": 24.0,
        "down_shares": 0.0, "down_cost": 0.0,
        "up_ask": 0.85, "down_ask": 0.12,  # DOWN is now the cheap underdog
        "price_side": "UP", "book_side": "UP", "chainlink_side": "UP",
    })
    check("hedges the OTHER side (DOWN) when UP is held",
          d["action"] == "buy" and d["side"] == "DOWN", str(d))


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("t_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                global FAILED
                FAILED += 1
                print(f"  FAIL {name} raised {type(exc).__name__}: {exc}")
    print()
    print("cheap_hedge:", "PASS" if FAILED == 0 else f"{FAILED} FAILURES")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
