"""Cheap-hedge decision. Pure: no I/O, no orders.

One clip per round, fires only when we have already built a real position on
one side and the UNDERDOG's ask sits inside a narrow cheap band. The point is
insurance against a late reversal - if the round flips, the underdog pays $1
per share and the cheap shares recover most of the exposed loss. When the
round settles the way we bet, the small hedge cost was the premium.

Deliberately independent of LATE_TRIM. That module fires later (T-60..T-20),
buys the FAVORITE (0.80-0.88), and closes a hole that already exists. This
module fires earlier (T-180..T-60), buys the UNDERDOG (0.10-0.15), and pays
a small premium for a large payout if the round reverses.

Only insures a "large" position: MIN_HELD_COST enforces that the exposed
side is worth insuring in the first place. On a small position the insurance
premium eats too much of the win when we are right.
"""
from __future__ import annotations

import math
from typing import Any


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def evaluate_cheap_hedge(
    *,
    enabled: bool,
    remaining: float,
    start: float,
    cutoff: float,
    up_shares: float,
    up_cost: float,
    down_shares: float,
    down_cost: float,
    up_ask: float | None,
    down_ask: float | None,
    ask_min: float,
    ask_max: float,
    min_held_cost: float,
    loss_cap: float,
    max_hedge_cost: float,
    current_traded_side: str | None,
    require_strong_signal: bool,
    already_hedged: bool,
) -> dict[str, Any]:
    """Return {'action': 'buy'|'skip', 'side', 'amount', 'reason', ...} for one
    cheap-hedge clip. Never raises - every branch is a decision, not an
    exception, so a caller in the hot exit path can act on the return value
    without a try/except.
    """
    up_c = _finite(up_cost) or 0.0
    down_c = _finite(down_cost) or 0.0

    out: dict[str, Any] = {
        "action": "skip",
        "side": None,
        "reason": "idle",
        "amount": None,
        "max_price": None,
        "ask": None,
        "shares": None,
        "held_side": None,
        "held_cost": None,
        "hedge_cost_uncapped": None,
    }

    if not enabled:
        out["reason"] = "off"
        return out

    remain = _finite(remaining)
    if remain is None or remain <= cutoff or remain > start:
        out["reason"] = "outside window"
        return out

    if already_hedged:
        out["reason"] = "already hedged this round"
        return out

    # Determine which side is "held" - the one with the larger cost. A round
    # with roughly equal exposure on both sides is not the shape this feature
    # exists for; the caller has already paid a pair overround and reversal
    # protection is not the right tool for that. Refuse cleanly.
    if up_c > down_c:
        held_side = "UP"
        held_cost = up_c
        hedge_side = "DOWN"
        hedge_ask = _finite(down_ask)
    elif down_c > up_c:
        held_side = "DOWN"
        held_cost = down_c
        hedge_side = "UP"
        hedge_ask = _finite(up_ask)
    else:
        out["reason"] = "no clear held side"
        return out

    out["held_side"] = held_side
    out["held_cost"] = held_cost

    if held_cost < min_held_cost:
        out["reason"] = (
            f"held cost ${held_cost:.2f} < min ${min_held_cost:.2f}"
        )
        return out

    if hedge_ask is None:
        out["reason"] = "no ask on underdog"
        return out

    out["ask"] = hedge_ask
    if not ask_min <= hedge_ask <= ask_max:
        out["reason"] = (
            f"underdog ask {hedge_ask:.3f} outside "
            f"[{ask_min:.2f}, {ask_max:.2f}]"
        )
        return out

    if require_strong_signal:
        # Refuse if the strategy is CURRENTLY trading the opposite side -
        # that is a reversal in progress, and buying the underdog then is
        # chasing, not hedging. Passing the actual traded side (rather
        # than the raw SIG_PRICE) is what makes this correct under
        # SIGNAL_MINORITY_RULE: with minority on, the traded side is the
        # dissenting signal, so an "any signal disagrees" check would
        # refuse every legitimate hedge by design.
        #
        # A None current side is not a refusal - just means we do not know
        # the last direction, which is common at round start before the
        # first phase-2 evaluation lands.
        if (current_traded_side in ("UP", "DOWN")
                and current_traded_side != held_side):
            out["reason"] = (
                f"strategy currently trading {current_traded_side}, "
                f"opposite of held {held_side}"
            )
            return out

    # Sizing: cap the reversal loss at LOSS_CAP.
    # We need enough hedge shares to recover (held_cost - loss_cap) when the
    # underdog pays $1. Each hedge share costs `hedge_ask` and pays $1, so
    # its NET recovery per share is (1 - hedge_ask). shares_needed follows.
    target_recovery = held_cost - loss_cap
    if target_recovery <= 0.0:
        out["reason"] = (
            f"held cost ${held_cost:.2f} already within loss cap "
            f"${loss_cap:.2f}"
        )
        return out
    denom = 1.0 - hedge_ask
    if denom <= 0.0:
        out["reason"] = "underdog ask >= 1.0; hedge would not recover anything"
        return out
    shares_needed = target_recovery / denom
    hedge_cost_uncapped = shares_needed * hedge_ask
    out["hedge_cost_uncapped"] = hedge_cost_uncapped

    # Cap the hedge itself so a wide gap between held_cost and loss_cap
    # doesn't ask for a hedge bigger than the operator will spend. Accepting
    # a partial hedge is better than skipping the insurance entirely.
    hedge_cost = min(hedge_cost_uncapped, max_hedge_cost)
    if hedge_cost <= 0.0:
        out["reason"] = "hedge cost non-positive"
        return out

    out.update({
        "action": "buy",
        "side": hedge_side,
        "reason": "hedge",
        "amount": hedge_cost,
        "max_price": ask_max,
        "shares": hedge_cost / hedge_ask,
    })
    return out
