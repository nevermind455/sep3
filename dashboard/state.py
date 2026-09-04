"""Observed bot state.

This module NEVER computes a trading decision. It records what the bot did
and what the bot's own functions returned. Anything the bot does not produce
is stored as None and rendered as `--`.

Thread-safe: the bot calls blocking work under asyncio.to_thread, so probes
fire from worker threads while the renderer reads from the event loop.
"""
from __future__ import annotations

import threading
import time
import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Mapping

from .metrics import LatencyRegistry
from .safety import terminal_text

MISSING = "--"

# The submit path is instrumented in main_bot at these named stages. Adding
# one here also adds it to snapshot() and the LATENCY panel without any
# further wiring - `state.latency.observe("stage", ms)` starts filling it.
LATENCY_STAGES = (
    "validate",   # signals + book re-sample under the validation limit
    "guard",      # pre-submit price/side re-check inside the executor
    "network",    # FOK POST round trip until ACK
    "total",      # end-to-end place_trade call as timed by the probe
    "frame",      # dashboard frame render time
)


@dataclass
class Stamped:
    """A value plus when it arrived. `None` value means never observed."""
    value: Any = None
    at: float = 0.0          # monotonic
    wall: float = 0.0        # unix, for clock display
    source: str = ""
    latency_ms: float | None = None
    count: int = 0

    def set(self, value: Any, source: str = "", latency_ms: float | None = None) -> None:
        self.value = value
        self.at = time.monotonic()
        self.wall = time.time()
        self.source = source or self.source
        self.latency_ms = latency_ms
        self.count += 1

    def clear(self) -> None:
        """Make an unavailable value visibly absent without losing provenance."""
        self.value = None
        self.at = 0.0
        self.wall = 0.0
        self.latency_ms = None

    @property
    def age(self) -> float | None:
        return self.age_at(time.monotonic())

    def age_at(self, now: float) -> float | None:
        return None if self.at == 0.0 else max(0.0, now - self.at)

    def fresh(self, within: float) -> bool:
        a = self.age
        return a is not None and a <= within

    def status(self, warn: float, dead: float) -> str:
        """OK / STALE / DISCONNECTED / WAIT — never invented."""
        return self.status_at(warn, dead, time.monotonic())

    def status_at(self, warn: float, dead: float, now: float) -> str:
        a = self.age_at(now)
        if a is None:
            return "WAIT"
        if a > dead:
            return "DISCONNECTED"
        if a > warn:
            return "STALE"
        return "OK"


@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float

    def push(self, p: float) -> None:
        self.h = max(self.h, p)
        self.l = min(self.l, p)
        self.c = p


@dataclass
class Event:
    at: float
    wall: float
    tag: str
    text: str
    level: str = "info"     # info | good | warn | bad
    repeat: int = 1


@dataclass
class Overlay:
    """Transient centred notification. Purely visual; never blocks."""
    big: str
    sub: str = ""
    level: str = "info"
    born: float = 0.0
    ttl: float = 2.6

    def alive(self, now: float) -> bool:
        return now - self.born < self.ttl

    def intensity(self, now: float) -> float:
        """1.0 at birth, easing to 0.0 at ttl — drives the glow ramp."""
        frac = (now - self.born) / self.ttl
        return max(0.0, min(1.0, 1.0 - frac))


class TerminalState:
    """Single shared snapshot store. Every field is observed, never derived
    from a strategy re-implementation."""

    CANDLE_SECONDS = 15

    def __init__(self, max_events: int = 600, max_samples: int = 1800) -> None:
        self._lock = threading.RLock()
        self.started = time.time()
        self.started_mono = time.monotonic()

        # --- feeds -----------------------------------------------------
        self.spot = Stamped(source="binance @trade")          # price_ws.latest_price
        self.spot_changed = Stamped(source="binance @trade")  # last CHANGE, staleness proxy
        self.chainlink = Stamped(source="Chainlink 60s TWAP RTDS")
        self.chainlink_prev: float | None = None
        self.chainlink_observation_id: int | None = None
        self.chainlink_repeat = 0
        self.book = Stamped(source="clob /book (bot poll)")   # (bids, asks)
        self.book_token: str | None = None
        self.down_book = Stamped(source="clob websocket DOWN book")
        self.down_book_token: str | None = None
        self.balance = Stamped(source="clob balance-allowance")
        self.tokens = Stamped(source="gamma /events")
        self.token_fallback = False                            # H6: previous-window market

        # --- round context (from timer.py, the bot's own clock) --------
        self.round_label: str = MISSING
        self.seconds_left: int | None = None
        self.round_key: int | None = None
        # The round whose local inputs main_bot is currently evaluating.
        # This can briefly lag round_key while an old async validation unwinds.
        self.strategy_round_key: int | None = None

        # --- strategy observations (captured at the call site) ---------
        self.start_price = Stamped(source="main_bot start_price")
        self.start_chainlink = Stamped(source="main_bot start_chainlink_price")
        self.sig_price = Stamped(source="strategy.decide")
        self.sig_book = Stamped(source="orderbook.liquidity_signal")
        self.sig_chainlink = Stamped(source="strategy.decide")
        self.decision = Stamped(source="strategy.final_decision")
        self.decision_forced = False        # main_bot's `side or book or chainlink or UP`

        # --- execution --------------------------------------------------
        self.last_order = Stamped(source="polymarket_trade.place_trade")
        self.last_order_error: str | None = None
        self.cancel = Stamped(source="polymarket_trade.cancel_all_open_orders")
        self.orders_ok = 0
        self.orders_fail = 0
        self.staked = 0.0
        self.stake_curve: Deque[tuple[float, float]] = deque(maxlen=400)

        # --- health ------------------------------------------------------
        self.loop_beat = Stamped(source="run_bot heartbeat")
        self.render_ms: Deque[float] = deque(maxlen=60)
        # Rolling p50/p95/p99 for the submit path and the render loop. Named
        # here rather than in main_bot so the dashboard can render any stage
        # that gets observed, in one stable order.
        self.latency = LatencyRegistry(LATENCY_STAGES, capacity=512)
        self.frames = 0
        self.mode = "LIVE"
        self.bet_size: float | None = None
        self.trade_window: int | None = None
        self.max_buy_price: float | None = None
        self.min_buy_price: float | None = None
        self.telemetry_error: str | None = None
        # Filled by run_feeds from the persistent live/paper Ledger.  The
        # dashboard never invents PnL from order acknowledgements.
        self.accounting: dict[str, Any] = {}

        # --- series ------------------------------------------------------
        self.samples: Deque[tuple[float, float]] = deque(maxlen=max_samples)  # (wall, price)
        self.candles: Deque[Candle] = deque(maxlen=240)
        self.events: Deque[Event] = deque(maxlen=max_events)
        self.trades: list[dict] = []
        # Exits the stop loss has taken, newest last, plus its current arming
        # state. Kept separate from `trades` because an exit is not an entry:
        # mixing them makes the trade table's SIDE/RESULT columns lie.
        self.exits: list[dict] = []
        self.stop_status: dict = {}
        self.late_trim: dict = {}
        self.overlay: Overlay | None = None

        # --- notes: things this build cannot source ----------------------
        self.absent: dict[str, str] = {}

    # ------------------------------------------------------------ helpers
    def lock(self):
        return self._lock

    def note_absent(self, key: str, why: str) -> None:
        with self._lock:
            self.absent[terminal_text(key, 80)] = terminal_text(why, 1000)

    def _telemetry_reject(self, surface: str, reason: str) -> None:
        """Record invalid observed data without ever raising into the bot."""
        message = f"{surface}: {reason}"
        self.telemetry_error = message
        self.event("DASH", message, "warn")

    @staticmethod
    def _finite(value: Any, *, positive: bool = False,
                nonnegative: bool = False) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        if positive and number <= 0:
            return None
        if nonnegative and number < 0:
            return None
        return number

    def _normalise_book(self, levels: Any, side: str) -> list[dict[str, float]]:
        if levels is None:
            return []
        try:
            raw_levels = list(levels)
        except (TypeError, ValueError):
            self._telemetry_reject("book probe", f"{side} levels are not iterable")
            return []
        clean: list[dict[str, float]] = []
        invalid = 0
        for level in raw_levels:
            if not isinstance(level, Mapping):
                invalid += 1
                continue
            price = self._finite(level.get("price"), positive=True)
            size = self._finite(level.get("size"), nonnegative=True)
            if price is None or price > 1.0 or size is None:
                invalid += 1
                continue
            clean.append({"price": price, "size": size})
        if invalid:
            self._telemetry_reject("book probe", f"discarded {invalid} invalid {side} level(s)")
        return clean

    # ------------------------------------------------------------- round
    def set_round_context(self, round_key: int | None, label: str,
                          seconds_left: int | float | None) -> bool:
        """Atomically advance the displayed round and clear its old inputs.

        A missing opening observation must render as missing.  Keeping the
        previous round's Price To Beat under a new label is more dangerous
        than leaving the field blank, so every round-scoped value is reset at
        the same instant as the key and clock.
        """
        with self._lock:
            key: int | None = None
            if round_key is not None and not isinstance(round_key, bool):
                try:
                    candidate = int(round_key)
                    if candidate >= 0 and float(round_key) == candidate:
                        key = candidate
                except (TypeError, ValueError, OverflowError):
                    key = None

            remaining = self._finite(seconds_left, nonnegative=True)
            if remaining is None or remaining > 300:
                clean_seconds = None
            else:
                clean_seconds = int(remaining)

            changed = key is not None and key != self.round_key
            if changed:
                self.round_key = key
                if self.strategy_round_key != key:
                    self.strategy_round_key = None
                for field in (self.start_price, self.start_chainlink,
                              self.sig_price, self.sig_book,
                              self.sig_chainlink, self.decision):
                    field.clear()
                self.decision_forced = False

            self.round_label = terminal_text(label, 80).strip() or MISSING
            self.seconds_left = clean_seconds
            return changed

    def mark_strategy_round(self, round_key: int) -> bool:
        """Tag later strategy probes with the round main_bot actually latched."""
        if isinstance(round_key, bool):
            return False
        try:
            key = int(round_key)
            valid = key >= 0 and float(round_key) == key
        except (TypeError, ValueError, OverflowError):
            return False
        if not valid:
            return False
        with self._lock:
            self.strategy_round_key = key
        return True

    def push_price_to_beat(self, price: float | None,
                           source: str = "Chainlink 60s TWAP boundary",
                           round_key: int | None = None) -> bool:
        """Record this round's immutable official opening TWAP, if observed."""
        if price is None:
            return False
        with self._lock:
            if round_key is not None and round_key != self.round_key:
                return False
            clean = self._finite(price, positive=True)
            if clean is None:
                self._telemetry_reject(
                    "Price To Beat probe", "non-finite or non-positive price")
                return False
            if self.start_chainlink.value == clean:
                return True
            self.start_chainlink.set(clean, source=terminal_text(source, 120))
            return True

    # ------------------------------------------------------------- feeds
    def push_spot(self, price: float | None) -> None:
        if price is None:
            return
        with self._lock:
            price = self._finite(price, positive=True)
            if price is None:
                self._telemetry_reject("spot probe", "non-finite or non-positive price")
                return
            prev = self.spot.value
            self.spot.set(price)
            if prev != price:
                self.spot_changed.set(price)
                now = time.time()
                self.samples.append((now, price))
                bucket = int(now // self.CANDLE_SECONDS) * self.CANDLE_SECONDS
                if self.candles and self.candles[-1].t == bucket:
                    self.candles[-1].push(price)
                else:
                    self.candles.append(Candle(bucket, price, price, price, price))

    def push_chainlink(self, price: float | None, latency_ms: float | None = None,
                       observation_id: int | None = None) -> None:
        """Record a fresh running TWAP once per source observation.

        The dashboard polls faster than RTDS publishes.  ``observation_id``
        (the signed payload timestamp) prevents those polls from inflating the
        repeated-value counter or making an old observation look freshly
        received.
        """
        with self._lock:
            if price is None:
                self.chainlink.clear()
                self.chainlink_prev = None
                self.chainlink_observation_id = None
                self.chainlink_repeat = 0
                return
            price = self._finite(price, positive=True)
            if price is None:
                self._telemetry_reject("Chainlink probe", "non-finite or non-positive price")
                return
            latency_ms = self._finite(latency_ms, nonnegative=True) if latency_ms is not None else None
            clean_observation: int | None = None
            if observation_id is not None and not isinstance(observation_id, bool):
                try:
                    candidate = int(observation_id)
                    if candidate > 0 and float(observation_id) == candidate:
                        clean_observation = candidate
                except (TypeError, ValueError, OverflowError):
                    clean_observation = None
            if (clean_observation is not None
                    and clean_observation == self.chainlink_observation_id
                    and self.chainlink.value == price):
                # Age is useful live telemetry, but this is not a new RTDS
                # observation and must not refresh Stamped.at or repeat count.
                self.chainlink.latency_ms = latency_ms
                return
            if self.chainlink_prev is not None and abs(price - self.chainlink_prev) < 1e-9:
                self.chainlink_repeat += 1
            else:
                self.chainlink_repeat = 0
            self.chainlink_prev = price
            self.chainlink_observation_id = clean_observation
            self.chainlink.set(price, latency_ms=latency_ms)

    def push_book(self, token: str | None, bids, asks, latency_ms: float | None = None) -> None:
        with self._lock:
            self.book_token = terminal_text(token, 160) if token is not None else None
            clean_bids = self._normalise_book(bids, "bid")
            clean_asks = self._normalise_book(asks, "ask")
            latency_ms = self._finite(latency_ms, nonnegative=True) if latency_ms is not None else None
            self.book.set((clean_bids, clean_asks), latency_ms=latency_ms)

    def push_down_book(self, token: str | None, bids, asks,
                       latency_ms: float | None = None) -> None:
        with self._lock:
            self.down_book_token = terminal_text(token, 160) if token is not None else None
            clean_bids = self._normalise_book(bids, "bid")
            clean_asks = self._normalise_book(asks, "ask")
            latency_ms = self._finite(latency_ms, nonnegative=True) if latency_ms is not None else None
            self.down_book.set((clean_bids, clean_asks), latency_ms=latency_ms)

    # ------------------------------------------------------------ events
    def event(self, tag: str, text: str, level: str = "info") -> None:
        with self._lock:
            tag = terminal_text(tag, 24).strip() or "LOG"
            text = terminal_text(text, 2000)
            level = level if level in ("info", "good", "warn", "bad") else "info"
            if self.events:
                last = self.events[-1]
                if last.tag == tag and last.text == text:
                    last.repeat += 1
                    last.at = time.monotonic()
                    last.wall = time.time()
                    return
            self.events.append(
                Event(time.monotonic(), time.time(), tag, text, level)
            )

    def flash(self, big: str, sub: str = "", level: str = "info", ttl: float = 2.6) -> None:
        with self._lock:
            ttl_value = self._finite(ttl, positive=True)
            if ttl_value is None:
                self._telemetry_reject("overlay", "invalid ttl; using default")
                ttl_value = 2.6
            self.overlay = Overlay(
                big=terminal_text(big, 120), sub=terminal_text(sub, 240),
                level=level if level in ("info", "good", "warn", "bad") else "info",
                born=time.monotonic(), ttl=min(ttl_value, 30.0),
            )

    # ------------------------------------------------------------ orders
    def record_order(self, side: str, amount: float, ok: bool, error: str | None,
                     latency_ms: float | None = None, *, count_stake: bool = True) -> None:
        with self._lock:
            clean_amount = self._finite(amount, nonnegative=True)
            if clean_amount is None:
                self._telemetry_reject("order probe", "invalid amount")
            self.last_order.set(
                {"side": terminal_text(side, 16), "amount": clean_amount,
                 "ok": bool(ok), "error": terminal_text(error, 1000) if error else None},
                latency_ms=latency_ms,
            )
            self.last_order_error = terminal_text(error, 1000) if error else None
            if ok:
                self.orders_ok += 1
                if count_stake and clean_amount is not None:
                    self.staked += clean_amount
            else:
                self.orders_fail += 1
            self.stake_curve.append((time.time(), self.staked))

    # ------------------------------------------------------------ derived
    def feed_health(self, now: float | None = None) -> dict[str, str]:
        """Status per feed. Every entry is observed, or WAIT if never seen."""
        with self._lock:
            now = time.monotonic() if now is None else now
            out = {
                "BINANCE WS": self.spot_changed.status_at(5.0, 20.0, now),
                "POLY BOOK": self.book.status_at(90.0, 400.0, now),
                "CHAINLINK": self.chainlink.status_at(120.0, 600.0, now),
                "GAMMA API": self.tokens.status_at(400.0, 900.0, now),
                "BALANCE": "OK" if self.balance.value is not None else "WAIT",
                "LOOP": self.loop_beat.status_at(3.0, 12.0, now),
            }
            # Absent subsystems are ABSENT, not DISCONNECTED — they were
            # never built, so calling them "down" would be a lie.
            out["POLY WS"] = "ABSENT"
            out["USER WS"] = "ABSENT"
            out["DATABASE"] = "ABSENT"
            out["RECONCILE"] = "ABSENT"
            out["SETTLEMENT"] = "ABSENT"
            return out

    def best_book(self) -> dict[str, Any]:
        """Top of book for whichever token the bot last fetched."""
        with self._lock:
            return self._best_book_value(self.book.value)

    @staticmethod
    def _best_book_value(value: Any) -> dict[str, Any]:
        if not value or not isinstance(value, (tuple, list)) or len(value) != 2:
            return {}
        bids, asks = value
        bids = list(bids or [])
        asks = list(asks or [])
        b = max(bids, key=lambda x: x["price"], default=None)
        a = min(asks, key=lambda x: x["price"], default=None)
        depth_b = sum(x["size"] for x in bids)
        depth_a = sum(x["size"] for x in asks)
        return {
            "bid": b["price"] if b else None, "bid_sz": b["size"] if b else None,
            "ask": a["price"] if a else None, "ask_sz": a["size"] if a else None,
            "spread": (a["price"] - b["price"]) if (a and b) else None,
            "depth_bid": depth_b, "depth_ask": depth_a,
        }

    def best_down_book(self) -> dict[str, Any]:
        with self._lock:
            return self._best_book_value(self.down_book.value)
