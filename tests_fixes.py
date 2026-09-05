#!/usr/bin/env python3
"""Tests for the C1 freeze fix and the Chainlink strike.

    python tests_fixes.py
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import threading
import time
import types
from decimal import Decimal

sys.path.insert(0, ".")

# Run against config.py's built-in defaults, never the operator's .env.
#
# Every assertion in this file is written against the defaults. config.py calls
# load_dotenv(override=False), so the suite cannot neutralise a knob by
# deleting it - .env simply repopulates it on the next reload - and knobs whose
# default is computed (MAX_ROUND_EXPOSURE) cannot be pinned to a literal
# either. Stubbing the loader before config is first imported is the only way
# to get all 46 keys at once, and it survives importlib.reload() because
# config re-runs `from dotenv import load_dotenv` each time.
#
# Without this the suite graded whatever the operator had most recently tuned
# instead of the code: a 7-band ladder plus PHASE2_MULTI_SIGNAL=1 turned 22
# tests red, including one that timed out because no order was ever reached.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *_a, **_kw: False

if importlib.util.find_spec("requests") is None:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *_a, **_kw: None
    sys.modules["requests"] = requests_stub

import chainlink_strike as strike_mod  # noqa: E402
import market_discovery  # noqa: E402
from chainlink_strike import (ChainlinkStrike, PING_EVERY, RAW_TOPIC,  # noqa: E402
                              SDK_TOPIC, TWAP_WINDOW, window_start)

P = F = 0


def check(name, cond, detail=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
        print(f"  FAIL {name} {detail}")


# ----------------------------------------------------------------- C1 ---
# threading.active_count() is a noisy global - other libraries start threads
# and a flaky test is worse than no test. Count the blocked workers directly.
BLOCKED = {"n": 0}


def _blocking_wait(ev):
    BLOCKED["n"] += 1
    ev.wait()
    BLOCKED["n"] -= 1


async def t_c1_old_pattern_leaks_threads():
    """The original: wait_for cancels the future, the thread never returns."""
    ev = threading.Event()
    BLOCKED["n"] = 0
    timeouts = 0
    for _ in range(6):
        try:
            await asyncio.wait_for(asyncio.to_thread(_blocking_wait, ev), timeout=0.05)
        except asyncio.TimeoutError:
            timeouts += 1
    check("old pattern times out on every stranded wait", timeouts == 6, str(timeouts))
    check("old pattern strands a worker per attempt", BLOCKED["n"] >= 5,
          f"only {BLOCKED['n']} blocked after 6 attempts")
    ev.set()
    await asyncio.sleep(0.2)
    check("stranded workers only clear when the event fires", BLOCKED["n"] == 0,
          str(BLOCKED["n"]))


async def _new_sleep(seconds, stop_event, tick=0.1):
    """Exactly what main_bot.py now runs."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not stop_event.is_set():
        await asyncio.sleep(tick)


async def t_c1_fix_leaks_nothing():
    ev = threading.Event()
    BLOCKED["n"] = 0
    for _ in range(12):
        await _new_sleep(0.05, ev, tick=0.01)
    check("fixed pattern strands nothing", BLOCKED["n"] == 0, str(BLOCKED["n"]))
    check("fixed pattern uses no worker thread at all", BLOCKED["n"] == 0)


async def t_c1_fix_still_exits_early():
    ev = threading.Event()
    t0 = time.monotonic()
    task = asyncio.create_task(_new_sleep(6.0, ev, tick=0.02))
    await asyncio.sleep(0.1)
    ev.set()
    await task
    dt = time.monotonic() - t0
    check("stop_event still interrupts the wait", dt < 0.5, f"{dt:.2f}s")


async def t_c1_fix_waits_the_full_time():
    ev = threading.Event()
    t0 = time.monotonic()
    await _new_sleep(0.4, ev, tick=0.02)
    dt = time.monotonic() - t0
    check("waits the full duration when not stopped", 0.35 <= dt <= 0.7, f"{dt:.2f}s")


# ------------------------------------------------------------- strike ---
def _msg(value, ts_ms, topic=RAW_TOPIC, symbol="btc/usd",
         twap_window=TWAP_WINDOW, full_accuracy_value=None):
    payload = {"symbol": symbol, "timestamp": ts_ms,
               "value": str(value), "window_s": twap_window}
    if full_accuracy_value is not None:
        payload["full_accuracy_value"] = str(full_accuracy_value)
    return json.dumps({"topic": topic, "type": "update", "timestamp": ts_ms,
                       "payload": payload})


def t_strike_takes_the_exact_boundary_observation():
    s = ChainlinkStrike()
    w = 1786320000                       # a 300-aligned boundary
    check("boundary maths", window_start(w + 17) == w, str(window_start(w + 17)))

    s._handle(_msg(64_894.00, w * 1000))          # first print of the window
    s._handle(_msg(64_910.55, (w + 12) * 1000))   # later prints must not win
    s._handle(_msg(64_870.10, (w + 240) * 1000))
    check("strike is the exact-boundary TWAP observation",
          s.strike_for(w) == Decimal("64894.0"), str(s.strike_for(w)))
    check("latest TWAP still tracked",
          s.value == Decimal("64870.1"), str(s.value))

    s._handle(_msg(65_000.00, (w + 300) * 1000))  # next window
    check("next window gets its own strike", s.strike_for(w + 300) == 65_000.00)
    check("previous window untouched", s.strike_for(w) == 64_894.00)
    check("unseen window returns None, never a guess", s.strike_for(w + 600) is None)


def t_strike_accepts_raw_and_sdk_twap_topics():
    for topic in (RAW_TOPIC, SDK_TOPIC):
        s = ChainlinkStrike()
        w = 1786320000
        s._handle(_msg(1.0, w * 1000, topic=topic))
        check(f"parses {topic}", s.strike_for(w) == 1.0)


def t_strike_prefers_exact_e18_and_rejects_stale_values():
    s = ChainlinkStrike(stale_after=20.0)
    now_ms = int(time.time() * 1000)
    exact = 64894123456789012345678
    s._handle(_msg(1.0, now_ms, full_accuracy_value=exact))
    check("signed E18 value wins over display float",
          s.value == Decimal(exact) / (Decimal(10) ** 18), str(s.value))
    check("new TWAP is live", s.current_value() == s.value)
    s.value_mono -= 21.0
    check("stale TWAP is withheld from the strategy", s.current_value() is None)


def t_out_of_order_packet_cannot_rewind_live_twap():
    s = ChainlinkStrike()
    w = 1786320000
    s._handle(_msg(101, (w + 10) * 1000))
    s._handle(_msg(99, w * 1000))
    check("late delivery can still supply the exact boundary observation",
          s.strike_for(w) == Decimal("99"), str(s.strike_for(w)))
    check("late packet cannot rewind the live TWAP",
          s.value == Decimal("101"), str(s.value))


def t_boundary_plus_one_is_never_used_as_the_opening_twap():
    s = ChainlinkStrike()
    w = 1786320000
    s._handle(_msg(101, (w + 1) * 1000))
    check("boundary+1s is not the official opening observation",
          s.strike_for(w) is None, str(s.strikes))
    s._handle(_msg(99, w * 1000))
    check("an out-of-order exact-boundary packet is accepted",
          s.strike_for(w) == Decimal("99"), str(s.strikes))


def t_strike_ignores_noise():
    s = ChainlinkStrike()
    w = 1786320000
    s._handle("PONG")
    s._handle("not json")
    s._handle(_msg(9.0, w * 1000, topic="crypto_prices_chainlink"))  # spot topic
    s._handle(_msg(9.0, w * 1000, topic="prices.crypto.chainlink")) # SDK spot
    s._handle(_msg(9.0, w * 1000, symbol="eth/usd"))             # wrong symbol
    s._handle(_msg(9.0, w * 1000, twap_window=30))                # wrong TWAP window
    s._handle(_msg("abc", w * 1000))                              # bad value
    check("noise never becomes a strike", s.strike_for(w) is None, str(s.strikes))
    check("PONG does not crash", True)


def t_divergence_reports_the_binance_gap():
    s = ChainlinkStrike()
    w = 1786320000
    s._handle(_msg(64_894.00, w * 1000))
    d = s.divergence(64_901.50, w)
    check("diff computed", abs(d["diff"] - 7.50) < 1e-9, str(d))
    check("bps computed", abs(d["diff_bps"] - 1.1558) < 1e-3, str(d))
    check("no strike means no claim", s.divergence(64_901.50, w + 300)["diff"] is None)
    check("no binance price means no claim", s.divergence(None, w)["diff"] is None)


def t_strike_memory_is_bounded():
    s = ChainlinkStrike()
    w = 1786320000
    for i in range(400):
        s._handle(_msg(1000 + i, (w + i * 300) * 1000))
    check("strike map stays bounded", len(s.strikes) <= 200, str(len(s.strikes)))


def t_strike_default_window_uses_unix_not_a_lagging_clob_clock():
    import timer

    original_wall = timer.wall
    original_unix = timer.unix
    original_time = strike_mod.time.time
    try:
        # CLOB /time has already crossed the boundary; Unix has not.
        # Round identity must stay on Unix or the displayed round overruns
        # and the next market opens late.
        strike_mod.time.time = lambda: 1_786_320_299.8
        timer.unix = lambda *_a, **_k: 1_786_320_299.8
        timer.wall = lambda *_a, **_k: 1_786_320_301.2
        check("RTDS default window follows Unix, not CLOB /time",
              window_start() == 1_786_320_000, str(window_start()))
    finally:
        timer.wall = original_wall
        timer.unix = original_unix
        strike_mod.time.time = original_time


def t_strike_rejects_nonfinite_and_malformed_matching_frames():
    s = ChainlinkStrike()
    w = 1_786_320_000
    s._handle(_msg("Infinity", w * 1000))
    s._handle(_msg("NaN", w * 1000))
    s._handle(json.dumps({"topic": RAW_TOPIC, "type": "update", "payload": [1]}))
    s._handle(123)
    check("non-finite RTDS prices are rejected", s.value is None, str(s.value))
    check("malformed matching RTDS frames are counted", s.invalid_messages == 4,
          str(s.health()))
    s._handle(_msg("100", w * 1000))
    check("a trusted update clears recovered malformed-frame health",
          s.value == Decimal("100") and s.last_error is None, str(s.health()))


async def t_strike_start_is_idempotent():
    s = ChainlinkStrike()

    async def idle():
        await s._stop.wait()

    s._run = idle
    first = s.start()
    second = s.start()
    check("duplicate strike start returns the existing task", first is second)
    check("duplicate strike start creates one named task",
          sum(t.get_name() == "chainlink_strike" for t in asyncio.all_tasks()) == 1)
    await s.stop()
    check("strike stop releases its task reference", s._task is None)


def t_mid_window_reconnect_never_invents_a_boundary_strike():
    s = ChainlinkStrike()
    w = 1786320000
    s._connection_window = w
    s._handle(_msg(64_950.0, (w + 180) * 1000))
    check("mid-window first observation is not called the strike",
          s.strike_for(w) is None, str(s.strikes))
    check("skipped partial window is visible in health",
          s.health()["partial_windows_skipped"] == 1, str(s.health()))
    s._handle(_msg(65_000.0, (w + 300) * 1000))
    check("next full window can be captured",
          s.strike_for(w + 300) == 65_000.0, str(s.strikes))


def t_round_state_cannot_reuse_a_previous_strike():
    source = pathlib.Path("main_bot.py").read_text(encoding="utf-8")
    # Both must be cleared at the transition, but they need not be adjacent -
    # a per-round one-shot flag legitimately sits between them. Match the
    # block rather than the exact two lines, so adding such a flag is not a
    # false failure on an invariant that still holds.
    _transition = source.split("active_window = round_window", 1)[-1][:600]
    check("round transition explicitly clears both start prices",
          "start_price = None" in _transition
          and "start_chainlink_price = None" in _transition)
    check("Binance strike is latched from the exchange timestamp",
          "active_window * 1000 <= ts_ms < (active_window + 5) * 1000" in source)
    check("stale last-known opening fallback was removed entirely",
          "last_known" not in source)
    # Reversed 2026-08-17: paper used to substitute a mid-round price when it
    # missed the open, which measures a different question than the market
    # asks and inverted the signal once price had moved.
    check("paper no longer latches a mid-round reference",
          "PAPER mid-round Binance reference" not in source)
    check("60s TWAP strike is looked up by active round",
          "chainlink_twap_for_round(active_window)" in source)
    check("spot Chainlink aggregator is absent from the decision path",
          "get_chainlink_btc_price" not in source and "import chainlink\n" not in source)
    check("Chainlink signal compares TWAP start with TWAP current",
          "chainlink_signal(\n                active_window, start_chainlink_price, current_cl)" in source and
          "current_cl = current_chainlink_twap()" in source)


async def t_rtds_subscription_matches_the_documented_contract():
    sent = []

    class FakeWS:
        async def send(self, payload):
            sent.append(payload)

    class FakeContext:
        async def __aenter__(self):
            return FakeWS()
        async def __aexit__(self, *_a):
            return False

    original = strike_mod.websockets.connect
    strike_mod.websockets.connect = lambda *_a, **_kw: FakeContext()
    try:
        service = ChainlinkStrike()
        service._stop.set()  # send the subscription, then leave immediately
        await service._session()
    finally:
        strike_mod.websockets.connect = original

    frame = json.loads(sent[0])
    check("RTDS heartbeat interval is five seconds", PING_EVERY == 5.0, str(PING_EVERY))
    check("one canonical Chainlink subscription is sent",
          len(frame["subscriptions"]) == 1, str(frame))
    sub = frame["subscriptions"][0]
    # The market's resolution text names the 60-second stream as the
    # settlement source, so subscribing to the 30-second one models a
    # different market.
    check("the settlement TWAP topic and update type match RTDS",
          sub["topic"] == "crypto_prices_twap_sixty" and sub["type"] == "update",
          str(sub))
    check("Chainlink symbol is encoded in filters",
          json.loads(sub["filters"]) == {"symbol": "btc/usd"}, str(sub))


def t_market_tokens_are_mapped_by_outcome_and_tradeability():
    event = {
        "slug": "btc-updown-5m-12300",
        "closed": False,
        "active": True,
        "markets": [
            {"closed": True, "active": True, "acceptingOrders": True,
             "outcomes": ["Up", "Down"], "clobTokenIds": ["closed-up", "closed-down"]},
            {"id": "12300", "closed": False, "active": True,
             "acceptingOrders": True, "enableOrderBook": True,
             "eventStartTime": "1970-01-01T03:25:00Z",
             "endDate": "1970-01-01T03:30:00Z",
             "cryptoMarketConfig": {
                 "asset": "btc", "duration": "5m", "twapEnabled": True,
                 "twapLookbackSeconds": 60,
             },
             "conditionId": "0x" + "a" * 64,
             "outcomes": json.dumps(["Down", "Up"]),
             "clobTokenIds": json.dumps(["202", "101"])},
        ],
    }
    parsed = market_discovery._parse_event(event, 12300)
    check("closed market is skipped",
          parsed["condition_id"] == "0x" + "a" * 64, str(parsed))
    check("UP token follows its label, not index zero",
          parsed["up_token_id"] == "101", str(parsed))
    check("DOWN token follows its label",
          parsed["down_token_id"] == "202", str(parsed))

    event["markets"][1]["acceptingOrders"] = False
    check("market refusing orders is rejected",
          market_discovery._parse_event(event, 12300) is None)


def t_market_discovery_requires_the_declared_60s_twap_contract():
    market = {
        "id": "12300", "closed": False, "active": True,
        "acceptingOrders": True, "enableOrderBook": True,
        "eventStartTime": "1970-01-01T03:25:00Z",
        "endDate": "1970-01-01T03:30:00Z",
        "conditionId": "0x" + "a" * 64,
        "outcomes": ["Up", "Down"], "clobTokenIds": ["101", "202"],
        "cryptoMarketConfig": {
            "asset": "btc", "duration": "5m", "twapEnabled": True,
            "twapLookbackSeconds": 60,
        },
    }
    event = {"slug": "btc-updown-5m-12300", "closed": False,
             "active": True, "markets": [market]}
    check("the current BTC/5m/60s TWAP contract is accepted",
          market_discovery._parse_event(event, 12300) is not None)

    valid = dict(market["cryptoMarketConfig"])
    cases = (
        ("missing crypto config", None),
        ("malformed crypto config", "not-an-object"),
        ("30-second TWAP", {**valid, "twapLookbackSeconds": 30}),
        ("spot market", {**valid, "twapEnabled": False}),
        ("wrong asset", {**valid, "asset": "eth"}),
        ("wrong duration", {**valid, "duration": "15m"}),
        ("string lookback", {**valid, "twapLookbackSeconds": "60"}),
        ("boolean lookback", {**valid, "twapLookbackSeconds": True}),
    )
    for name, config in cases:
        if config is None:
            market.pop("cryptoMarketConfig", None)
        else:
            market["cryptoMarketConfig"] = config
        check(f"{name} is rejected before token discovery",
              market_discovery._parse_event(event, 12300) is None,
              str(config))
    market["cryptoMarketConfig"] = valid


def t_market_discovery_never_falls_back_to_a_closed_round():
    calls = []
    current = market_discovery._current_5m_window_start_unix()
    original = market_discovery._fetch_slug
    market_discovery._fetch_slug = lambda slug: calls.append(slug) or None
    try:
        check("missing current round returns no tokens",
              market_discovery.get_btc_5m_tokens(current) is None)
        check("expired previous round is rejected before any API lookup",
              market_discovery.get_btc_5m_tokens(current - 300) is None)
    finally:
        market_discovery._fetch_slug = original
    check("only the requested round was queried",
          calls == [f"btc-updown-5m-{current}"], str(calls))


def t_order_failures_read_the_live_error_value():
    source = pathlib.Path("main_bot.py").read_text(encoding="utf-8")
    check("main bot reads the module's current order error",
          "polymarket_trade.last_order_error" in source)
    check("main bot no longer imports a stale immutable error value",
          "get_balance_allowance, last_order_error" not in source)


def t_paper_startup_does_not_abort_on_measured_clock_drift():
    source = pathlib.Path("main_bot.py").read_text(encoding="utf-8")
    check("live mode still fail-closes on unverified clock",
          'if mode == "LIVE":' in source
          and "CLOB clock synchronization could not be verified" in source)
    check("paper mode keeps Unix round windows when CLOB drift is large",
          "PAPER continues" in source
          and "Unix 5-minute windows" in source)
    check("strategy samples Unix time for round identity",
          "sampled_wall = timer.unix()" in source)


class _ClockResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def t_clock_offset_aligns_round_identity_to_clob():
    import timer
    timer.reset_clock_cache()
    server_ahead = 2.363
    orig = timer.requests.get

    def fake_get(*_a, **_k):
        return _ClockResp(time.time() + server_ahead)

    timer.requests.get = fake_get
    try:
        ok, detail, drift = timer.check_clock("https://clob.example", 2.0, cache_s=0)
        check("measured drift beyond 2s is not within live tolerance",
              ok is False, detail)
        check("local-behind server produces negative drift",
              drift is not None and drift < -2.0, str(drift))
        check("offset is stored for CLOB-aligned wall()",
              timer.clock_measured() and abs(timer.clock_offset() - drift) < 1e-9)
        residual = timer.wall() - (time.time() + server_ahead)
        check("wall() tracks CLOB time within a few hundred ms",
              abs(residual) < 0.25, f"{residual:.4f}s")
        check("window_start ignores CLOB offset and stays on Unix",
              timer.window_start() == timer.window_start(time.time()))
        explicit = 1_786_320_017.4
        check("explicit timestamps are not shifted",
              timer.window_start(explicit) == 1_786_320_000)
        check("seconds_left uses the supplied sample",
              timer.seconds_left(explicit) == 283)
    finally:
        timer.requests.get = orig
        timer.reset_clock_cache()
    check("reset clears the measured offset",
          not timer.clock_measured() and timer.clock_offset() == 0.0)


def t_binance_and_fresh_snapshot_follow_clob_time():
    import json
    import price_ws
    import timer
    from feeds.binance import BinanceTradeFeed

    timer.reset_clock_cache()
    orig = timer.requests.get
    orig_price = price_ws.latest_price
    orig_mono = price_ws.latest_price_mono
    orig_ts = price_ws.latest_price_ts_ms
    orig_id = price_ws.latest_trade_id
    timer.requests.get = lambda *_a, **_k: _ClockResp(time.time() + 3.5)
    try:
        timer.check_clock("https://clob.example", 2.0, cache_s=0)
        trade_ms = int(time.time() * 1000) + 3500
        feed = BinanceTradeFeed()
        feed._handle(json.dumps({"e": "trade", "p": "64123.50", "T": trade_ms}))
        check("binance keeps a print that is future-dated on the local clock",
              feed.price == 64123.50, str(feed.health.detail))
        published = price_ws.publish_price(64123.50, exchange_ts_ms=trade_ms)
        check("price bus accepts the CLOB-aligned print", published is True)
        fresh, ts = price_ws.fresh_snapshot(3.0)
        check("fresh_snapshot uses CLOB time, not the lagging local clock",
              fresh == 64123.50 and ts == trade_ms, str((fresh, ts)))
        price_ws.latest_price = None
        price_ws.latest_price_mono = None
        price_ws.latest_price_ts_ms = None
        price_ws.latest_trade_id = None
        past_feed = BinanceTradeFeed()
        past_ms = int(timer.wall() * 1000) - 3200
        past_feed._handle(json.dumps({"e": "trade", "p": "64124.00", "T": past_ms, "t": 2}))
        check("binance keeps a print a few seconds behind CLOB time",
              past_feed.price == 64124.00, str(past_feed.health.detail))
        published_past = price_ws.publish_price(64124.00, exchange_ts_ms=past_ms, trade_id=2)
        check("price bus accepts a cross-venue-aged print", published_past is True)
        fresh_past, ts_past = price_ws.fresh_snapshot(3.0)
        check("fresh_snapshot does not treat CLOB/Binance skew as local staleness",
              fresh_past == 64124.00 and ts_past == past_ms, str((fresh_past, ts_past)))
    finally:
        timer.requests.get = orig
        timer.reset_clock_cache()
        price_ws.latest_price = orig_price
        price_ws.latest_price_mono = orig_mono
        price_ws.latest_price_ts_ms = orig_ts
        price_ws.latest_trade_id = orig_id


def t_clock_check_failure_does_not_clear_last_offset():
    import timer
    timer.reset_clock_cache()
    orig = timer.requests.get
    timer.requests.get = lambda *_a, **_k: _ClockResp(time.time() + 0.05)
    try:
        ok, _detail, drift = timer.check_clock("https://clob.example", 2.0, cache_s=0)
        check("small drift is within tolerance", ok is True, str(drift))
        stored = timer.clock_offset()
        timer.requests.get = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down"))
        ok2, detail2, drift2 = timer.check_clock("https://clob.example", 2.0, cache_s=0)
        check("network failure is fail-closed", ok2 is False and drift2 is None, detail2)
        check("last good offset is kept after a failed refresh",
              timer.clock_measured() and abs(timer.clock_offset() - stored) < 1e-9)
    finally:
        timer.requests.get = orig
        timer.reset_clock_cache()


def t_clock_cache_is_invalidated_by_a_local_wall_clock_jump():
    import timer

    timer.reset_clock_cache()
    original = timer.requests.get
    calls = []

    def fake_get(*_a, **_k):
        calls.append(True)
        return _ClockResp(time.time())

    timer.requests.get = fake_get
    try:
        timer.check_clock("https://clob.example", 2.0, cache_s=30)
        timer.check_clock("https://clob.example", 2.0, cache_s=30)
        check("stable local clock uses the cached CLOB measurement",
              len(calls) == 1, str(calls))
        # Model a ten-second wall-clock adjustment without waiting in real
        # time.  Monotonic elapsed time has not moved with it.
        timer._clock_sample_wall -= 10.0
        timer.check_clock("https://clob.example", 2.0, cache_s=30)
        check("wall-clock jump forces an immediate CLOB recheck",
              len(calls) == 2, str(calls))
    finally:
        timer.requests.get = original
        timer.reset_clock_cache()


# ------------------------------------------------------ one-sided books ---
# In the last minute of a five-minute round the winning token keeps only bids
# and the losing token only asks.  The book vote must abstain there instead of
# naming the token nobody is offering, and the loop must find that side
# unbuyable before it pays for a submission that cannot fill.
def t_one_sided_book_casts_no_vote():
    import orderbook

    check("two-sided book still compares depth",
          orderbook.liquidity_signal([{"price": "0.4", "size": "10"}],
                                     [{"price": "0.6", "size": "20"}]) == "DOWN")
    check("winning token (bids only) casts no vote",
          orderbook.liquidity_signal([{"price": "0.99", "size": "12783"}], []) is None)
    check("losing token (asks only) casts no vote",
          orderbook.liquidity_signal([], [{"price": "0.01", "size": "12796"}]) is None)
    check("empty book still returns None", orderbook.liquidity_signal([], []) is None)
    check("zero-size levels count as an absent side",
          orderbook.liquidity_signal([{"price": "0.99", "size": "12783"}],
                                     [{"price": "0.5", "size": "0"}]) is None)


def t_unbuyable_side_is_refused_before_submission():
    import orderbook

    original = orderbook.get_orderbook

    def gate(bids, asks, min_price=0.0):
        """Return the refusal reason for this book shape, or None if buyable."""
        orderbook.get_orderbook = lambda *_a, **_kw: (bids, asks)
        try:
            orderbook.validate_buy_liquidity("1", 5.0, 0.99, 0.25, min_price=min_price)
        except ValueError as exc:
            return str(exc)
        return None

    try:
        check("winning side with no offers is refused",
              gate([{"price": "0.99", "size": "12783"}], []) == "selected token has no asks")
        check("losing side with no bid is refused",
              "no bids" in (gate([], [{"price": "0.01", "size": "12796"}]) or ""))
        check("an ask above MAX_BUY_PRICE is refused",
              "exceeds MAX_BUY_PRICE" in (gate([{"price": "0.99", "size": "5"}],
                                               [{"price": "0.995", "size": "5"}]) or ""))
        check("an ask below MIN_BUY_PRICE is refused",
              "below MIN_BUY_PRICE" in (gate([{"price": "0.19", "size": "100"}],
                                             [{"price": "0.08", "size": "100"}],
                                             min_price=0.20) or ""))
        check("depth below the bet size is refused",
              "only $" in (gate([{"price": "0.40", "size": "100"}],
                                [{"price": "0.50", "size": "2"}]) or ""))
        check("a two-sided book with real depth passes",
              gate([{"price": "0.49", "size": "100"}],
                   [{"price": "0.50", "size": "100"}]) is None)
        check("a book inside the 0.20-0.90 band passes the min floor",
              gate([{"price": "0.49", "size": "100"}],
                   [{"price": "0.50", "size": "100"}], min_price=0.20) is None)
    finally:
        orderbook.get_orderbook = original


def t_orderbook_rejects_nonfinite_controls_before_network_io():
    import orderbook

    original = orderbook.get_orderbook
    calls = []
    orderbook.get_orderbook = lambda *_a, **_k: calls.append(True) or ([], [])
    try:
        for name, kwargs in (
                ("amount", {"amount": float("nan")}),
                ("max price", {"max_price": float("inf")}),
                ("spread", {"max_spread": float("nan")}),
                ("minimum", {"min_price": -0.1})):
            args = {"token_id": "1", "amount": 5.0, "max_price": 0.9,
                    "max_spread": 0.25, "min_price": 0.2}
            args.update(kwargs)
            try:
                orderbook.validate_buy_liquidity(**args)
            except ValueError:
                check(f"invalid {name} is rejected", True)
            else:
                check(f"invalid {name} is rejected", False)
    finally:
        orderbook.get_orderbook = original
    check("invalid controls are rejected before a CLOB request", not calls, str(calls))

    now = time.time()
    data = {"asset_id": "1", "timestamp": str(int(now * 1000)),
            "bids": [{"price": "0.4", "size": "1"}], "asks": []}
    for label, kwargs in (
            ("nonfinite now", {"now": float("nan")}),
            ("nonfinite age limit", {"now": now, "max_age_s": float("nan")})):
        try:
            orderbook.parse_orderbook(data, "1", **kwargs)
        except ValueError:
            check(f"{label} cannot bypass freshness validation", True)
        else:
            check(f"{label} cannot bypass freshness validation", False)


def t_orderbook_retries_one_transient_read_then_validates_identity():
    import orderbook
    import timer

    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"asset_id": "123", "timestamp": str(int(timer.wall() * 1000)),
                    "bids": [{"price": "0.4", "size": "2"}],
                    "asks": [{"price": "0.5", "size": "2"}]}

    calls = []
    # orderbook now reads through the pooled session, so the stub goes on
    # http_pool.get rather than requests.get.
    original_get = orderbook.http_pool.get
    original_sleep = orderbook.time.sleep
    try:
        def fake_get(*_a, **_k):
            calls.append(True)
            if len(calls) == 1:
                raise orderbook.requests.Timeout("temporary")
            return Resp()

        orderbook.http_pool.get = fake_get
        orderbook.time.sleep = lambda *_a: None
        bids, asks = orderbook.get_orderbook("123")
        check("one transient CLOB book failure is retried", len(calls) == 2, str(calls))
        check("retried CLOB book is still strictly parsed",
              bids[0]["price"] == "0.4" and asks[0]["price"] == "0.5")
    finally:
        orderbook.http_pool.get = original_get
        orderbook.time.sleep = original_sleep


def t_market_discovery_backs_off_and_retries_rate_limits():
    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"Retry-After": "0.01"}

        def raise_for_status(self):
            if self.status_code >= 400:
                exc = market_discovery.requests.HTTPError("rate limited")
                exc.response = self
                raise exc

        def json(self):
            return {"slug": "btc-updown-5m-12300"}

    calls, sleeps = [], []
    # market_discovery reads through the pooled session now.
    original_get = market_discovery.http_pool.get
    original_sleep = market_discovery.time.sleep
    try:
        market_discovery.http_pool.get = lambda *_a, **_k: (
            calls.append(True) or Resp(429 if len(calls) == 1 else 200))
        market_discovery.time.sleep = lambda delay: sleeps.append(delay)
        event = market_discovery._fetch_slug("btc-updown-5m-12300")
        check("Gamma 429 is retried exactly once", len(calls) == 2, str(calls))
        check("Gamma rate-limit retry uses bounded backoff",
              len(sleeps) == 1 and 0.05 <= sleeps[0] <= 1.0, str(sleeps))
        check("Gamma retry returns only the exact requested slug",
              event and event["slug"] == "btc-updown-5m-12300", str(event))
    finally:
        market_discovery.http_pool.get = original_get
        market_discovery.time.sleep = original_sleep

    check("overflowing market windows fail closed",
          market_discovery.get_btc_5m_tokens(float("inf")) is None)


async def t_transient_unfillable_book_is_retried_next_attempt():
    """A temporary empty/spread book must not blacklist a side for 5 minutes."""
    import main_bot

    active = 1_786_320_000
    calls = {"probe": 0, "orders": 0}
    rows = []

    class Strike:
        def strike_for(self, _window):
            return Decimal("100")

        def current_value(self):
            return Decimal("100")

        def divergence(self, *_a):
            return {"diff": None}

    bids = [{"price": "0.49", "size": "20"}]
    asks = [{"price": "0.50", "size": "10"}]

    def probe(*_a, **_k):
        calls["probe"] += 1
        if calls["probe"] == 1:
            raise ValueError("temporary empty ask side")
        return bids, asks

    def submit(*_a, **_k):
        calls["orders"] += 1
        main_bot.stop_event.set()
        return True

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    try:
        replace(main_bot, "execution_mode", "PAPER")
        replace(main_bot, "_paper_broker", object())
        replace(main_bot, "_round_exposure_provider", None)
        replace(main_bot, "_round_held_tokens_provider", None)
        replace(main_bot, "_execution_ready_provider", None)
        replace(main_bot, "_strike", Strike())
        replace(main_bot, "get_balance_allowance",
                lambda: {"balance": 1000.0, "allowance": 1000.0})
        replace(main_bot, "place_trade", submit)
        replace(main_bot, "_append_trade", lambda row: rows.append(row))
        replace(main_bot.polymarket_trade, "live_execution_disabled", lambda: True)
        replace(main_bot.market_discovery, "get_tokens_for_current_round", lambda _w: {
            "window_start": active, "window_end": active + 300,
            "up_token_id": "11", "down_token_id": "12",
            "orderbook_token_id": "11", "condition_id": "0x" + "a" * 64,
        })
        replace(main_bot.orderbook, "get_orderbook", lambda *_a, **_k: (bids, asks))
        replace(main_bot.orderbook, "validate_buy_liquidity", probe)
        # Stamp the print inside the opening 5s so the boundary strike latches
        # legitimately. It used to sit at +100s and rely on the PAPER
        # mid-round fallback, which no longer exists.
        replace(main_bot.price_ws, "latest_snapshot",
                lambda: (100.0, time.monotonic(), (active + 1) * 1000))
        replace(main_bot.price_ws, "fresh_snapshot",
                lambda *_a, **_k: (101.0, (active + 100) * 1000))
        replace(main_bot.timer, "unix", lambda *_a, **_k: active + 100.0)
        replace(main_bot.timer, "wall", lambda *_a, **_k: active + 100.0)
        replace(main_bot.timer, "check_clock",
                lambda *_a, **_k: (True, "clock synchronized", 0.0))
        replace(main_bot.config, "TRADE_INTERVAL_SECONDS", 0.01)
        replace(main_bot.config, "TRADE_LAST_SECONDS", 300)
        replace(main_bot.config, "MAX_ROUND_EXPOSURE", 100.0)
        replace(main_bot.config, "CANCEL_OPEN_BEFORE_TRADE", False)
        replace(main_bot.config, "PHASE2_ENABLED", True)
        main_bot.stop_event.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            await asyncio.wait_for(main_bot.run_bot(), timeout=1.5)
    finally:
        main_bot.stop_event.set()
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        main_bot.stop_event.clear()

    check("transient unfillable side is probed again", calls["probe"] == 2, str(calls))
    check("replenished book can submit on the next attempt", calls["orders"] == 1,
          str(calls))
    check("the failed attempt remains visible in the journal",
          rows and rows[0]["result"] == "skipped_unfillable", str(rows))


def _book(ask):
    return ([{"price": f"{ask - 0.01:.2f}", "size": "40"}],
            [{"price": f"{ask:.2f}", "size": "40"}])


async def _drive_phase2_with_hold(*, execution_mode, held_provider,
                                  execution_ready_provider=None,
                                  timeout=0.55, price_votes=None,
                                  book_votes=None, chainlink_vote="DOWN",
                                  chainlink_votes=None,
                                  chainlink_start=100, chainlink_current=101,
                                  diagnostic_side="DOWN",
                                  stop_after_vote=None,
                                  stop_after_book_vote=None,
                                  stop_after_orders=1,
                                  allow_signal_flips=False,
                                  minority_rule=False,
                                  price_fallback=False,
                                  partial_signals=False,
                                  must_fire=False):
    """Drive a forced DOWN signal through phase 2 after a restart."""
    import main_bot

    active = 1_786_320_000
    seen = {"orders": 0, "probes": 0, "price_votes": 0, "order_sides": [],
            "book_votes": 0, "chainlink_votes": 0, "executor_guards": []}
    votes = list(price_votes or ())
    books = list(book_votes or ("DOWN",))
    chains = list(chainlink_votes or ())
    bids, asks = _book(0.50)

    class Strike:
        def strike_for(self, _window):
            return (None if chainlink_start is None
                    else Decimal(str(chainlink_start)))

        def current_value(self):
            return (None if chainlink_current is None
                    else Decimal(str(chainlink_current)))

        def divergence(self, *_a):
            return {"diff": None}

    def submit(side, *_args, **kwargs):
        guard = kwargs.get("pre_submit_guard")
        allowed = guard() if callable(guard) else None
        seen["executor_guards"].append(allowed)
        if allowed is not True:
            main_bot.stop_event.set()
            return False
        seen["orders"] += 1
        seen["order_sides"].append(side)
        if (stop_after_orders is not None
                and seen["orders"] >= stop_after_orders):
            main_bot.stop_event.set()
        return True

    def tagged_price_signal(_round, _start, _current):
        index = min(seen["price_votes"], len(votes) - 1)
        seen["price_votes"] += 1
        vote = votes[index]
        if stop_after_vote is not None and seen["price_votes"] >= stop_after_vote:
            main_bot.stop_event.set()
        return vote

    def probe(*_args, **_kwargs):
        seen["probes"] += 1
        return bids, asks

    def tagged_book_signal(*_args, **_kwargs):
        index = min(seen["book_votes"], len(books) - 1)
        seen["book_votes"] += 1
        vote = books[index]
        if (stop_after_book_vote is not None
                and seen["book_votes"] >= stop_after_book_vote):
            main_bot.stop_event.set()
        return vote

    def tagged_chainlink_signal(*_args, **_kwargs):
        index = min(seen["chainlink_votes"], len(chains) - 1)
        seen["chainlink_votes"] += 1
        return chains[index]

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    try:
        is_paper = execution_mode == "PAPER"
        replace(main_bot, "execution_mode", execution_mode)
        replace(main_bot, "_paper_broker", object() if is_paper else None)
        replace(main_bot, "_round_exposure_provider",
                lambda _window, _condition: 0.0)
        replace(main_bot, "_round_held_tokens_provider", held_provider)
        replace(main_bot, "_execution_ready_provider",
                execution_ready_provider or (lambda _condition: True))
        replace(main_bot, "_strike", Strike())
        replace(main_bot, "get_balance_allowance",
                lambda: {"balance": 1000.0, "allowance": 1000.0})
        replace(main_bot, "place_trade", submit)
        replace(main_bot, "_append_trade", lambda _row: None)
        replace(main_bot.polymarket_trade, "live_execution_disabled", lambda: is_paper)
        replace(main_bot.market_discovery, "get_tokens_for_current_round", lambda _w: {
            "window_start": active, "window_end": active + 300,
            "up_token_id": "11", "down_token_id": "12",
            "orderbook_token_id": "11", "condition_id": "0x" + "a" * 64,
        })
        replace(main_bot.orderbook, "get_orderbook", lambda *_a, **_k: (bids, asks))
        replace(main_bot.orderbook, "liquidity_signal", tagged_book_signal)
        replace(main_bot.orderbook, "validate_buy_liquidity", probe)
        replace(main_bot.strategy, "decide", lambda *_a, **_k: chainlink_vote)
        replace(main_bot.strategy, "final_decision",
                lambda *_a, **_k: diagnostic_side)
        if votes:
            replace(main_bot, "price_signal", tagged_price_signal)
        if chains:
            replace(main_bot, "chainlink_signal", tagged_chainlink_signal)
        replace(main_bot.price_ws, "latest_snapshot",
                lambda: (100.0, time.monotonic(), (active + 1) * 1000))
        replace(main_bot.price_ws, "fresh_snapshot",
                lambda *_a, **_k: (101.0, (active + 100) * 1000))
        replace(main_bot.timer, "unix", lambda *_a, **_k: active + 100.0)
        replace(main_bot.timer, "wall", lambda *_a, **_k: active + 100.0)
        replace(main_bot.timer, "check_clock",
                lambda *_a, **_k: (True, "clock synchronized", 0.0))
        replace(main_bot.config, "PHASE2_ENABLED", True)
        replace(main_bot.config, "TRADE_INTERVAL_SECONDS", 0.01)
        replace(main_bot.config, "TRADE_LAST_SECONDS", 300)
        replace(main_bot.config, "MAX_ROUND_EXPOSURE", 100.0)
        replace(main_bot.config, "CANCEL_OPEN_BEFORE_TRADE", False)
        replace(main_bot.config, "PAPER_ALLOW_SIGNAL_FLIPS", allow_signal_flips)
        replace(main_bot.config, "SIGNAL_MINORITY_RULE", minority_rule)
        replace(main_bot.config, "SIGNAL_PRICE_FALLBACK_COMBINED",
                price_fallback)
        replace(main_bot.config, "PHASE2_MULTI_SIGNAL", False)
        replace(main_bot.config, "PHASE2_PARTIAL_SIGNALS", partial_signals)
        replace(main_bot.config, "PHASE2_MUST_FIRE", must_fire)
        replace(main_bot.config, "LIVE_ALLOW_SIGNAL_FLIPS", allow_signal_flips)
        main_bot.stop_event.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(main_bot.run_bot(), timeout=timeout)
    finally:
        main_bot.stop_event.set()
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        main_bot.stop_event.clear()
    return seen


async def t_restart_held_up_blocks_phase2_down_in_paper_and_live():
    condition = "0x" + "a" * 64
    paper_calls = []

    def paper_held(window, discovered_condition):
        paper_calls.append((window, discovered_condition))
        return {"11"} if discovered_condition == condition else set()

    paper = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=paper_held)
    check("paper phase 2 refreshes condition-backed held tokens",
          any(discovered == condition for _window, discovered in paper_calls),
          str(paper_calls))
    check("paper restart blocks phase-2 DOWN complement before liquidity/submit",
          paper["orders"] == 0 and paper["probes"] == 0, str(paper))

    live_calls = []

    def live_held(window, discovered_condition):
        live_calls.append((window, discovered_condition))
        return {"11"}

    live = await _drive_phase2_with_hold(
        execution_mode="LIVE", held_provider=live_held)
    check("live phase 2 restores window-backed held tokens at round start",
          live_calls and live_calls[0][1] is None, str(live_calls))
    check("live restart blocks phase-2 DOWN complement before liquidity/submit",
          live["orders"] == 0 and live["probes"] == 0, str(live))


async def t_phase2_must_fire_places_instead_of_skipping():
    complement = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: {"11"},
        must_fire=True)
    check("must-fire places the complement instead of holding the other leg",
          complement["order_sides"] == ["DOWN"] and complement["orders"] == 1,
          str(complement))

    live_complement = await _drive_phase2_with_hold(
        execution_mode="LIVE", held_provider=lambda *_a: {"11"},
        must_fire=True)
    check("must-fire places the live complement instead of skipping",
          live_complement["order_sides"] == ["DOWN"]
          and live_complement["orders"] == 1, str(live_complement))

    flipped = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("DOWN", "UP"), must_fire=True)
    check("must-fire retargets a validation flip and still places",
          flipped["order_sides"] == ["UP"] and flipped["orders"] == 1,
          str(flipped))


async def t_phase2_price_signal_is_the_only_order_side_authority():
    matching = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("DOWN", "DOWN", "DOWN"), diagnostic_side="UP")
    check("diagnostic consensus cannot override fresh SIG PRICE",
          matching["order_sides"] == ["DOWN"] and matching["price_votes"] >= 4
          and matching["executor_guards"] == [True],
          str(matching))

    neutral = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=(None,), stop_after_vote=1)
    check("phase 2 skips a neutral initial SIG PRICE",
          neutral["orders"] == 0, str(neutral))

    final_flip = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("DOWN", "UP"), stop_after_vote=2)
    check("phase 2 skips a SIG PRICE flip during final validation",
          final_flip["orders"] == 0 and final_flip["price_votes"] >= 2,
          str(final_flip))

    submit_stale = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("DOWN", "DOWN", None), stop_after_vote=3)
    check("phase 2 skips stale SIG PRICE immediately before submission",
          submit_stale["orders"] == 0 and submit_stale["price_votes"] >= 3,
          str(submit_stale))


async def t_price_first_authority_falls_back_only_when_primary_is_missing():
    primary = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_fallback=True, price_votes=("UP",) * 8,
        book_votes=("DOWN",) * 8, chainlink_vote="DOWN")
    check("usable SIG PRICE wins over an agreeing opposite fallback pair",
          primary["order_sides"] == ["UP"]
          and primary["executor_guards"] == [True], str(primary))

    fallback = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_fallback=True, price_votes=(None,) * 8,
        book_votes=("DOWN",) * 8, chainlink_vote="DOWN")
    check("missing SIG PRICE follows agreeing BOOK and CHAINLINK",
          fallback["order_sides"] == ["DOWN"]
          and fallback["executor_guards"] == [True], str(fallback))

    split = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_fallback=True, price_votes=(None,),
        book_votes=("DOWN",), chainlink_vote="UP",
        stop_after_vote=1)
    check("missing SIG PRICE abstains when BOOK and CHAINLINK disagree",
          split["orders"] == 0, str(split))

    late_primary = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_fallback=True,
        price_votes=(None, None, None, "UP"),
        book_votes=("DOWN",) * 8, chainlink_vote="DOWN")
    check("a late opposite SIG PRICE cancels the fallback at commit",
          late_primary["orders"] == 0
          and late_primary["executor_guards"] == [False],
          str(late_primary))


async def t_minority_rule_is_the_order_authority_at_every_recheck():
    stable = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        minority_rule=True, price_votes=("UP",) * 8,
        book_votes=("DOWN",) * 8, chainlink_vote="UP")
    check("stable BOOK minority can pass every gate and place DOWN",
          stable["order_sides"] == ["DOWN"]
          and stable["executor_guards"] == [True], str(stable))

    guard_flip = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        minority_rule=True, price_votes=("UP",) * 8,
        book_votes=("DOWN",) * 8,
        chainlink_votes=("UP", "UP", "UP", "DOWN"))
    check("commit guard rejects a minority flip during modeled latency",
          guard_flip["orders"] == 0
          and guard_flip["executor_guards"] == [False], str(guard_flip))

    changed = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        minority_rule=True, price_votes=("UP",) * 8,
        book_votes=("UP", "DOWN", "DOWN"), chainlink_vote="UP",
        stop_after_book_vote=2)
    check("a final minority flip aborts instead of submitting the old side",
          changed["orders"] == 0 and changed["book_votes"] >= 2,
          str(changed))

    restarted = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: {"11"},
        minority_rule=True, price_votes=("UP",) * 8,
        book_votes=("DOWN",) * 8, chainlink_vote="UP",
        allow_signal_flips=True)
    check("restart cannot treat its first minority sample as a verified flip",
          restarted["orders"] == 0, str(restarted))


async def t_partial_signals_are_honoured_by_final_rechecks():
    partial = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        minority_rule=True, partial_signals=True,
        price_votes=("DOWN",) * 8,
        book_votes=("DOWN",) * 8, chainlink_vote=None,
        chainlink_start=None, chainlink_current=None)
    check("missing Chainlink may abstain through submission in partial mode",
          partial["order_sides"] == ["DOWN"]
          and partial["executor_guards"] == [True], str(partial))


async def t_paper_signal_flip_mode_keeps_repeats_and_allows_verified_flip():
    repeat = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 8, stop_after_orders=2,
        allow_signal_flips=True, timeout=1.0)
    check("PAPER signal mode retains same-side cadence entries",
          repeat["order_sides"] == ["UP", "UP"], str(repeat))

    flipped = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN",) * 4,
        stop_after_orders=2, allow_signal_flips=True, timeout=1.0)
    check("PAPER signal mode accepts UP then DOWN after a stable epoch change",
          flipped["order_sides"] == ["UP", "DOWN"], str(flipped))
    check("both accepted sides retain executor-side fresh-price guards",
          flipped["executor_guards"] == [True, True], str(flipped))


async def t_paper_signal_flip_restart_baseline_requires_a_later_transition():
    no_transition = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: {"11"},
        price_votes=("DOWN", "DOWN"), stop_after_vote=2,
        allow_signal_flips=True)
    check("current opposite side is not credited as a restart-time transition",
          no_transition["orders"] == 0, str(no_transition))

    recovered = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: {"11"},
        # Initial DOWN is blocked. A later observed UP establishes the restored
        # side, and only the following UP->DOWN epoch may buy the complement.
        price_votes=("DOWN",) * 2 + ("UP",) * 4 + ("DOWN",) * 4,
        stop_after_orders=2, allow_signal_flips=True, timeout=1.2)
    check("unique durable leg permits only a later verified flip",
          recovered["order_sides"] == ["UP", "DOWN"], str(recovered))

    ambiguous = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: {"11", "12"},
        price_votes=("UP", "UP"), stop_after_vote=2,
        allow_signal_flips=True)
    check("both-token restart fails closed because accepted order is ambiguous",
          ambiguous["orders"] == 0, str(ambiguous))


async def t_signal_flip_mode_requires_each_new_edge_once_both_legs_are_held():
    # UP, then verified DOWN, then another DOWN with no new edge: only two.
    no_new_edge = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN",) * 6,
        stop_after_vote=10, stop_after_orders=None,
        allow_signal_flips=True, timeout=1.0)
    check("both-held state rejects a repeat without another signal epoch",
          no_new_edge["order_sides"] == ["UP", "DOWN"], str(no_new_edge))

    # After UP->DOWN is accepted, observe UP but reject it in-flight, then a
    # later DOWN is an away-then-back epoch and may enter again.
    away_back = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN",) * 4
        + ("UP", "DOWN") + ("DOWN",) * 4,
        stop_after_orders=3, allow_signal_flips=True, timeout=1.2)
    check("away-then-back transition can authorize the accepted side again",
          away_back["order_sides"] == ["UP", "DOWN", "DOWN"], str(away_back))


async def t_signal_flip_mode_never_loosens_live_or_inflight_guards():
    live = await _drive_phase2_with_hold(
        execution_mode="LIVE", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN",) * 2,
        stop_after_vote=6, stop_after_orders=None,
        allow_signal_flips=True, timeout=0.8)
    check("PAPER flip flag cannot authorize a LIVE complement",
          live["order_sides"] == ["UP"], str(live))

    neutral = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=(None,), stop_after_vote=1,
        allow_signal_flips=True)
    check("neutral SIG PRICE never creates a signal epoch or order",
          neutral["orders"] == 0, str(neutral))

    inflight = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP", "DOWN"), stop_after_vote=2,
        allow_signal_flips=True)
    check("an in-flight price flip still rejects the whole attempt",
          inflight["orders"] == 0, str(inflight))

    stale_gap = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN", None, "DOWN", "DOWN"),
        stop_after_vote=8, stop_after_orders=None,
        allow_signal_flips=True, timeout=0.8)
    check("transition followed by neutral cannot be consumed when side returns",
          stale_gap["order_sides"] == ["UP"], str(stale_gap))


async def t_live_execution_readiness_gates_phase2_submission():
    phase2_checks = {"n": 0}

    def phase2_drops(_condition):
        phase2_checks["n"] += 1
        return phase2_checks["n"] == 1

    phase2 = await _drive_phase2_with_hold(
        execution_mode="LIVE", held_provider=lambda *_a: set(),
        execution_ready_provider=phase2_drops)
    check("phase 2 rechecks readiness after validation and before submit",
          phase2_checks["n"] >= 2 and phase2["probes"] >= 1
          and phase2["orders"] == 0,
          f"checks={phase2_checks} seen={phase2}")


def t_submission_path_revalidates_every_signal_and_latency():
    source = pathlib.Path("main_bot.py").read_text(encoding="utf-8")
    check("per-round unfillable blacklist was removed",
          "unfillable_sides" not in source)
    check("submission path samples fresh Binance data after blocking I/O",
          "submit_lp, _submit_lp_ts = price_ws.fresh_snapshot" in source)
    check("submission path samples fresh Chainlink data after blocking I/O",
          "submit_cl = current_chainlink_twap()" in source)
    check("submission path revalidates the book decision",
          "final_book_side = orderbook.liquidity_signal" in source)
    check("submission path refuses a changed configured decision",
          "if submit_authority_side != side:" in source)
    final_diag = source.index(
        "_final_diagnostic_side = strategy.final_decision(")
    final_authority = source.index(
        "final_authority_side = _authority_side(")
    submit_diag = source.index(
        "_submit_diagnostic_side = strategy.final_decision(")
    submit_authority = source.index(
        "submit_authority_side = _authority_side(")
    check("dashboard decision probe ends on the configured authority",
          final_diag < final_authority and submit_diag < submit_authority)
    check("phase 2 performs an immediate price-side submission gate",
          source.count("submit_price_side = price_signal(") == 1,
          str(source.count("submit_price_side = price_signal(")))
    check("submission path bounds end-to-end validation latency",
          "validation_age > validation_limit" in source)
    check("persisted exposure rejects infinity as well as NaN",
          "not math.isfinite(persisted)" in source)


# ------------------------------------------------------ per-round trade log ---
# The RECENT TRADES panel renders main_bot.session_trades.  A row left over from
# the round that just closed reads as activity in the live market, so the loop
# empties that list at the boundary - and only that list.  trade_log.csv is the
# durable journal and must keep every row.
def t_session_trade_log_restarts_each_round():
    tree = ast.parse(pathlib.Path("main_bot.py").read_text(encoding="utf-8"))
    run_bot = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_bot"), None)
    check("run_bot is still the strategy loop", run_bot is not None)
    if run_bot is None:
        return

    rollover = next((n for n in ast.walk(run_bot)
                     if isinstance(n, ast.If)
                     and ast.unparse(n.test) == "round_window != active_window"), None)
    check("the loop still detects the round boundary", rollover is not None)
    if rollover is None:
        return

    body = {ast.unparse(stmt) for stmt in rollover.body}
    check("the on-screen trade log restarts at the boundary",
          "session_trades.clear()" in body, str(sorted(body)))
    check("the closed round's strike is still dropped with it",
          "start_price = None" in body and "start_chainlink_price = None" in body)
    check("the reset happens only at the boundary",
          sum(1 for n in ast.walk(run_bot)
              if isinstance(n, ast.Call)
              and ast.unparse(n).startswith("session_trades.clear")) == 1)

    appender = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "_append_trade"), None)
    check("_append_trade still exists", appender is not None)
    src = ast.unparse(appender).replace("'", '"') if appender else ""
    check("the CSV journal still appends every row",
          'TRADE_LOG.open("a"' in src, src[:160])
    check("nothing truncates or deletes the CSV journal",
          "unlink" not in src and '"w"' not in src and '"w+"' not in src)


# ----------------------------------------------------------- phase config ---
def _reload_config(**env):
    """Re-import config under a temporary environment. Returns the error text."""
    import importlib
    import os
    import config as cfg

    saved = dict(os.environ)
    try:
        os.environ.update({k: str(v) for k, v in env.items()})
        importlib.reload(cfg)
        return None
    except ValueError as exc:
        return str(exc)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        importlib.reload(cfg)


def t_signal_flip_config_requires_phase_two():
    parked = _reload_config(
        PAPER_ALLOW_SIGNAL_FLIPS="1", PHASE2_ENABLED="0")
    check("signal-flip experiment requires PHASE2_ENABLED=1",
          parked is not None and "PHASE2_ENABLED=1" in parked, str(parked))

    check("explicit Phase 2 signal-flip configuration loads",
          _reload_config(PAPER_ALLOW_SIGNAL_FLIPS="1",
                         PHASE2_ENABLED="1") is None)
    check("signal flips remain off by default",
          _reload_config(PAPER_ALLOW_SIGNAL_FLIPS="0") is None)

    parked_fire = _reload_config(PHASE2_MUST_FIRE="1", PHASE2_ENABLED="0")
    check("must-fire requires PHASE2_ENABLED=1",
          parked_fire is not None and "PHASE2_ENABLED=1" in parked_fire,
          str(parked_fire))
    check("must-fire configuration loads with phase 2",
          _reload_config(PHASE2_MUST_FIRE="1", PHASE2_ENABLED="1") is None)


def t_round_exposure_follows_the_enabled_phases():
    import importlib
    import os
    import config as cfg

    saved = dict(os.environ)
    try:
        os.environ.update({"PHASE2_ENABLED": "0", "BET_SIZE": "2.50"})
        importlib.reload(cfg)
        parked = cfg.MAX_ROUND_EXPOSURE
        check("parked phase 2 still reserves at least one entry",
              parked >= cfg.entry_cost_ceiling(cfg.MAX_BUY_PRICE) - 1e-9,
              f"{parked}")
        os.environ["PHASE2_ENABLED"] = "1"
        importlib.reload(cfg)
        check("switching phase 2 on raises the cap, it does not stay stale",
              cfg.MAX_ROUND_EXPOSURE > parked, str(cfg.MAX_ROUND_EXPOSURE))
    finally:
        os.environ.clear()
        os.environ.update(saved)
        importlib.reload(cfg)


def t_trade_log_path_can_isolate_an_experiment():
    import os

    import main_bot

    check("trade journal default remains trade_log.csv beside the bot",
          main_bot._configured_trade_log_path("trade_log.csv")
          == main_bot.SOURCE_ROOT / "trade_log.csv")
    previous = os.environ.get("BOT_TRADE_LOG_PATH")
    try:
        os.environ["BOT_TRADE_LOG_PATH"] = "state/signal_flip.csv"
        check("relative experiment journal from env is rooted beside the bot",
              main_bot._configured_trade_log_path()
              == main_bot.SOURCE_ROOT / "state" / "signal_flip.csv")
    finally:
        if previous is None:
            os.environ.pop("BOT_TRADE_LOG_PATH", None)
        else:
            os.environ["BOT_TRADE_LOG_PATH"] = previous


def t_trade_log_rotates_when_the_schema_changes():
    import csv as _csv
    import tempfile
    from pathlib import Path

    import main_bot

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "paper_trade_log.csv"
        original = main_bot.TRADE_LOG
        main_bot.TRADE_LOG = log
        try:
            # an old-schema file, written before phases existed
            with log.open("w", newline="", encoding="utf-8") as fh:
                w = _csv.DictWriter(fh, fieldnames=["time_et", "side", "amount",
                                                    "price_side", "book_side",
                                                    "chainlink_side", "result"])
                w.writeheader()
                w.writerow({"time_et": "Aug 16 00:00:00 ET", "side": "UP",
                            "amount": 5.0, "price_side": "UP", "book_side": "UP",
                            "chainlink_side": "UP", "result": "paper_filled"})
            main_bot.session_trades.clear()
            main_bot._append_trade({
                "time_et": "Aug 16 00:00:12 ET", "phase": "phase1", "side": "DOWN",
                "amount": 2.5, "price_side": "", "book_side": "",
                "chainlink_side": "", "result": "paper_filled"})
            archive = log.with_name("paper_trade_log.pre-phase.csv")
            check("the old-schema log is preserved, not overwritten", archive.exists())
            rows = list(_csv.DictReader(log.open(encoding="utf-8")))
            check("the new log carries the phase column",
                  rows and rows[0].get("phase") == "phase1", str(rows[:1]))
            check("one rotation only: a second write appends",
                  (main_bot._append_trade({
                      "time_et": "Aug 16 00:00:24 ET", "phase": "phase2",
                      "side": "UP", "amount": 2.5, "price_side": "UP",
                      "book_side": "", "chainlink_side": "UP",
                      "result": "paper_filled"}) or
                   len(list(_csv.DictReader(log.open(encoding="utf-8")))) == 2),
                  "expected two rows in the rotated log")
            check("a row written without a phase still validates",
                  main_bot._append_trade({
                      "time_et": "Aug 16 00:00:36 ET", "side": "UP", "amount": 2.5,
                      "price_side": "UP", "book_side": "", "chainlink_side": "UP",
                      "result": "paper_filled"}) is None)
        finally:
            main_bot.TRADE_LOG = original
            main_bot.session_trades.clear()


def t_paper_never_invents_a_strike_from_mid_round():
    """PAPER must skip a round whose boundary observation it missed.

    The market asks whether the closing TWAP beats the OPENING one. A
    mid-round substitute measures a different question and inverts the signal
    once price has moved - it put one recorded fill $58 the wrong side of the
    true strike. LIVE always skipped; PAPER now does too.
    """
    source = pathlib.Path("main_bot.py").read_text(encoding="utf-8")
    for gone in ("PAPER mid-round Chainlink reference",
                 "PAPER mid-round Binance reference"):
        check(f"the fallback is gone: {gone!r}", gone not in source)
    # The boundary latch itself must still be there, and still be exact.
    check("the exact-boundary window is still enforced",
          "active_window * 1000 <= ts_ms < (active_window + 5) * 1000" in source)
    check("a missing boundary still skips the round loudly",
          "Opening prices are captured only" in source)


def t_strategy_unchanged_finite_price_abstains():
    """A zero move is not an UP signal, including at the round boundary."""
    import math

    import strategy

    check("unchanged positive price abstains",
          strategy.decide(64_000.0, 64_000.0) is None)
    check("unchanged zero price abstains", strategy.decide(0.0, 0.0) is None)
    check("a positive move still votes UP",
          strategy.decide(64_000.0, 64_000.01) == "UP")
    check("a negative move still votes DOWN",
          strategy.decide(64_000.0, 63_999.99) == "DOWN")
    check("missing start still abstains", strategy.decide(None, 64_000.0) is None)
    check("missing current still abstains", strategy.decide(64_000.0, None) is None)
    check("equal non-finite values retain their prior comparison behavior",
          strategy.decide(math.inf, math.inf) == "UP")


def t_signal_journal_measures_edge_against_the_price():
    """Edge = accuracy minus what the market charged for the same call.

    Getting this backwards would make a losing signal look profitable, so it
    is pinned against a fixture whose answer is known by construction: a
    signal right 70% of the time, priced at 60c, is +10 points of edge.
    """
    import csv as _csv
    import json as _json
    import pathlib as _pl
    import tempfile as _tf

    import signal_journal as sj

    saved = (sj.JOURNAL, sj.WINNERS)
    out = io.StringIO()
    try:
        with _tf.TemporaryDirectory() as tmp:
            sj.JOURNAL = _pl.Path(tmp) / "j.csv"
            sj.WINNERS = _pl.Path(tmp) / "w.json"
            with sj.JOURNAL.open("w", newline="", encoding="utf-8") as fh:
                wr = _csv.DictWriter(fh, fieldnames=sj.FIELDS)
                wr.writeheader()
                for i in range(10):
                    wr.writerow({"wall": i, "window": 1000 + i, "secs_left": 150,
                                 "cl_strike": 100, "cl_now": 101,
                                 "bn_strike": 100, "bn_now": 101,
                                 "up_ask": 0.60, "up_bid": 0.59,
                                 "dn_ask": 0.41, "dn_bid": 0.40,
                                 "up_bid_vol": 10, "up_ask_vol": 5})
            sj.WINNERS.write_text(_json.dumps(
                {str(1000 + i): ("UP" if i < 7 else "DOWN") for i in range(10)}))
            with contextlib.redirect_stdout(out):
                sj.analyze()
    finally:
        sj.JOURNAL, sj.WINNERS = saved

    text = out.getvalue()
    check("journal reports the known accuracy", "70.0%" in text, text[:200])
    check("journal reports what the market charged", "60.0%" in text, text[:200])
    check("journal reports edge as accuracy minus price", "+10.0" in text, text[:200])

    # A signal that merely matches the price has no edge, however accurate.
    sides = sj._sides({"cl_strike": "100", "cl_now": "99", "bn_strike": "100",
                       "bn_now": "101", "up_bid_vol": "5", "up_ask_vol": "9"})
    check("a falling TWAP reads DOWN", sides["chainlink"] == "DOWN", str(sides))
    check("a rising spot reads UP", sides["binance"] == "UP", str(sides))
    check("ask-heavy book reads DOWN", sides["book"] == "DOWN", str(sides))
    empty = sj._sides({"up_bid_vol": "0", "up_ask_vol": "9"})
    check("a one-sided book abstains rather than voting",
          empty["book"] is None and empty["binance"] is None, str(empty))


def t_orderbook_quiet_book_is_not_mistaken_for_a_stale_one():
    """A book the venue has not changed recently is still the current book.

    Measured against btc-updown-5m: the venue left a full 0.5/0.51 book
    untouched for 95 seconds while answering every request in under 400ms.
    The old check measured staleness from the last CHANGE, so every one of
    those reads was refused as "stale or future-dated" and the round traded
    blind.
    """
    import orderbook
    import timer

    now = timer.wall()

    def book(ts_s, unit_div=1000, asset="1"):
        return {"asset_id": asset, "timestamp": str(int(ts_s * unit_div)),
                "bids": [{"price": "0.50", "size": "10"}],
                "asks": [{"price": "0.51", "size": "10"}]}

    def accepted(**kw):
        kw.setdefault("now", now)
        try:
            orderbook.parse_orderbook(kw.pop("data"), "1", **kw)
            return True, ""
        except ValueError as exc:
            return False, str(exc)

    for quiet in (33.0, 95.0, 300.0, 840.0):
        ok, why = accepted(data=book(now - quiet))
        check(f"a book unchanged for {quiet:.0f}s is accepted", ok, why)

    ok, why = accepted(data=book(now - 1200.0))
    check("a book unchanged past the frozen-venue bound is refused", not ok)
    check("the frozen-venue refusal names the cause",
          "not changed" in why, why)

    # Freshness of the copy we hold is what actually matters.
    ok, why = accepted(data=book(now - 1.0), received_at=now - 40.0)
    check("a response held longer than the age limit is refused", not ok)
    check("the held refusal is distinct from the quiet one",
          "stale in hand" in why, why)

    # Unit detection: the same instant expressed four ways must agree.
    for div, unit in ((1, "s"), (1000, "ms"), (10**6, "us"), (10**9, "ns")):
        ok, why = accepted(data=book(now - 2.0, unit_div=div))
        check(f"a timestamp in {unit} is read at the right scale", ok, why)
        if ok:
            check(f"{unit} is reported as the detected unit",
                  orderbook.LAST_TIMESTAMP_REPORT["unit"] == unit,
                  str(orderbook.LAST_TIMESTAMP_REPORT["unit"]))

    # Future-dating is a clock or unit fault, never a real book.
    ok, _ = accepted(data=book(now + 2.0))
    check("a book inside the future tolerance is accepted", ok)
    ok, why = accepted(data=book(now + 30.0))
    check("a book dated well ahead of us is refused", not ok)
    check("the future refusal points at the clock and the unit",
          "future-dated" in why and "unit" in why, why)

    # Safety validation must not be reachable around.
    for label, kw in (("now", {"now": float("nan")}),
                      ("max_age_s", {"max_age_s": float("nan")}),
                      ("max_quiet_s", {"max_quiet_s": float("nan")}),
                      ("future_tol_s", {"future_tol_s": float("nan")}),
                      ("received_at", {"received_at": float("nan")})):
        ok, _ = accepted(data=book(now - 1.0), **kw)
        check(f"a non-finite {label} cannot bypass validation", not ok)

    for label, data in (
            ("crossed", {"asset_id": "1", "timestamp": str(int(now * 1000)),
                         "bids": [{"price": "0.60", "size": "1"}],
                         "asks": [{"price": "0.50", "size": "1"}]}),
            ("empty", {"asset_id": "1", "timestamp": str(int(now * 1000)),
                       "bids": [], "asks": []}),
            ("mismatched asset", book(now, asset="999")),
            ("zero timestamp", {"asset_id": "1", "timestamp": "0",
                                "bids": [{"price": "0.5", "size": "1"}],
                                "asks": []}),
            ("unreadable timestamp", {"asset_id": "1", "timestamp": "abc",
                                      "bids": [{"price": "0.5", "size": "1"}],
                                      "asks": []})):
        ok, _ = accepted(data=data)
        check(f"a {label} book is still refused", not ok)

    # The rejection has to carry enough to diagnose it without a rerun.
    accepted(data=book(now - 33.0))
    r = orderbook.LAST_TIMESTAMP_REPORT
    for field in ("exchange_ts_raw", "exchange_ts_s", "unit", "local_ts_s",
                  "clock_offset_s", "received_at_s", "quiet_s", "held_s",
                  "max_age_s", "max_quiet_s", "future_tol_s", "source"):
        check(f"the diagnostic report carries {field}", field in r, str(sorted(r)))


def t_ws_book_accepts_a_quiet_resubscribe_snapshot():
    """The same fault on the websocket path blocked the initial sync.

    A resubscribe snapshot carries the last-change timestamp. Bounding it by
    stale_after meant any book quiet for more than a few seconds never synced
    at all, so the token stayed unusable for the whole round.
    """
    import timer
    from feeds.book import BookState

    b = BookState(stale_after=8.0)
    now_ms = int(timer.wall() * 1000)

    check("a snapshot quiet for 60s passes the event-time gate",
          b._fresh_exchange_ts(now_ms - 60_000))
    check("a snapshot quiet for 10 min passes the event-time gate",
          b._fresh_exchange_ts(now_ms - 600_000))
    check("a snapshot older than the frozen-venue bound is refused",
          not b._fresh_exchange_ts(now_ms - 1_200_000))
    check("a future-dated event is refused",
          not b._fresh_exchange_ts(now_ms + 30_000))
    check("a missing timestamp is refused", not b._fresh_exchange_ts(None))
    check("an unreadable timestamp is refused", not b._fresh_exchange_ts("abc"))
    check("liveness is still measured from receipt, not event time",
          b.stale_after == 8.0)


def main():
    # A crashing test must be one failure, not a suite that stops reporting.
    def run(fn, is_async=False):
        global F
        try:
            asyncio.run(fn()) if is_async else fn()
        except Exception as exc:
            F += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")

    for fn in [v for k, v in sorted(globals().items())
               if k.startswith("t_") and not asyncio.iscoroutinefunction(v)]:
        run(fn)
    for fn in [v for k, v in sorted(globals().items())
               if k.startswith("t_") and asyncio.iscoroutinefunction(v)]:
        run(fn, True)
    print(f"\n{P} passed, {F} failed")
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
