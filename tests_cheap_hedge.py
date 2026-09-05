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


# Held 40 DOWN shares for $24 all-in = 0.60/share. Underdog UP at 0.12.
#   hedge fee/share = 0.07 * 0.12 * 0.88          = 0.00739
#   locked/pair     = 1 - (0.60 + 0.12 + 0.00739) = +0.2726
# Comfortably above the 0.02 default edge, so this pair fires.
BASE = dict(
    enabled=True,
    remaining=120.0,          # inside the 60-180 window
    start=180.0,
    cutoff=60.0,
    up_shares=0.0, up_cost=0.0,
    down_shares=40.0, down_cost=24.0,     # held DOWN, $24 all-in, 0.60/sh
    up_ask=0.12, down_ask=0.85,           # UP is the cheap underdog
    ask_min=0.10, ask_max=0.15,
    min_held_cost=15.0,
    fee_rate=0.07,
    min_locked_edge=0.02,
    max_hedge_cost=3.50,
    current_traded_side="DOWN",           # strategy still trading held side
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
    check("held all-in per share is cost/shares",
          abs(d["held_all_in"] - 0.60) < 1e-9, str(d["held_all_in"]))
    # locked = 1 - (0.60 + 0.12 + 0.07*0.12*0.88) = 0.27261
    check("locked/pair computed with BOTH legs' fees",
          abs(d["locked_per_pair"] - 0.27261) < 1e-4,
          str(d["locked_per_pair"]))
    # full match wants 40 shares * 0.12 = $4.80, clamped to max_hedge_cost 3.50
    check("sizing targets a FULL match before the clamp",
          abs(d["hedge_cost_uncapped"] - 4.80) < 1e-9,
          str(d["hedge_cost_uncapped"]))
    check("spend clamped to max_hedge_cost",
          abs(d["amount"] - 3.50) < 1e-9, str(d["amount"]))
    check("shares match hedge_cost / ask",
          abs(d["shares"] - d["amount"] / 0.12) < 1e-6, str(d))
    check("max_price is ASK_MAX (never above)",
          d["max_price"] == 0.15, str(d))
    check("reason names the mechanism", d["reason"] == "locked pair", str(d))


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


def t_current_traded_side_flipped_refuses():
    # Held DOWN, but the strategy is currently trading UP - reversal in
    # progress, no hedge.
    d = evaluate_cheap_hedge(**{**BASE, "current_traded_side": "UP"})
    check("current traded side flip refuses",
          d["action"] == "skip"
          and "strategy currently trading UP" in d["reason"],
          str(d))


def t_current_traded_side_none_does_not_refuse():
    # A None current side is not a flip - just unknown. Hedge should not
    # be blocked on unknown; the caller passes None before the first
    # phase-2 evaluation of the round.
    d = evaluate_cheap_hedge(**{**BASE, "current_traded_side": None})
    check("None current side does not block",
          d["action"] == "buy", str(d))


def t_require_strong_signal_disabled_ignores_flip():
    d = evaluate_cheap_hedge(**{
        **BASE, "require_strong_signal": False, "current_traded_side": "UP"
    })
    check("with require_strong_signal off, a flipped current side is ignored",
          d["action"] == "buy", str(d))


def t_minority_rule_scenario_now_permits_hedge():
    # This is the exact case that used to fail. With minority rule ON, the
    # bot builds a DOWN position while SIG_PRICE stays UP (majority). Now
    # we check the ACTUAL traded side, not SIG_PRICE, so the hedge fires.
    # current_traded_side matches held_side because that is what the bot
    # picked via minority_decision.
    d = evaluate_cheap_hedge(**{
        **BASE, "current_traded_side": "DOWN",     # bot picked DOWN
    })
    check("under minority rule, hedge fires when current side matches held",
          d["action"] == "buy" and d["side"] == "UP", str(d))


def t_losing_pair_is_refused():
    # Held 40 sh for $34 all-in = 0.85/share. Underdog at 0.12 plus its fee
    # makes the pair 0.85 + 0.12 + 0.0074 = 0.9774 -> locks only +0.0226.
    # Push the entry to 0.90/share and the pair goes underwater.
    d = evaluate_cheap_hedge(**{**BASE, "down_cost": 36.0,
                                "min_held_cost": 0.0})
    # all-in 0.90 -> locked = 1 - (0.90 + 0.12 + 0.00739) = -0.0274
    check("pair that would lose after fees is refused",
          d["action"] == "skip" and "pair locks" in d["reason"], str(d))
    check("locked/pair is reported even on refusal",
          d["locked_per_pair"] is not None and d["locked_per_pair"] < 0,
          str(d["locked_per_pair"]))


def t_thin_edge_below_minimum_is_refused():
    # all-in 0.85 -> locked = 1 - (0.85 + 0.12 + 0.00739) = +0.0226.
    # Positive, but demand a 0.05 edge and it must still refuse.
    d = evaluate_cheap_hedge(**{**BASE, "down_cost": 34.0,
                                "min_held_cost": 0.0,
                                "min_locked_edge": 0.05})
    check("locked edge below the required minimum refuses",
          d["action"] == "skip" and "needs 0.0500" in d["reason"], str(d))
    check("same pair fires when the minimum is lowered",
          evaluate_cheap_hedge(**{**BASE, "down_cost": 34.0,
                                  "min_held_cost": 0.0,
                                  "min_locked_edge": 0.02})["action"] == "buy")


def t_zero_shares_refused():
    d = evaluate_cheap_hedge(**{**BASE, "down_shares": 0.0,
                                "min_held_cost": 0.0})
    check("held cost with no shares cannot be matched",
          d["action"] == "skip" and "no shares to match" in d["reason"],
          str(d))


def t_hedge_cost_clamped_by_max():
    # 40 held shares at a full match costs 40 * 0.12 = $4.80, above the
    # $3.50 ceiling, so the spend clamps but the hedge still fires.
    d = evaluate_cheap_hedge(**BASE)
    check("hedge_cost capped at max_hedge_cost",
          abs(d["amount"] - 3.50) < 1e-9, str(d["amount"]))
    check("hedge still fires despite the clamp",
          d["action"] == "buy", str(d))
    check("uncapped (full-match) cost reported for observability",
          abs(d["hedge_cost_uncapped"] - 4.80) < 1e-9, str(d))
    # Raise the ceiling and the full match goes through untouched.
    full = evaluate_cheap_hedge(**{**BASE, "max_hedge_cost": 10.0})
    check("with headroom it buys the full match",
          abs(full["amount"] - 4.80) < 1e-9, str(full["amount"]))
    check("full match means one hedge share per held share",
          abs(full["shares"] - 40.0) < 1e-6, str(full["shares"]))


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
        "current_traded_side": "UP",       # strategy still buying UP
    })
    check("hedges the OTHER side (DOWN) when UP is held",
          d["action"] == "buy" and d["side"] == "DOWN", str(d))


# --- price cap must carry the locked-edge proof into the order --------------
# The decision's max_price becomes the FOK's ceiling, and a FOK walks the book.
# Capping at ask_max let an authorized hedge fill above the ask the gate was
# evaluated against, turning a proven pair into a guaranteed loss.
def _locked(all_in: float, price: float, fee_rate: float = 0.07) -> float:
    return 1.0 - (all_in + price + fee_rate * price * (1.0 - price))


def t_price_cap_never_authorizes_a_losing_fill():
    # Held 40 DOWN at 0.86 all-in. At the 0.10 ask the pair locks +0.034, but
    # a fill at the 0.20 band top would lock -0.071 on every matched pair.
    d = evaluate_cheap_hedge(**{
        **BASE,
        "down_cost": 34.4, "down_shares": 40.0,   # 0.86/share all-in
        "up_ask": 0.10,
        "ask_min": 0.10, "ask_max": 0.20,
    })
    check("still fires at the observed ask", d["action"] == "buy", str(d))
    check("cap is tightened below the sanity band top",
          d["max_price"] < 0.20, str(d["max_price"]))
    check("a fill at the cap still clears min_locked_edge",
          _locked(0.86, d["max_price"]) >= BASE["min_locked_edge"] - 1e-9,
          f"cap={d['max_price']} locked={_locked(0.86, d['max_price'])}")
    check("one tick above the cap would NOT clear it",
          _locked(0.86, d["max_price"] + 0.01) < BASE["min_locked_edge"],
          str(d["max_price"]))
    check("cap is never below the ask that authorized the order",
          d["max_price"] >= d["ask"], str(d))


def t_price_cap_stays_at_the_band_when_the_band_binds():
    # A cheap held side (0.60 all-in) clears the edge well past 0.15, so the
    # sanity band remains the binding constraint and the cap does not move.
    d = evaluate_cheap_hedge(**BASE)
    check("cap stays at ask_max when the edge allows more",
          d["max_price"] == BASE["ask_max"], str(d["max_price"]))


def t_price_cap_reported_even_when_it_equals_the_edge_ceiling():
    d = evaluate_cheap_hedge(**{
        **BASE,
        "down_cost": 34.4, "down_shares": 40.0,
        "up_ask": 0.10,
        "ask_min": 0.10, "ask_max": 0.20,
        "min_locked_edge": 0.0,
    })
    check("a zero required edge still caps at break-even",
          d["action"] == "buy" and d["max_price"] < 0.20, str(d))
    check("break-even cap locks ~0.0 per pair",
          abs(_locked(0.86, d["max_price"])) < 1e-9,
          str(_locked(0.86, d["max_price"])))


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
