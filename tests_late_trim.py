#!/usr/bin/env python3
"""Last-60s loss trim: decision table and paper execution floor.

    python tests_late_trim.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from dataclasses import replace
from accounting.ledger import Ledger  # noqa: E402
from late_trim import evaluate_late_trim, settlement_paths  # noqa: E402
from paper_trade import PaperBroker  # noqa: E402
from tests_paper import book, paper_order, rules  # noqa: E402
import tests_paper as tp  # noqa: E402


def check(name, condition, detail=""):
    tp.check(name, condition, detail)


def _base(**overrides):
    args = dict(
        enabled=True,
        remaining=40.0,
        start=60.0,
        cutoff=20.0,
        clips_used=0,
        max_clips=2,
        interval_s=12.0,
        last_clip_age_s=None,
        up_shares=10.0,
        up_cost=5.0,
        down_shares=35.0,
        down_cost=18.0,
        up_ask=0.83,
        down_ask=0.18,
        ask_min=0.80,
        ask_max=0.88,
        price_side="UP",
        chainlink_side="UP",
        amount=2.50,
        stop_blocks=False,
    )
    args.update(overrides)
    return evaluate_late_trim(**args)


def t_settlement_paths_use_both_legs():
    paths = settlement_paths(9.72, 5.15, 36.09, 18.11)
    check("if down is green on the stacked book", paths["if_down"] > 0, str(paths))
    check("if up is the hole", paths["if_up"] < 0, str(paths))
    check("cost is both legs", abs(paths["cost"] - 23.26) < 1e-9, str(paths["cost"]))


def t_trim_buys_only_the_red_favorite():
    d = _base()
    check("buys UP", d["action"] == "buy" and d["side"] == "UP", str(d))
    check("hole is -if_up", abs(d["hole"] - (23.0 - 10.0)) < 1e-9, str(d))


def t_trim_does_not_add_to_the_already_green_stacked_side():
    # 7x DOWN is green; UP is the hole. If BTC still says DOWN, skip.
    d = _base(up_ask=0.22, down_ask=0.83, price_side="DOWN", chainlink_side="DOWN")
    check("skip when signal is the green stacked side", d["action"] == "skip", str(d))
    check("does not buy DOWN", d["side"] != "DOWN" or d["action"] == "skip", str(d))
    # Same book, BTC/Chainlink now agree UP at 0.83 -> trim UP.
    d2 = _base()
    check("buys the red 0.80 side only", d2["action"] == "buy" and d2["side"] == "UP", str(d2))


def t_trim_skips_both_green_and_both_red():
    green = _base(up_shares=40.0, up_cost=10.0, down_shares=30.0, down_cost=10.0)
    check("both green idles", green["reason"] == "both paths green", str(green))
    red = _base(up_shares=5.0, up_cost=12.0, down_shares=5.0, down_cost=12.0)
    check("both red idles", red["reason"] == "both paths a hole", str(red))


def t_trim_requires_window_signals_and_ask_band():
    check("off", _base(enabled=False)["reason"] == "off")
    check("too early", _base(remaining=61)["reason"] == "outside window")
    check("too late", _base(remaining=19)["reason"] == "outside window")
    check("T-60 is still inside", _base(remaining=60)["action"] == "buy")
    check("window ignores the last-minute floor",
          _base(remaining=40, start=60.0)["action"] == "buy")
    check("clip cap", _base(clips_used=2)["reason"] == "clip cap")
    check("interval", _base(last_clip_age_s=5.0)["reason"] == "interval")
    check("ask high", _base(up_ask=0.91)["reason"] == "ask out of band")
    check("ask low", _base(up_ask=0.79)["reason"] == "ask out of band")
    check("sig price", _base(price_side="DOWN")["reason"] == "sig price disagrees")
    check("chainlink", _base(chainlink_side="DOWN")["reason"] == "chainlink disagrees")
    check("stop", _base(stop_blocks=True)["reason"] == "stop conflict")
    check("second clip after interval still buys",
          _base(clips_used=1, last_clip_age_s=12.0)["action"] == "buy")


def t_ledger_open_inventory_is_per_condition():
    fd, path = tempfile.mkstemp(suffix=".json")
    import os
    os.close(fd)
    os.unlink(path)
    led = Ledger(path=path)
    try:
        led.record_fill("u1", "tok-up", shares=10.0, price=0.40, condition_id="0xcond")
        led.record_fill("d1", "tok-down", shares=5.0, price=0.55, condition_id="0xcond")
        inv = led.open_inventory_for_condition("0xcond")
        check("two legs", set(inv) == {"tok-up", "tok-down"}, str(inv))
        check("up shares", abs(inv["tok-up"]["shares"] - 10.0) < 1e-9)
        check("empty other market", led.open_inventory_for_condition("0xother") == {})
    finally:
        os.path.exists(path) and os.unlink(path)


def t_paper_late_trim_floor_opens_t40_only_when_enabled():
    end = (int(time.time()) // 300 + 1) * 300
    sampled = end - 40.0
    original_unix, original_wall = __import__("timer").unix, __import__("timer").wall
    timer_mod = __import__("timer")
    previous = config.LATE_TRIM_ENABLED
    with tempfile.TemporaryDirectory() as tmp:
        broker = PaperBroker(
            Ledger(path=str(pathlib.Path(tmp) / "paper_ledger.json")),
            market_context=lambda: {
                "condition_id": "cond", "up_token_id": "up", "down_token_id": "down"},
            host="https://public.invalid",
            max_buy_price=.99, start_balance=100,
            account_path=pathlib.Path(tmp) / "paper_account.json",
            audit_path=pathlib.Path(tmp) / "paper_orders.jsonl",
            book_fetch=lambda token: book(token),
            rules_fetch=lambda cid: rules(),
            min_seconds_to_expiry=60, trade_window_seconds=120,
            latency_ms=0,
        )
        timer_mod.unix = lambda *_a, **_k: sampled
        timer_mod.wall = lambda *_a, **_k: sampled
        broker._book_fetch = lambda token: replace(
            book(token),
            timestamp=str(int(sampled * 1000)),
            received_wall=sampled,
        )
        try:
            config.LATE_TRIM_ENABLED = False
            denied = paper_order(broker, amount=2.5, end=end,
                                 pre_submit_guard=None)
            check("T-40 is still closed without late trim",
                  denied is False and "execution interval" in (broker.last_error or ""),
                  broker.last_error)
            denied_kw = broker.place_trade(
                "UP", 2.5, "up", "down", "cond", end, min_expiry=20.0)
            check("min_expiry is refused while the flag is off",
                  denied_kw is False and "late trim is disabled" in (broker.last_error or ""),
                  broker.last_error)
            config.LATE_TRIM_ENABLED = True
            filled = broker.place_trade(
                "UP", 2.5, "up", "down", "cond", end, 0.88,
                min_expiry=config.LATE_TRIM_CUTOFF_SECONDS)
            check("T-40 fills when late trim is on", filled is True, broker.last_error)
        finally:
            config.LATE_TRIM_ENABLED = previous
            timer_mod.unix = original_unix
            timer_mod.wall = original_wall


def main() -> int:
    for test in (t_settlement_paths_use_both_legs,
                 t_trim_buys_only_the_red_favorite,
                 t_trim_does_not_add_to_the_already_green_stacked_side,
                 t_trim_skips_both_green_and_both_red,
                 t_trim_requires_window_signals_and_ask_band,
                 t_ledger_open_inventory_is_per_condition,
                 t_paper_late_trim_floor_opens_t40_only_when_enabled):
        try:
            test()
        except Exception as exc:
            tp.FAIL += 1
            tp.FAILURES.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{tp.PASS} passed, {tp.FAIL} failed")
    for item in tp.FAILURES[:20]:
        print(f"  - {item}")
    return 1 if tp.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
