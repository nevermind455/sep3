"""Cheap-hedge decision. Pure: no I/O, no orders.

One clip per round. Fires only when buying the UNDERDOG completes a pair
that is PROVABLY PROFITABLE after both legs' fees - never as a speculative
premium. A matched UP+DOWN pair redeems for exactly $1.00 whichever way the
round settles, so if the all-in cost of both legs is below $1.00 the matched
portion is locked profit regardless of outcome.

That is the whole test:

    locked_per_pair = 1.00 - (held_all_in_per_share
                              + underdog_ask
                              + underdog_fee_per_share)

and the hedge fires only when ``locked_per_pair >= min_locked_edge``.

A raw price band cannot express this. At a 0.80 entry an underdog at 0.18
is roughly break-even after fees; at a 0.60 entry the same 0.18 is a large
locked profit; at a 0.90 entry it is a guaranteed loss. The band survives
only as a loose sanity bound on absurd quotes - the locked-edge test is the
real gate.

Sizing targets a FULL match (one underdog share per held share), because
every matched pair carries the same locked edge and leaving shares unmatched
just leaves that profit on the table. ``max_hedge_cost`` is the spend ceiling
when a full match would cost more than the operator wants to commit.

The returned ``max_price`` is the price the proof still holds at, not the top
of the sanity band. A FOK walks the book, so an order capped at ``ask_max``
could fill above the ask the gate was evaluated against and turn a proven pair
into a guaranteed loss; the cap is therefore solved back out of the locked-edge
inequality (see ``_max_price_for_locked_edge``).

Deliberately independent of LATE_TRIM. That module fires later (T-60..T-20),
buys the FAVORITE, and closes a hole that already exists. This one fires
anywhere before its cutoff, buys the UNDERDOG, and only when the finished
pair cannot lose.
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


def _max_price_for_locked_edge(held_all_in: float, fee_rate: float,
                               min_locked_edge: float) -> float | None:
    """Highest fill price that still leaves ``min_locked_edge`` per pair.

    The gate below proves the pair is profitable at the ask we can SEE, but a
    FOK walks the book and fills anywhere up to its cap. Capping at ``ask_max``
    therefore let an authorized hedge fill at a price the proof never covered:
    at a 0.86 held all-in a 0.10 ask locks +0.034/pair, while the same order
    filling at the 0.20 band top locks -0.071 - a guaranteed loss on every
    matched pair. So solve the gate for price and cap the order there.

        1 - (held_all_in + p + fee_rate*p*(1-p)) >= min_locked_edge

    Rearranged with ``B = 1 - held_all_in - min_locked_edge``:

        fee_rate*p^2 - (1 + fee_rate)*p + B >= 0

    On [0, 1] that parabola's vertex sits at (1+f)/(2f) >= 1 for any
    fee_rate <= 1, so the expression is decreasing across the whole price
    range and the smaller root is the ceiling. Returns None when no price
    works (the pair cannot clear the required edge at any cost).
    """
    b = 1.0 - held_all_in - min_locked_edge
    if not math.isfinite(b) or b <= 0.0:
        return None
    if fee_rate <= 0.0:
        return b
    disc = (1.0 + fee_rate) ** 2 - 4.0 * fee_rate * b
    if not math.isfinite(disc) or disc < 0.0:
        return None
    root = ((1.0 + fee_rate) - math.sqrt(disc)) / (2.0 * fee_rate)
    return root if math.isfinite(root) and root > 0.0 else None


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
    fee_rate: float,
    min_locked_edge: float,
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
    up_sh = _finite(up_shares) or 0.0
    down_sh = _finite(down_shares) or 0.0

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
        "held_shares": None,
        "held_all_in": None,
        "locked_per_pair": None,
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
        held_side, held_cost, held_shares = "UP", up_c, up_sh
        hedge_side = "DOWN"
        hedge_ask = _finite(down_ask)
    elif down_c > up_c:
        held_side, held_cost, held_shares = "DOWN", down_c, down_sh
        hedge_side = "UP"
        hedge_ask = _finite(up_ask)
    else:
        out["reason"] = "no clear held side"
        return out

    out["held_side"] = held_side
    out["held_cost"] = held_cost
    out["held_shares"] = held_shares

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

    # ---- the real gate: is the finished pair profitable after both fees? ----
    # `held_cost` is the ledger's all-in figure (notional + fee already paid),
    # so dividing by shares gives what each held share truly cost us. A
    # matched pair redeems exactly $1.00, so anything the two legs cost below
    # that is locked profit no matter which outcome wins.
    if held_shares <= 0.0:
        out["reason"] = "held side has no shares to match"
        return out
    held_all_in = held_cost / held_shares
    out["held_all_in"] = held_all_in

    hedge_fee_per_share = fee_rate * hedge_ask * (1.0 - hedge_ask)
    locked = 1.0 - (held_all_in + hedge_ask + hedge_fee_per_share)
    out["locked_per_pair"] = locked
    if locked < min_locked_edge:
        out["reason"] = (
            f"pair locks {locked:+.4f}/share (held all-in {held_all_in:.4f} "
            f"+ ask {hedge_ask:.4f} + fee {hedge_fee_per_share:.4f}); "
            f"needs {min_locked_edge:.4f}"
        )
        return out

    # ---- sizing: target a FULL match ----------------------------------------
    # Every matched pair carries the same locked edge, so a partial match just
    # leaves guaranteed profit unclaimed. Buy one underdog share per held
    # share, and let max_hedge_cost be the only thing that trims it.
    hedge_cost_uncapped = held_shares * hedge_ask
    out["hedge_cost_uncapped"] = hedge_cost_uncapped
    hedge_cost = min(hedge_cost_uncapped, max_hedge_cost)
    if hedge_cost <= 0.0:
        out["reason"] = "hedge cost non-positive"
        return out

    # ---- price cap: carry the proof into the order -------------------------
    # The caller turns `max_price` into the FOK's ceiling, and a FOK walks the
    # book. Capping at ask_max would authorize a fill the locked-edge test
    # never covered, so cap at the highest price that still clears the edge.
    edge_cap = _max_price_for_locked_edge(held_all_in, fee_rate, min_locked_edge)
    if edge_cap is None:
        out["reason"] = "no fill price clears the required locked edge"
        return out
    price_cap = min(ask_max, edge_cap)
    out["max_price"] = price_cap
    if price_cap < hedge_ask:
        # Unreachable while the gate above holds (it passed AT hedge_ask), but
        # a rounding-edge case here would send an order that cannot fill, so
        # refuse rather than emit one.
        out["reason"] = (
            f"edge cap {price_cap:.4f} is below the {hedge_ask:.4f} ask"
        )
        return out

    out.update({
        "action": "buy",
        "side": hedge_side,
        "reason": "locked pair",
        "amount": hedge_cost,
        "shares": hedge_cost / hedge_ask,
    })
    return out
