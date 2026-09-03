"""Last-60s loss trim decision. Pure: no I/O, no orders.

The window is LATE_TRIM_START_SECONDS..LATE_TRIM_CUTOFF_SECONDS, independent
of the normal last-minute floor. This module only answers whether one extra
FOK of the 0.80-0.88 favorite is allowed, given the combined round book. The
executor still has to pass liquidity, cash, and the narrower expiry floor.
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


def settlement_paths(up_shares, up_cost, down_shares, down_cost) -> dict[str, float]:
    """PnL if each outcome pays $1, using both legs' cost."""
    up_sh = _finite(up_shares) or 0.0
    down_sh = _finite(down_shares) or 0.0
    up_c = _finite(up_cost) or 0.0
    down_c = _finite(down_cost) or 0.0
    cost = up_c + down_c
    return {
        "up_shares": up_sh,
        "down_shares": down_sh,
        "cost": cost,
        "if_up": up_sh - cost,
        "if_down": down_sh - cost,
    }


def evaluate_late_trim(
    *,
    enabled: bool,
    remaining: float,
    start: float,
    cutoff: float,
    clips_used: int,
    max_clips: int,
    interval_s: float,
    last_clip_age_s: float | None,
    up_shares: float,
    up_cost: float,
    down_shares: float,
    down_cost: float,
    up_ask: float | None,
    down_ask: float | None,
    ask_min: float,
    ask_max: float,
    price_side: str | None,
    chainlink_side: str | None,
    amount: float,
    stop_blocks: bool = False,
) -> dict[str, Any]:
    """Return a buy/skip decision for one last-minute trim clip.

    Buys only the red path, and only when the other path is already green.
    Both-green and both-red books are idle.
    """
    paths = settlement_paths(up_shares, up_cost, down_shares, down_cost)
    out = {
        "action": "skip",
        "side": None,
        "reason": "idle",
        "amount": None,
        "max_price": None,
        "ask": None,
        "if_up": paths["if_up"],
        "if_down": paths["if_down"],
        "hole": None,
        "clips_used": clips_used,
    }
    remain = _finite(remaining)
    if not enabled:
        out["reason"] = "off"
        return out
    if remain is None or remain <= cutoff or remain > start:
        out["reason"] = "outside window"
        return out
    if clips_used >= max_clips:
        out["reason"] = "clip cap"
        return out
    if last_clip_age_s is not None and last_clip_age_s < interval_s:
        out["reason"] = "interval"
        return out
    if paths["cost"] <= 0.0 and paths["up_shares"] <= 0.0 and paths["down_shares"] <= 0.0:
        out["reason"] = "no inventory"
        return out

    if_up, if_down = paths["if_up"], paths["if_down"]
    if if_up >= 0.0 and if_down >= 0.0:
        out["reason"] = "both paths green"
        return out
    if if_up < 0.0 and if_down < 0.0:
        out["reason"] = "both paths a hole"
        return out
    if if_up < 0.0:
        side = "UP"
        hole = -if_up
        ask = _finite(up_ask)
    else:
        side = "DOWN"
        hole = -if_down
        ask = _finite(down_ask)
    out["side"] = side
    out["hole"] = hole
    out["ask"] = ask
    if stop_blocks:
        out["reason"] = "stop conflict"
        return out
    if ask is None:
        out["reason"] = "no ask"
        return out
    if not ask_min <= ask <= ask_max:
        out["reason"] = "ask out of band"
        return out
    if price_side != side:
        out["reason"] = "sig price disagrees"
        return out
    if chainlink_side != side:
        out["reason"] = "chainlink disagrees"
        return out
    size = _finite(amount)
    if size is None or size <= 0.0:
        out["reason"] = "invalid amount"
        return out
    out.update({
        "action": "buy",
        "reason": "trim",
        "amount": size,
        "max_price": ask_max,
    })
    return out
