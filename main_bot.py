"""BTC 5-min Polymarket bot - A.5 snapshot (Mar 11 2026 ~03:14 ET)."""
import asyncio
import csv
import math
import os
import threading
import time
from pathlib import Path

import config
import market_discovery
import http_pool
import orderbook
import price_ws
import strategy
import polymarket_trade
import timer
import late_trim
from polymarket_trade import cancel_all_open_orders, get_balance_allowance, place_trade
from timer import current_round_window_et, now_et, seconds_left

SOURCE_ROOT = Path(__file__).resolve().parent


def _configured_trade_log_path(raw: str | None = None) -> Path:
    """Resolve an optional experiment journal without changing the default."""
    configured = (os.environ.get("BOT_TRADE_LOG_PATH", "trade_log.csv")
                  if raw is None else raw)
    configured = str(configured or "").strip() or "trade_log.csv"
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else SOURCE_ROOT / candidate


TRADE_LOG = _configured_trade_log_path()
session_trades = []

# run_feeds.py replaces only the execution functions when --paper is chosen.
# The strategy and its timing loop stay identical.
execution_mode = "LIVE"
_paper_broker = None
_accounting_enabled = False
_round_exposure_provider = None
_round_held_tokens_provider = None
# Returns (entry_price, fee_per_share) for one open leg, or None. Only the
# pair-lock guard reads it; without it that guard stays closed.
_round_leg_basis_provider = None
_execution_ready_provider = None
_round_inventory_provider = None
_late_trim_status: dict = {}

# Set by run_feeds / run_terminal when the RTDS 60-second TWAP feed is running.
# The direct main_bot.py entrypoint creates the same service itself.
_strike = None
_strike_read_error = None


def timer_window_start(window: int = 300) -> int:
    return timer.window_start(window=window)


def chainlink_twap_for_round(window_ts: int | None = None):
    """Return the captured 60-second TWAP strike, never a spot substitute."""
    service = _strike
    if service is None:
        return None
    try:
        return service.strike_for(
            timer_window_start() if window_ts is None else window_ts)
    except Exception as exc:
        _record_strike_read_error(exc)
        return None


def current_chainlink_twap():
    """Return only a fresh 60-second TWAP from the RTDS service."""
    service = _strike
    if service is None:
        return None
    try:
        return service.current_value()
    except Exception as exc:
        _record_strike_read_error(exc)
        return None


BINANCE_AGG_TRADES = "https://api.binance.com/api/v3/aggTrades"


def _recover_boundary_print(window_start: int, timeout: float = 6.0):
    """Fetch the round's opening print that the websocket failed to latch.

    The latch needs a trade stamped inside the first 5 seconds of the round.
    If the socket is mid-reconnect across the boundary that trade is never
    delivered, and the whole round is skipped - measured at roughly one round
    in five.

    This is recovery, not substitution: aggTrades is queried for the SAME
    [window, window+5) interval, so the value returned is the one the socket
    would have latched, not a later price standing in for it. A response whose
    timestamp falls outside that interval is refused, because a trade from
    later in the round answers a different question - the market asks whether
    the close beats the OPEN.
    """
    try:
        response = http_pool.get(
            BINANCE_AGG_TRADES,
            params={"symbol": config.SYMBOL, "startTime": int(window_start) * 1000,
                    "endTime": (int(window_start) + 5) * 1000, "limit": 1},
            timeout=timeout)
        response.raise_for_status()
        rows = response.json()
    except Exception:
        return None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    try:
        stamped = int(rows[0]["T"])
        price = float(rows[0]["p"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (int(window_start) * 1000 <= stamped < (int(window_start) + 5) * 1000):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def price_signal(round_key: int, start_price, current_price):
    """Round-tagged Binance direction used by execution and dashboard telemetry."""
    del round_key  # Identity is consumed by the telemetry wrapper.
    return strategy.decide(start_price, current_price)


def chainlink_signal(round_key: int, start_price, current_price):
    """Round-tagged Chainlink direction kept as a diagnostic signal."""
    del round_key  # Identity is consumed by the telemetry wrapper.
    return strategy.decide(start_price, current_price)


def _fresh_price_permit(round_key: int, start_price, expected_side: str, *,
                        signal_observer=None) -> bool:
    """Authorize one irreversible order step against the latest SIG PRICE.

    The executor calls this after its own blocking work (and again for each
    live retry), so an order cannot outlive the Binance signal that selected
    its side.  A missing/stale print, a round rollover, equality, or a flipped
    side all fail closed.
    """
    if expected_side not in ("UP", "DOWN") or start_price is None:
        return False
    try:
        sampled_wall = timer.unix()
        if timer.window_start(sampled_wall) != round_key:
            if signal_observer is not None:
                signal_observer(None)
            return False
        current_price, current_ts_ms = price_ws.fresh_snapshot(
            config.BTC_STALE_AFTER)
        if current_price is None or current_ts_ms is None:
            if signal_observer is not None:
                signal_observer(None)
            return False
        if timer.window_start(float(current_ts_ms) / 1000.0) != round_key:
            if signal_observer is not None:
                signal_observer(None)
            return False
        sampled_side = price_signal(round_key, start_price, current_price)
        if signal_observer is not None:
            signal_observer(sampled_side)
        return sampled_side == expected_side
    except (TypeError, ValueError, OverflowError):
        if signal_observer is not None:
            try:
                signal_observer(None)
            except Exception:
                pass
        return False


def _fresh_authority_permit(round_key: int, start_price,
                            start_chainlink_price, book_side,
                            expected_side: str, *, signal_observer=None) -> bool:
    """Revalidate the configured phase-2 decision at the commit boundary.

    Normal mode remains price-authoritative and keeps the established guard.
    Price-fallback mode accepts a Book+Chainlink consensus only while fresh
    SIG PRICE is unavailable. Minority mode recomputes the dissenting side.

    The Book vote is the snapshot validated immediately before ``place_trade``.
    A REST read inside the broker's ledger lock would age the already-built FOK
    quote and leave it unvalidated, so only the in-memory price legs are sampled
    again inside this final callback.
    """
    price_fallback = config.SIGNAL_PRICE_FALLBACK_COMBINED
    if not config.SIGNAL_MINORITY_RULE and not price_fallback:
        return _fresh_price_permit(
            round_key, start_price, expected_side,
            signal_observer=signal_observer)
    if (expected_side not in ("UP", "DOWN")
            or (config.SIGNAL_MINORITY_RULE and start_price is None)):
        return False

    def observe(side):
        if signal_observer is not None:
            signal_observer(side)

    try:
        sampled_wall = timer.unix()
        if timer.window_start(sampled_wall) != round_key:
            observe(None)
            return False
        current_price, current_ts_ms = price_ws.fresh_snapshot(
            config.BTC_STALE_AFTER)
        price_sample_ready = (
            start_price is not None and current_price is not None
            and current_ts_ms is not None
            and timer.window_start(float(current_ts_ms) / 1000.0) == round_key)
        if not price_sample_ready and not price_fallback:
            observe(None)
            return False
        price_side = (price_signal(round_key, start_price, current_price)
                      if price_sample_ready else None)
        if price_side is None and not price_fallback:
            observe(None)
            return False

        current_chainlink = current_chainlink_twap()
        chainlink_side = chainlink_signal(
            round_key, start_chainlink_price, current_chainlink)
        if (config.SIGNAL_MINORITY_RULE and current_chainlink is None
                and not config.PHASE2_PARTIAL_SIGNALS):
            observe(None)
            return False
        if price_fallback and price_side is None and chainlink_side is None:
            observe(None)
            return False
        authority_side = _authority_side(
            price_side, book_side, chainlink_side)
        observe(authority_side)
        return authority_side == expected_side
    except (TypeError, ValueError, OverflowError):
        try:
            observe(None)
        except Exception:
            pass
        return False


def _late_trim_amount() -> float:
    from decimal import Decimal
    value = (Decimal(str(config.BET_SIZE))
             * Decimal(str(config.LATE_TRIM_CLIP_MULT)))
    return float(value.quantize(Decimal("0.01")))


def _in_late_trim_window(remaining: float) -> bool:
    return (config.LATE_TRIM_ENABLED
            and config.LATE_TRIM_CUTOFF_SECONDS < remaining
            <= config.LATE_TRIM_START_SECONDS)


def _inventory_legs(condition_id, up_id, down_id):
    inv = {}
    if _round_inventory_provider is not None:
        try:
            inv = _round_inventory_provider(condition_id) or {}
        except Exception:
            inv = {}
    up = inv.get(str(up_id)) or {}
    down = inv.get(str(down_id)) or {}
    return (
        float(up.get("shares") or 0.0), float(up.get("cost") or 0.0),
        float(down.get("shares") or 0.0), float(down.get("cost") or 0.0),
    )


def _best_quote(token_id):
    bids, asks = orderbook.get_orderbook(token_id)
    ask = float(asks[0]["price"]) if asks else None
    bid = float(bids[0]["price"]) if bids else None
    return bid, ask


def _set_late_trim_status(decision: dict, *, clips: int, remaining: float | None) -> None:
    global _late_trim_status
    _late_trim_status = {
        "enabled": bool(config.LATE_TRIM_ENABLED),
        "action": decision.get("action"),
        "reason": decision.get("reason"),
        "side": decision.get("side"),
        "hole": decision.get("hole"),
        "if_up": decision.get("if_up"),
        "if_down": decision.get("if_down"),
        "ask": decision.get("ask"),
        "clips": clips,
        "max_clips": config.LATE_TRIM_MAX_CLIPS,
        "amount": _late_trim_amount() if config.LATE_TRIM_ENABLED else None,
        "remaining": remaining,
    }


async def _run_late_trim(mode, exact_remaining, active_window, round_end,
                         start_price, start_chainlink_price, lp,
                         round_exposure, held_tokens, signal_epoch, trim):
    """One last-minute trim attempt. Does not open the normal entry window."""
    now_mono = asyncio.get_running_loop().time()
    last_age = (None if trim["last_mono"] is None
                else now_mono - trim["last_mono"])
    empty = late_trim.evaluate_late_trim(
        enabled=config.LATE_TRIM_ENABLED,
        remaining=exact_remaining,
        start=config.LATE_TRIM_START_SECONDS,
        cutoff=config.LATE_TRIM_CUTOFF_SECONDS,
        clips_used=trim["clips"],
        max_clips=config.LATE_TRIM_MAX_CLIPS,
        interval_s=config.LATE_TRIM_INTERVAL_SECONDS,
        last_clip_age_s=last_age,
        up_shares=0.0, up_cost=0.0, down_shares=0.0, down_cost=0.0,
        up_ask=None, down_ask=None,
        ask_min=config.LATE_TRIM_ASK_MIN, ask_max=config.LATE_TRIM_ASK_MAX,
        price_side=None, chainlink_side=None,
        amount=_late_trim_amount(),
    )
    tokens = await asyncio.to_thread(
        market_discovery.get_tokens_for_current_round, active_window)
    if not tokens or tokens.get("window_end") != round_end:
        empty["reason"] = "no market tokens"
        _set_late_trim_status(empty, clips=trim["clips"], remaining=exact_remaining)
        await asyncio.sleep(0.2)
        return round_exposure, held_tokens
    up_id, down_id = tokens["up_token_id"], tokens["down_token_id"]
    condition_id = tokens["condition_id"]
    round_exposure, held_tokens = _refresh_durable_round_state(
        active_window, condition_id, round_exposure, held_tokens)
    up_sh, up_c, dn_sh, dn_c = _inventory_legs(condition_id, up_id, down_id)
    up_bid = up_ask = dn_bid = dn_ask = None
    try:
        up_bid, up_ask = await asyncio.to_thread(_best_quote, up_id)
    except Exception:
        up_bid = up_ask = None
    try:
        dn_bid, dn_ask = await asyncio.to_thread(_best_quote, down_id)
    except Exception:
        dn_bid = dn_ask = None
    price_side = price_signal(active_window, start_price, lp)
    chainlink_side = chainlink_signal(
        active_window, start_chainlink_price, current_chainlink_twap())
    if price_side is not None:
        signal_epoch.observe(price_side)
    stop_blocks = False
    if config.STOP_LOSS_ENABLED:
        remain = exact_remaining
        armed = (config.STOP_LOSS_EXIT_CUTOFF_SECONDS < remain
                 <= config.STOP_LOSS_ARM_SECONDS)
        # Don't buy a token the stop is trying to dump.
        if armed:
            paths = late_trim.settlement_paths(up_sh, up_c, dn_sh, dn_c)
            strong = "UP" if paths["if_up"] < 0 <= paths["if_down"] else (
                "DOWN" if paths["if_down"] < 0 <= paths["if_up"] else None)
            bid = up_bid if strong == "UP" else dn_bid if strong == "DOWN" else None
            if bid is not None and bid <= config.STOP_LOSS_PRICE:
                stop_blocks = True
    decision = late_trim.evaluate_late_trim(
        enabled=config.LATE_TRIM_ENABLED,
        remaining=exact_remaining,
        start=config.LATE_TRIM_START_SECONDS,
        cutoff=config.LATE_TRIM_CUTOFF_SECONDS,
        clips_used=trim["clips"],
        max_clips=config.LATE_TRIM_MAX_CLIPS,
        interval_s=config.LATE_TRIM_INTERVAL_SECONDS,
        last_clip_age_s=last_age,
        up_shares=up_sh, up_cost=up_c, down_shares=dn_sh, down_cost=dn_c,
        up_ask=up_ask, down_ask=dn_ask,
        ask_min=config.LATE_TRIM_ASK_MIN, ask_max=config.LATE_TRIM_ASK_MAX,
        price_side=price_side, chainlink_side=chainlink_side,
        amount=_late_trim_amount(),
        stop_blocks=stop_blocks,
    )
    _set_late_trim_status(decision, clips=trim["clips"], remaining=exact_remaining)
    if decision["action"] != "buy":
        if trim.get("last_reason") != decision["reason"]:
            print(f"{_ts()} [TRIM] skip: {decision['reason']}")
            trim["last_reason"] = decision["reason"]
        await asyncio.sleep(0.2)
        return round_exposure, held_tokens
    side = decision["side"]
    amount = decision["amount"]
    ceiling = config.entry_cost_ceiling(config.LATE_TRIM_ASK_MAX)
    if round_exposure + ceiling > config.MAX_ROUND_EXPOSURE + 1e-9:
        print(f"{_ts()} [TRIM] skip: exposure cap "
              f"(${round_exposure:.2f}/${config.MAX_ROUND_EXPOSURE:.2f})")
        await asyncio.sleep(0.2)
        return round_exposure, held_tokens
    if not _execution_ready(mode, condition_id):
        print(f"{_ts()} [TRIM] skip: private fill stream not ready")
        await asyncio.sleep(0.2)
        return round_exposure, held_tokens
    verb = "Simulating trim FOK" if mode == "PAPER" else "Placing trim FOK"
    print(
        f"{_ts()} [TRIM] {verb}: {side} ${amount:.2f} @<{config.LATE_TRIM_ASK_MAX:.2f} "
        f"hole ${decision['hole']:.2f} ({trim['clips'] + 1}/"
        f"{config.LATE_TRIM_MAX_CLIPS})"
    )
    ok = await asyncio.to_thread(
        place_trade, side, amount, up_id, down_id, condition_id, round_end,
        decision["max_price"],
        pre_submit_guard=lambda: _fresh_price_permit(
            active_window, start_price, side,
            signal_observer=signal_epoch.observe),
        min_expiry=config.LATE_TRIM_CUTOFF_SECONDS)
    result = "rejected_or_unsubmitted"
    if ok:
        round_exposure += ceiling
        held_tokens.add(up_id if side == "UP" else down_id)
        signal_epoch.record_accepted(side)
        trim["clips"] += 1
        trim["last_mono"] = asyncio.get_running_loop().time()
        trim["last_reason"] = "filled"
        result = "paper_filled" if mode == "PAPER" else (
            polymarket_trade.last_order_status or
            "accepted_pending_confirmation").lower()
        print(f"{_ts()} [TRIM] filled {side} ({trim['clips']}/"
              f"{config.LATE_TRIM_MAX_CLIPS})")
    else:
        reason = polymarket_trade.last_order_error or "unknown"
        if mode == "PAPER" and _paper_broker is not None:
            reason = _paper_broker.last_error or reason
        print(f"{_ts()} [TRIM] not placed: {reason}")
        trim["last_reason"] = reason
    _append_trade({
        "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
        "phase": "late_trim",
        "side": side,
        "amount": amount,
        "price_side": price_side or "",
        "book_side": "",
        "chainlink_side": chainlink_side or "",
        "result": result,
    })
    _set_late_trim_status(decision, clips=trim["clips"], remaining=exact_remaining)
    await asyncio.sleep(0.2)
    return round_exposure, held_tokens


def _fresh_signal_permit(source: str, expected_side: str, *, round_key: int,
                         chainlink_start=None, book_token: str | None = None,
                         ) -> bool:
    """Re-check the signal that selected a multi-signal leg, before the fill.

    The price path has ``_fresh_price_permit`` for this; SIG BOOK and SIG
    CHAINLINK need the same contract, because an order must not outlive the
    signal that chose its side. Runs inside the broker's pre-submit callback,
    after the modeled latency, so it re-reads live state rather than reusing
    the value that opened the attempt. Anything unreadable fails closed.
    """
    if expected_side not in ("UP", "DOWN"):
        return False
    try:
        if source == "chainlink":
            return chainlink_signal(
                round_key, chainlink_start, current_chainlink_twap()
            ) == expected_side
        if source == "book":
            if not book_token:
                return False
            bids, asks = orderbook.get_orderbook(book_token)
            return orderbook.liquidity_signal(bids, asks) == expected_side
    except Exception:
        return False
    return False


class _RoundSignalEpoch:
    """Track accepted direction against observed non-neutral authority runs.

    The epoch advances on the first usable side and on every later UP/DOWN
    transition.  Neutral or missing samples never manufacture a transition.
    This state is deliberately round-local: carrying it across binary markets
    would let a move in one condition authorize the complement of another.
    """

    __slots__ = (
        "observed_side", "epoch", "accepted_side", "accepted_epoch",
        "ambiguous_restart",
    )

    def __init__(self):
        self.observed_side = None
        self.epoch = 0
        self.accepted_side = None
        self.accepted_epoch = None
        self.ambiguous_restart = False

    def observe(self, side: str | None) -> int:
        if side not in ("UP", "DOWN"):
            # Revoke an unconsumed edge without inventing a direction.  The
            # same side returning after an unknown-price gap must first move
            # away and back before it can authorize a held complement.
            if (self.accepted_epoch is not None
                    and self.epoch > self.accepted_epoch):
                self.accepted_epoch = self.epoch
            return self.epoch
        if side != self.observed_side:
            self.observed_side = side
            self.epoch += 1
        return self.epoch

    def initialize_from_durable(self, held_tokens: set[str],
                                up_token: str, down_token: str) -> None:
        """Restore a conservative last-side baseline after a restart.

        One durable leg identifies the last possible accepted direction, but
        it is anchored to the *current* epoch.  Therefore a current opposite
        signal cannot be mistaken for a transition observed by this process;
        a later non-neutral UP/DOWN transition is required.  With both legs,
        order is unknowable from the set, so PAPER flip mode remains blocked.
        """
        if self.accepted_side is not None or self.ambiguous_restart:
            return
        held = {str(token) for token in held_tokens}
        up_token, down_token = str(up_token), str(down_token)
        known = held.intersection((up_token, down_token))
        if held.difference((up_token, down_token)) or len(known) > 1:
            self.ambiguous_restart = True
            return
        if known == {up_token}:
            self.accepted_side = "UP"
            self.accepted_epoch = self.epoch
        elif known == {down_token}:
            self.accepted_side = "DOWN"
            self.accepted_epoch = self.epoch

    def record_accepted(self, side: str) -> None:
        if side not in ("UP", "DOWN"):
            return
        self.accepted_side = side
        self.accepted_epoch = self.epoch

    def paper_flip_permit(self, side: str) -> tuple[bool, str]:
        """Allow a held complement only after a post-accept transition."""
        if self.ambiguous_restart:
            return False, (
                "durable holdings contain both/unknown legs; "
                "last accepted side is ambiguous")
        if self.accepted_side is None or self.accepted_epoch is None:
            return False, "last accepted side is unavailable"
        if self.observed_side != side or self.epoch <= self.accepted_epoch:
            return False, ("no later non-neutral transition of the deciding "
                           "signal was observed")
        return True, "verified transition of the deciding signal"


def _authority_side(price_side, book_side, chainlink_side):
    """The signal that actually decides the order side under this config.

    The round epoch has to track whatever drives execution. That may be the
    explicit minority experiment, normal SIG PRICE, or (only while SIG PRICE
    is absent) the configured BOOK+CHAINLINK consensus fallback.
    """
    if config.SIGNAL_MINORITY_RULE:
        return strategy.minority_decision(price_side, book_side, chainlink_side)
    if price_side in ("UP", "DOWN"):
        return price_side
    if config.SIGNAL_PRICE_FALLBACK_COMBINED:
        # With the primary signal absent, two independent fallback votes must
        # agree. A 1-1 split is not a combined direction and cannot authorize
        # an order merely because one source happened to be checked first.
        if (book_side in ("UP", "DOWN")
                and chainlink_side == book_side):
            return book_side
    return None


def _must_fire_side(price_side, book_side, chainlink_side, *, last_side=None):
    """Pick a placeable side when PHASE2_MUST_FIRE cannot abstain.

    Configured authority still wins. If SIG PRICE is flat at the strike, use
    the diagnostic vote, then book, then Chainlink, then the last accepted
    side. A last-resort UP keeps the cycle from dying with no order.
    """
    configured = _authority_side(price_side, book_side, chainlink_side)
    if configured in ("UP", "DOWN"):
        source = ("SIG PRICE" if price_side in ("UP", "DOWN")
                  else "BOOK+CHAINLINK FALLBACK")
        if config.SIGNAL_MINORITY_RULE:
            source = "MINORITY"
        return configured, source
    diagnostic = strategy.final_decision(price_side, book_side, chainlink_side)
    if diagnostic in ("UP", "DOWN"):
        return diagnostic, "DIAGNOSTIC"
    if book_side in ("UP", "DOWN"):
        return book_side, "SIG BOOK"
    if chainlink_side in ("UP", "DOWN"):
        return chainlink_side, "SIG CHAINLINK"
    if last_side in ("UP", "DOWN"):
        return last_side, "LAST SIDE"
    return "UP", "MUST-FIRE DEFAULT"


stop_event = threading.Event()


def _ts():
    return now_et().strftime("[%b %d %H:%M:%S ET]")


def _record_strike_read_error(exc) -> None:
    """Fail closed and surface a broken RTDS service object once per error type."""
    global _strike_read_error
    detail = f"{type(exc).__name__}: {exc}"[:160]
    if detail != _strike_read_error:
        _strike_read_error = detail
        print(f"{_ts()} [TWAP] Read failed: {detail}")


TRADE_LOG_FIELDS = ["time_et", "phase", "side", "amount", "price_side",
                    "book_side", "chainlink_side", "result"]


def _rotate_trade_log_if_stale() -> bool:
    """Move an old-schema log aside once, rather than mixing column counts.

    A file whose header lacks `phase` cannot hold the new rows: csv readers
    key off the header, so the extra value would be silently dropped.
    """
    if not TRADE_LOG.exists():
        return False
    try:
        with TRADE_LOG.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
    except OSError:
        return False
    if header == TRADE_LOG_FIELDS:
        return False
    archive = TRADE_LOG.with_name(f"{TRADE_LOG.stem}.pre-phase{TRADE_LOG.suffix}")
    try:
        TRADE_LOG.replace(archive)
    except OSError as exc:
        print(f"{_ts()} [LOG] could not archive the old trade log: {type(exc).__name__}")
        return False
    print(f"{_ts()} [LOG] trade log schema changed; previous rows kept in {archive.name}")
    return True


def _append_trade(row):
    row.setdefault("phase", "")
    session_trades.append(row)
    try:
        _rotate_trade_log_if_stale()
        write_header = not TRADE_LOG.exists()
        with TRADE_LOG.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            if write_header:
                w.writeheader()
            w.writerow(row)
            f.flush()
    except OSError as exc:
        # A display journal failure must not relabel an already-submitted live
        # order as failed or crash the state machine. The persistent fill
        # ledger remains authoritative.
        print(f"{_ts()} [LOG] Trade CSV write failed: {type(exc).__name__}")


async def _cooldown(seconds: float | None = None) -> None:
    """Wait out the trade interval, but never across a round boundary.

    BUGFIX: this used to be a flat TRADE_INTERVAL_SECONDS sleep that only woke
    for shutdown. Round rotation and the opening-print latch both live at the
    TOP of the strategy loop, so a cooldown beginning a second or two before a
    boundary held the loop for the rest of its 12s - and the new round was not
    detected until ~10s in. By then the opening print, which is only latchable
    from a trade stamped in the first 5 seconds, was already unreachable and
    the round was lost. Returning at the boundary costs nothing: the loop
    re-enters, sees the new window, and the interval restarts naturally.
    """
    gap = config.TRADE_INTERVAL_SECONDS if seconds is None else seconds
    deadline = time.monotonic() + gap
    entry_window = timer.window_start()
    while time.monotonic() < deadline and not stop_event.is_set():
        if timer.window_start() != entry_window:
            return
        await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def _pair_lock_permit(condition_id: str | None, other_token: str,
                      ask) -> tuple[bool, str]:
    """Permit a complement leg only when the finished pair cannot lose.

    Every failure path returns False. This relaxes the guard that normally
    stops the bot owning both legs, so anything it cannot positively verify -
    a disabled switch, a missing provider, an unreadable basis, a raising
    ledger - has to read as "refuse", exactly as if the lock were off.
    """
    if not config.PAIR_LOCK_ENABLED:
        return False, "pair-lock is disabled"
    if _round_leg_basis_provider is None:
        return False, "no ledger basis provider is installed"
    try:
        basis = _round_leg_basis_provider(condition_id, other_token)
    except Exception as exc:
        return False, f"ledger basis unavailable ({type(exc).__name__})"
    if not basis:
        return False, "the held leg has no readable cost basis"
    entry, entry_fee = basis
    permitted, locked = config.pair_lock_permits(entry, entry_fee, ask)
    if not permitted:
        return False, (
            f"pair would cost ${entry + entry_fee + float(ask):.4f} all-in "
            f"for a $1.00 payout (needs {config.PAIR_LOCK_MIN_EDGE:.4f} edge)"
        )
    return True, (
        f"locks ${locked:+.4f}/pair: held {entry:.3f}+{entry_fee:.4f}fee "
        f"plus this leg at {float(ask):.3f}"
    )


def _refresh_durable_round_state(window_start: int, condition_id: str | None,
                                 exposure: float,
                                 held_tokens: set[str]) -> tuple[float, set[str]]:
    """Merge persisted exposure and owned/accepted legs into loop state."""
    current = float(exposure)
    if not math.isfinite(current) or current < 0:
        raise RuntimeError("in-memory round exposure is invalid")
    if _round_exposure_provider is not None:
        persisted = float(_round_exposure_provider(window_start, condition_id))
        if not math.isfinite(persisted) or persisted < 0:
            raise RuntimeError("persisted round exposure is invalid")
        current = max(current, persisted)

    merged = set(held_tokens)
    if _round_held_tokens_provider is not None:
        durable = _round_held_tokens_provider(window_start, condition_id)
        if durable is None:
            durable = ()
        if isinstance(durable, (str, bytes, dict)):
            raise RuntimeError("persisted held-token state is invalid")
        try:
            for raw in durable:
                token = str(raw or "")
                if (not token or len(token) > 256 or not token.isprintable()
                        or any(ch.isspace() for ch in token)):
                    raise RuntimeError("persisted held-token state is invalid")
                merged.add(token)
        except TypeError as exc:
            raise RuntimeError("persisted held-token state is invalid") from exc
    return current, merged


def _execution_ready(mode: str, condition_id: str | None) -> bool:
    """Paper is self-accounting; LIVE requires its private fill stream."""
    if mode != "LIVE":
        return True
    if _execution_ready_provider is None:
        return False
    try:
        # Require a literal boolean so a malformed provider cannot fail open.
        return _execution_ready_provider(condition_id) is True
    except Exception as exc:
        raise RuntimeError("live execution-readiness check failed") from exc


def _kill_switch():
    print("[KILL] Press Enter in this terminal to stop the bot...")
    try:
        input()
    except EOFError:
        return
    stop_event.set()
    print(f"{_ts()} [BOT] Kill switch triggered.")


async def run_bot():
    start_price = None
    joined_window = None
    boundary_backfilled = False
    start_chainlink_price = None
    active_window = None
    round_exposure = 0.0
    last_status = 0.0
    skip_logged_window = None
    held_tokens: set[str] = set()
    signal_epoch = _RoundSignalEpoch()
    trim = {"clips": 0, "last_mono": None, "last_reason": None}

    mode = str(execution_mode or "LIVE").upper()
    if mode not in {"LIVE", "PAPER"}:
        raise RuntimeError(f"invalid execution mode {mode!r}")
    if mode == "PAPER":
        if _paper_broker is None or not polymarket_trade.live_execution_disabled():
            raise RuntimeError("paper mode firewall was not installed")
    elif _paper_broker is not None or polymarket_trade.live_execution_disabled():
        raise RuntimeError("live mode cannot start in a paper-disabled process")

    print(f"{_ts()} [BOT] ========== BTC 5-min Polymarket bot started ({mode}) ==========")
    if mode == "PAPER":
        print(f"{_ts()} [PAPER] LIVE ORDERS DISABLED | no signer, wallet auth, or order endpoint.")
        print(f"{_ts()} [PAPER] Fills use the live public book; settlement uses Polymarket resolution.")
    print(f"{_ts()} [BOT] Timezone: Eastern (ET). Rounds every 5 min. Trade from open, every {config.TRADE_INTERVAL_SECONDS:.0f}s.")
    print(
        f"{_ts()} [BOT] First prints latch the strike; then trade for "
        f"{config.TRADE_LAST_SECONDS}s of the round, every {config.TRADE_INTERVAL_SECONDS:.0f}s."
    )
    print(
        f"{_ts()} [BOT] Time check: now {now_et().strftime('%b %d %H:%M:%S ET')} | "
        f"current round {current_round_window_et()} (compare with Polymarket)"
    )
    print(f"{_ts()} [BOT] Kill switch: press Enter in this terminal to stop safely.")
    if config.LATE_TRIM_ENABLED:
        print(
            f"{_ts()} [TRIM] last-minute loss trim ON | "
            f"T-{config.LATE_TRIM_START_SECONDS:.0f}..T-{config.LATE_TRIM_CUTOFF_SECONDS:.0f} | "
            f"{config.LATE_TRIM_MAX_CLIPS} clips x ${_late_trim_amount():.2f} | "
            f"ask {config.LATE_TRIM_ASK_MIN:.2f}-{config.LATE_TRIM_ASK_MAX:.2f}"
        )
    else:
        print(f"{_ts()} [TRIM] last-minute loss trim OFF (LATE_TRIM_ENABLED=0)")

    bal = await asyncio.to_thread(get_balance_allowance)
    if bal:
        if mode == "PAPER":
            print(f"{_ts()} [PAPER] Simulated cash balance: ${bal['balance']:.2f}")
        else:
            print(f"{_ts()} [BOT] Polymarket USDC balance: ${bal['balance']:.2f} | allowance: ${bal['allowance']:.2f}")
    else:
        message = "Could not read balance/allowance at startup"
        if mode == "LIVE":
            raise RuntimeError(f"{message}; live mode is fail-closed")
        print(f"{_ts()} [BOT] WARN: {message}.")

    clock_ok, clock_detail, drift = await asyncio.to_thread(
        timer.check_clock, config.CLOB_HOST, config.CLOCK_MAX_DRIFT_SECONDS)
    if drift is not None:
        print(
            f"{_ts()} [CLOCK] {clock_detail}; "
            f"round windows use Unix time, CLOB offset {timer.clock_offset():+.3f}s "
            f"applies only to book timestamps"
        )
    else:
        print(f"{_ts()} [CLOCK] {clock_detail}")
    if not clock_ok:
        if mode == "LIVE":
            raise RuntimeError(
                "CLOB clock synchronization could not be verified; "
                f"timing is unsafe ({clock_detail}). Sync this computer's clock "
                "to internet time, then restart. Windows: start the Windows Time "
                "service and run `w32tm /resync`.")
        if drift is not None:
            print(
                f"{_ts()} [CLOCK] WARN: local clock is past the "
                f"{config.CLOCK_MAX_DRIFT_SECONDS:.3f}s live limit; PAPER continues "
                "and keeps Unix 5-minute windows so rounds match Polymarket slugs."
            )
        else:
            print(
                f"{_ts()} [CLOCK] WARN: CLOB clock could not be verified; "
                "PAPER continues on the local clock."
            )

    print(f"{_ts()} [BOT] Strategy loop started. Waiting for price feed and next round (ET)...")

    while not stop_event.is_set():
        sampled_wall = timer.unix()
        remain = seconds_left(sampled_wall)
        round_window = timer.window_start(sampled_wall)
        round_end = round_window + 300
        exact_remaining = round_end - sampled_wall
        if round_window != active_window:
            # The first window this process sees was already underway when it
            # started: its open is behind us, the book has moved, and only part
            # of the trading window remains. Note it so the round can be
            # observed but not traded, and begin at the next clean boundary.
            if joined_window is None:
                joined_window = round_window
            # Never carry a prior round's strike into the next market.  The
            # old loop only overwrote these when a feed read succeeded.
            active_window = round_window
            start_price = None
            boundary_backfilled = False
            start_chainlink_price = None
            # The on-screen trade log is a per-round view. Rows from the round
            # that just closed would read as activity in this market, so the
            # log restarts at the boundary. trade_log.csv keeps every row.
            session_trades.clear()
            # Per-round holdings so we never buy both legs of the same market.
            held_tokens = set()
            signal_epoch = _RoundSignalEpoch()
            trim = {"clips": 0, "last_mono": None, "last_reason": None}
            # LIVE authorizations are keyed by the known five-minute window,
            # so they can be restored before discovery. PAPER inventory is
            # keyed by condition and is refreshed immediately after discovery.
            round_exposure, held_tokens = _refresh_durable_round_state(
                active_window, None, 0.0, held_tokens)

        display_price, _display_mono, display_ts = price_ws.latest_snapshot()
        lp, lp_ts_ms = price_ws.fresh_snapshot(config.BTC_STALE_AFTER)

        if start_chainlink_price is None:
            twap_start = chainlink_twap_for_round(active_window)
            if twap_start is not None:
                start_chainlink_price = twap_start
                print(
                    f"{_ts()} [ROUND] Chainlink 60s TWAP "
                    f"start_price=${start_chainlink_price:,.2f}"
                )
                if lp is not None and _strike is not None:
                    d = _strike.divergence(lp, active_window)
                    if d.get("diff") is not None:
                        print(
                            f"{_ts()} [TWAP] Binance is "
                            f"{d['diff']:+.2f} / {d['diff_bps']:+.1f}bps "
                            f"from the 60s TWAP strike"
                        )
            # PAPER used to substitute a mid-round TWAP here when the boundary
            # observation was missed. It cannot: the market asks whether the
            # closing TWAP beats the OPENING one, so a mid-round reference
            # measures a different question and inverts the signal whenever
            # price has already moved. Measured at 4.9% of phase-2 fills, one
            # of them $58 the wrong side of the true strike. Both modes now
            # skip the round instead, which is what LIVE always did.

        # Latch the first print whose exchange timestamp is in the opening
        # 5 seconds. Never invent a strike from a later print: see above.
        if start_price is None:
            for px, ts_ms in ((lp, lp_ts_ms), (display_price, display_ts)):
                if (px is not None and ts_ms is not None
                        and active_window * 1000 <= ts_ms < (active_window + 5) * 1000):
                    start_price = px
                    print(
                        f"{_ts()} [ROUND] New round started "
                        f"(Binance start_price=${start_price:,.2f})"
                    )
                    break
            if (start_price is None and not boundary_backfilled
                    and exact_remaining <= 300 - config.BOUNDARY_BACKFILL_AFTER):
                # The socket has had its chance; ask REST for the same trade.
                boundary_backfilled = True
                recovered = await asyncio.to_thread(
                    _recover_boundary_print, active_window)
                if recovered is not None:
                    start_price = recovered
                    print(f"{_ts()} [ROUND] Opening print recovered from REST "
                          f"(Binance start_price=${start_price:,.2f})")
                else:
                    print(f"{_ts()} [ROUND] Opening print could not be recovered; "
                          f"this round has no Binance reference.")

        now = asyncio.get_running_loop().time()
        if now - last_status >= 30:
            last_status = now
            price_txt = (f"${display_price:,.2f}" if display_price is not None
                         else "waiting for price...")
            if not config.PHASE2_ENABLED:
                phase = "idle | phase 2 parked"
            elif exact_remaining > config.TRADE_LAST_SECONDS:
                wait_s = exact_remaining - config.TRADE_LAST_SECONDS
                phase = f"analysis | first trade in {wait_s:.0f}s"
            elif _in_late_trim_window(exact_remaining):
                phase = "LATE TRIM"
            elif exact_remaining >= config.MIN_SECONDS_TO_EXPIRY:
                phase = "TRADE WINDOW"
            else:
                phase = "round ending"
            print(
                f"{_ts()} [BOT] Running | round {current_round_window_et()} | "
                f"ends in {remain}s | {phase} | {price_txt}"
            )

        if (config.SKIP_JOINED_ROUND and joined_window is not None
                and active_window == joined_window):
            if skip_logged_window != active_window:
                skip_logged_window = active_window
                nxt = now_et(round_end).strftime("%I:%M%p ET").lstrip("0")
                print(f"{_ts()} [ROUND] Joined this round in progress "
                      f"({exact_remaining:.0f}s left); waiting for the next "
                      f"market at {nxt}.")
            await asyncio.sleep(0.2)
            continue

        if (config.PHASE2_ENABLED
                and 0 < exact_remaining <= config.TRADE_LAST_SECONDS
                and exact_remaining >= config.MIN_SECONDS_TO_EXPIRY
                and not _in_late_trim_window(exact_remaining)):
            # Keep each signal on one source: Binance start vs Binance now,
            # Chainlink 60s TWAP start vs Chainlink 60s TWAP now.  Mixing a
            # TWAP strike with a spot current value silently flips close calls.
            current_cl = current_chainlink_twap()
            missing = []
            if start_price is None:
                missing.append("Binance boundary print")
            if lp is None:
                missing.append("fresh Binance print")
            if start_chainlink_price is None:
                missing.append("Chainlink boundary TWAP")
            if current_cl is None:
                missing.append("fresh Chainlink TWAP")
            blocking = missing
            if config.SIGNAL_PRICE_FALLBACK_COMBINED:
                # Either complete path is enough: Binance owns the side when
                # available; otherwise Chainlink plus the Book vote (read
                # below) may form the explicit fallback consensus.
                binance_missing = [item for item in missing
                                    if "Binance" in item]
                chainlink_missing = [item for item in missing
                                      if "Chainlink" in item]
                blocking = (binance_missing + chainlink_missing
                            if binance_missing and chainlink_missing else [])
            elif config.PHASE2_PARTIAL_SIGNALS:
                # In ordinary price-authority mode Chainlink is diagnostic and
                # may abstain; Binance remains mandatory.
                blocking = [item for item in missing if "Binance" in item]
                abstaining = [item for item in missing if item not in blocking]
                if abstaining and not blocking and skip_logged_window != active_window:
                    skip_logged_window = active_window
                    print(f"{_ts()} [RISK] SIG CHAINLINK abstains this round "
                          f"(missing {', '.join(abstaining)}); trading continues "
                          f"on the signals that are ready.")
            if blocking:
                missing = blocking
                structural = [item for item in missing if "boundary" in item]
                if structural:
                    if skip_logged_window != active_window:
                        skip_logged_window = active_window
                        next_open = now_et(round_end).strftime("%I:%M%p ET").lstrip("0")
                        print(
                            f"{_ts()} [RISK] No order this round: missing "
                            f"{', '.join(missing)}."
                        )
                        print(
                            f"{_ts()} [ROUND] Opening prices are captured only "
                            f"in the first 5s after the 5-minute boundary. Keep "
                            f"the bot running through {next_open}; the next "
                            f"open is the first trade chance."
                        )
                else:
                    print(f"{_ts()} [RISK] No order: missing {', '.join(missing)}.")
                await asyncio.sleep(0.2)
                continue

            print(f"{_ts()} [BOT] Trade window ({exact_remaining:.2f}s left) - validating live state...")

            tokens = await asyncio.to_thread(
                market_discovery.get_tokens_for_current_round, active_window)
            if not tokens:
                print(f"{_ts()} [BOT] WARN: No market tokens - cannot place order.")
                await _cooldown(1.0)
                continue
            if (tokens.get("window_start") != active_window
                    or tokens.get("window_end") != round_end):
                print(f"{_ts()} [RISK] No order: discovered market does not match sampled round.")
                await _cooldown(1.0)
                continue

            up_id = tokens["up_token_id"]
            down_id = tokens["down_token_id"]
            ob_id = tokens.get("orderbook_token_id") or up_id
            # Accepted live submissions and confirmed paper fills survive a
            # restart. Restore both the cap and complement guard before any
            # phase-2 decision can reach submission.
            round_exposure, held_tokens = _refresh_durable_round_state(
                active_window, tokens["condition_id"],
                round_exposure, held_tokens)
            if not _execution_ready(mode, tokens["condition_id"]):
                print(f"{_ts()} [RISK] No order: private fill stream is not "
                      "LIVE and subscribed to this market.")
                await asyncio.sleep(0.2)
                continue

            print(f"{_ts()} [BOT] Market tokens found for this round.")

            running = current_cl
            ptb = start_chainlink_price
            ptb_s = f"${ptb:,.2f}" if ptb is not None else "N/A"
            run_s = f"${running:,.2f}" if running is not None else "N/A"
            print(
                f"{_ts()} [BOT] Price-to-beat (round start) = {ptb_s} | "
                f"Running price = {run_s} "
                f"(official Chainlink 60s TWAP source)"
            )

            price_side = price_signal(active_window, start_price, lp)
            book_side = None
            chainlink_side = chainlink_signal(
                active_window, start_chainlink_price, current_cl)
            try:
                bids, asks = await asyncio.to_thread(orderbook.get_orderbook, ob_id)
                book_side = orderbook.liquidity_signal(bids, asks)
            except Exception as exc:
                print(f"{_ts()} [MARKET] Orderbook rejected: {type(exc).__name__}: {exc}")
                await _cooldown(1.0)
                continue

            diagnostic_side = strategy.final_decision(
                price_side, book_side, chainlink_side)
            # Anchor holdings before a must-fire fallback so LAST SIDE can
            # reuse a durable accepted direction. Minority still waits below
            # when must-fire is off, so a restart sample cannot look like a flip.
            if config.PHASE2_MUST_FIRE:
                signal_epoch.initialize_from_durable(held_tokens, up_id, down_id)
            if (price_side is None
                    and not config.SIGNAL_PRICE_FALLBACK_COMBINED
                    and not config.PHASE2_MUST_FIRE):
                print(f"{_ts()} [RISK] No order: fresh SIG PRICE is neutral or unavailable.")
                await asyncio.sleep(0.2)
                continue
            # Resolve the configured authority after publishing the diagnostic
            # vote. SIG PRICE wins whenever it speaks. The fallback is reachable
            # only when it does not, and requires Book + Chainlink agreement.
            if config.PHASE2_MUST_FIRE:
                side, authority_source = _must_fire_side(
                    price_side, book_side, chainlink_side,
                    last_side=signal_epoch.accepted_side or signal_epoch.observed_side)
            else:
                side = _authority_side(price_side, book_side, chainlink_side)
                authority_source = (
                    "MINORITY" if config.SIGNAL_MINORITY_RULE else
                    "SIG PRICE" if price_side in ("UP", "DOWN") else
                    "BOOK+CHAINLINK FALLBACK")
            signal_epoch.observe(side)
            if side is None:
                if config.SIGNAL_PRICE_FALLBACK_COMBINED:
                    print(f"{_ts()} [RISK] No order: SIG PRICE is unavailable and "
                          "SIG BOOK + SIG CHAINLINK do not agree on a fallback.")
                elif config.SIGNAL_MINORITY_RULE:
                    print(f"{_ts()} [RISK] No order: signals are tied or "
                          f"unanimous-neutral, so there is no minority side.")
                else:
                    print(f"{_ts()} [RISK] No order: no configured signal authority.")
                await asyncio.sleep(0.2)
                continue
            # Anchor restart recovery only after the configured authority has
            # been observed. Doing this earlier in minority mode made the first
            # post-restart sample look like a verified signal flip and could
            # authorize the complementary leg immediately.
            if not config.PHASE2_MUST_FIRE:
                signal_epoch.initialize_from_durable(held_tokens, up_id, down_id)
            print(
                f"{_ts()} [SIGNAL] price={price_side} book={book_side or 'n/a'} "
                f"chainlink={chainlink_side} -> diagnostic={diagnostic_side or 'n/a'} "
                f"authority={authority_source} ORDER SIDE={side}"
            )

            entry_ceiling = config.entry_cost_ceiling(config.MAX_BUY_PRICE)
            if round_exposure + entry_ceiling > config.MAX_ROUND_EXPOSURE + 1e-9:
                print(
                    f"{_ts()} [RISK] Round exposure cap reached "
                    f"(${round_exposure:.2f}/${config.MAX_ROUND_EXPOSURE:.2f})."
                )
                await _cooldown()
                continue

            clock_ok, clock_detail, drift = await asyncio.to_thread(
                timer.check_clock, config.CLOB_HOST, config.CLOCK_MAX_DRIFT_SECONDS)
            if mode == "LIVE":
                if not clock_ok:
                    print(f"{_ts()} [RISK] No order: {clock_detail}.")
                    await asyncio.sleep(0.5)
                    continue
            elif not clock_ok and drift is None and not timer.clock_measured():
                print(f"{_ts()} [RISK] No order: {clock_detail}.")
                await asyncio.sleep(0.5)
                continue

            # Discovery, book reads and clock I/O take time. Re-sample the
            # boundary immediately before any authenticated action.
            action_wall = timer.unix()
            if (timer.window_start(action_wall) != active_window
                    or action_wall >= round_end - config.MIN_SECONDS_TO_EXPIRY):
                print(f"{_ts()} [RISK] No order: round changed during validation.")
                await asyncio.sleep(0.2)
                continue

            if config.CANCEL_OPEN_BEFORE_TRADE:
                action = "Clearing simulated orders" if mode == "PAPER" else "Closing any open orders first"
                print(f"{_ts()} [BOT] {action} (so new order can go through)...")
                cancelled = await asyncio.to_thread(cancel_all_open_orders)
                if not cancelled:
                    reason = polymarket_trade.last_order_error or "cancel-all failed"
                    print(f"{_ts()} [RISK] No order because cancellation failed: {reason}")
                    await asyncio.sleep(0.5)
                    continue

            action_wall = timer.unix()
            if (timer.window_start(action_wall) != active_window
                    or action_wall >= round_end - config.MIN_SECONDS_TO_EXPIRY):
                print(f"{_ts()} [RISK] No order: round changed before submission.")
                await asyncio.sleep(0.2)
                continue

            # Discovery and clock/cancellation I/O can take several seconds.
            # Re-sample every decision input after that I/O, then bound the
            # validation-to-submit interval.  Never place an order using a
            # signal that was fresh only at the beginning of the pipeline.
            validation_started = time.monotonic()
            final_lp, _final_lp_ts = price_ws.fresh_snapshot(config.BTC_STALE_AFTER)
            final_cl = current_chainlink_twap()
            if ((final_lp is None
                 and not config.SIGNAL_PRICE_FALLBACK_COMBINED)
                    or (final_cl is None
                        and not config.PHASE2_PARTIAL_SIGNALS
                        and not config.SIGNAL_PRICE_FALLBACK_COMBINED)):
                print(f"{_ts()} [RISK] No order: a price feed became stale during validation.")
                await asyncio.sleep(0.2)
                continue
            try:
                final_bids, final_asks = await asyncio.to_thread(
                    orderbook.get_orderbook, ob_id)
                final_book_side = orderbook.liquidity_signal(final_bids, final_asks)
            except Exception as exc:
                print(
                    f"{_ts()} [MARKET] Final orderbook validation failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                await _cooldown(1.0)
                continue
            final_price_side = price_signal(active_window, start_price, final_lp)
            final_chainlink_side = chainlink_signal(
                active_window, start_chainlink_price, final_cl)
            _final_diagnostic_side = strategy.final_decision(
                final_price_side, final_book_side, final_chainlink_side)
            final_authority_side = _authority_side(
                final_price_side, final_book_side, final_chainlink_side)
            if (final_price_side is None
                    and not config.SIGNAL_PRICE_FALLBACK_COMBINED):
                signal_epoch.observe(None)
                if not config.PHASE2_MUST_FIRE:
                    print(f"{_ts()} [RISK] No order: SIG PRICE became neutral during validation.")
                    await asyncio.sleep(0.2)
                    continue
                print(f"{_ts()} [BOT] SIG PRICE became neutral; PHASE2_MUST_FIRE keeps {side}.")
            if final_authority_side is None and config.PHASE2_MUST_FIRE:
                final_authority_side = side
            signal_epoch.observe(final_authority_side)
            if final_authority_side is None:
                print(f"{_ts()} [RISK] No order: deciding signals became tied or "
                      "unavailable during validation.")
                await asyncio.sleep(0.2)
                continue
            if final_authority_side != side:
                if config.PHASE2_MUST_FIRE:
                    print(
                        f"{_ts()} [BOT] Decision changed during validation "
                        f"({side} -> {final_authority_side}); firing {final_authority_side}."
                    )
                    side = final_authority_side
                else:
                    print(
                        f"{_ts()} [RISK] No order: configured decision changed during "
                        f"validation ({side} -> {final_authority_side})."
                    )
                    await asyncio.sleep(0.2)
                    continue

            other_token = down_id if side == "UP" else up_id
            if other_token in held_tokens and config.PHASE2_MUST_FIRE:
                print(
                    f"{_ts()} [BOT] Complement already held; "
                    f"PHASE2_MUST_FIRE still fires {side}."
                )
            elif other_token in held_tokens:
                flip_allowed = False
                flip_detail = "PAPER signal-flip mode is disabled"
                flips_enabled = (config.PAPER_ALLOW_SIGNAL_FLIPS if mode == "PAPER"
                                 else config.LIVE_ALLOW_SIGNAL_FLIPS)
                if flips_enabled:
                    flip_allowed, flip_detail = signal_epoch.paper_flip_permit(side)
                lock_ok = False
                lock_detail = ("pair-lock is disabled"
                               if not config.PAIR_LOCK_ENABLED
                               else "pair-lock not evaluated")
                if not flip_allowed and config.PAIR_LOCK_ENABLED:
                    # Reached only when the complement is already held, so this
                    # extra book read stays off the common path. The selected
                    # leg is not necessarily ob_id, so final_asks cannot price
                    # it: a pair checked against the wrong leg is not checked.
                    lock_asks = None
                    try:
                        _lock_bids, lock_asks = await asyncio.to_thread(
                            orderbook.get_orderbook,
                            up_id if side == "UP" else down_id)
                    except Exception as exc:
                        lock_detail = f"book read failed ({type(exc).__name__})"
                    if lock_asks:
                        lock_ok, lock_detail = _pair_lock_permit(
                            tokens["condition_id"], other_token,
                            float(lock_asks[0]["price"]))
                    elif lock_asks is not None:
                        lock_detail = "no ask on the selected leg"
                # Multi-signal rounds are expected to hold both legs, so the
                # guard cannot also be the thing that stops the next cycle
                # trading. It now runs in LIVE too, so standing the guard down
                # unconditionally is no longer acceptable: the complement is
                # allowed only where the pair-lock proves the finished pair
                # cannot lose. Unconditional pairs were measured at -$0.22 each
                # at the 1.0100 overround this book actually runs.
                # With the lock ON, a complement is allowed only where the pair
                # is provably profitable. With it OFF the operator has chosen to
                # take pairs unconditionally, so the guard stands down entirely
                # - "lock disabled" must mean no restriction, not no pairs.
                multi_allowed = config.PHASE2_MULTI_SIGNAL and (
                    lock_ok or not config.PAIR_LOCK_ENABLED)
                if not (flip_allowed or lock_ok or multi_allowed):
                    print(
                        f"{_ts()} [RISK] No order: already hold the other leg of "
                        f"this market; {flip_detail}; {lock_detail}."
                    )
                    await _cooldown()
                    continue
                if lock_ok:
                    print(f"{_ts()} [PAIR] completing the pair: {lock_detail}")
                elif multi_allowed:
                    print(f"{_ts()} [MULTI] complement guard stood down by "
                          f"PHASE2_MULTI_SIGNAL")

            # Each remaining signal trades its own side, BEFORE the price leg's
            # own probe runs: a price leg that cannot fill must not silently
            # suppress the other two. When a signal disagrees with
            # SIG PRICE this deliberately buys the complement, so the guard is
            # stood down here by explicit configuration. PAPER only: two venue
            # orders are not an atomic pair and LIVE must never hold both legs
            # by accident. The exposure cap is still enforced per leg.
            extras_started = time.monotonic()
            if config.PHASE2_MULTI_SIGNAL:
                for source, extra_side in (("book", final_book_side),
                                           ("chainlink", final_chainlink_side)):
                    if extra_side not in ("UP", "DOWN") or extra_side == side:
                        continue
                    extra_ceiling = config.entry_cost_ceiling(config.MAX_BUY_PRICE)
                    if round_exposure + extra_ceiling > config.MAX_ROUND_EXPOSURE + 1e-9:
                        print(f"{_ts()} [MULTI] {source} leg skipped: round exposure "
                              f"cap (${round_exposure:.2f}/"
                              f"${config.MAX_ROUND_EXPOSURE:.2f}).")
                        continue
                    extra_token = up_id if extra_side == "UP" else down_id
                    # This leg disagrees with the order side, so if that side is
                    # held it is the complement - and completing a pair is only
                    # worth doing when both entries plus both fees stay under
                    # the $1.00 the pair redeems for. Same rule in PAPER and
                    # LIVE so the paper run rehearses what live will do.
                    held_side_token = up_id if side == "UP" else down_id
                    if config.PAIR_LOCK_ENABLED and held_side_token in held_tokens:
                        pair_ok, pair_detail = _pair_lock_permit(
                            tokens["condition_id"], held_side_token,
                            config.MAX_BUY_PRICE)
                        if not pair_ok:
                            print(f"{_ts()} [MULTI] {source} leg skipped: would "
                                  f"complete a losing pair; {pair_detail}")
                            _append_trade({
                                "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                                "phase": f"phase2-{source}",
                                "side": extra_side,
                                "amount": config.BET_SIZE,
                                "price_side": final_price_side or "",
                                "book_side": final_book_side or "",
                                "chainlink_side": final_chainlink_side or "",
                                "result": "skipped_pair_would_lose",
                            })
                            continue
                    # SIG CHAINLINK reads from memory, so it can re-check inside
                    # the broker's pre-submit guard exactly like SIG PRICE does.
                    # SIG BOOK cannot: that guard runs while the broker holds
                    # its state lock immediately before the durable fill, and a
                    # REST read there (409ms median, up to 16s if it retries)
                    # would age the already-quoted book past
                    # ORDERBOOK_MAX_AGE_SECONDS with nothing left to revalidate
                    # it. Re-check the book here instead, off the lock.
                    extra_guard = None
                    if source == "chainlink":
                        extra_guard = (
                            lambda _e=extra_side: _fresh_signal_permit(
                                "chainlink", _e, round_key=active_window,
                                chainlink_start=start_chainlink_price))
                    else:
                        try:
                            _gb, _ga = await asyncio.to_thread(
                                orderbook.get_orderbook, ob_id)
                            still = orderbook.liquidity_signal(_gb, _ga)
                        except Exception as exc:
                            still = None
                            print(f"{_ts()} [MULTI] book leg skipped: re-check "
                                  f"failed ({type(exc).__name__}).")
                        if still != extra_side:
                            if still is not None:
                                print(f"{_ts()} [MULTI] book leg skipped: SIG BOOK "
                                      f"moved {extra_side} -> {still or 'neutral'}.")
                            _append_trade(
                                {
                                    "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                                    "phase": "phase2-book",
                                    "side": extra_side,
                                    "amount": config.BET_SIZE,
                                    "price_side": final_price_side or "",
                                    "book_side": final_book_side or "",
                                    "chainlink_side": final_chainlink_side or "",
                                    "result": "skipped_signal_moved",
                                }
                            )
                            continue
                    try:
                        await asyncio.to_thread(
                            orderbook.validate_buy_liquidity, extra_token,
                            config.BET_SIZE, config.MAX_BUY_PRICE,
                            config.MAX_ALLOWED_SPREAD,
                            min_price=config.MIN_BUY_PRICE)
                    except ValueError as exc:
                        print(f"{_ts()} [MULTI] {source} leg skipped: {extra_side} "
                              f"is not buyable - {exc}.")
                        extra_result = "skipped_unfillable"
                    except Exception as exc:
                        print(f"{_ts()} [MULTI] {source} leg probe failed: "
                              f"{type(exc).__name__}: {exc}")
                        extra_result = "skipped_unfillable"
                    else:
                        print(f"{_ts()} [MULTI] SIG {source.upper()}={extra_side} "
                              f"differs from order side {side}; placing its own "
                              f"leg (complement guard stood down)")
                        extra_ok = await asyncio.to_thread(
                            place_trade, extra_side, config.BET_SIZE,
                            up_id, down_id, tokens["condition_id"], round_end,
                            pre_submit_guard=extra_guard)
                        if extra_ok:
                            round_exposure += extra_ceiling
                            held_tokens.add(extra_token)
                            extra_result = "paper_filled"
                        else:
                            extra_result = "rejected_or_unsubmitted"
                            print(f"{_ts()} [MULTI] {source} leg NOT placed - "
                                  f"{polymarket_trade.last_order_error or 'unknown'}")
                    _append_trade(
                        {
                            "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                            "phase": f"phase2-{source}",
                            "side": extra_side,
                            "amount": config.BET_SIZE,
                            "price_side": final_price_side or "",
                            "book_side": final_book_side or "",
                            "chainlink_side": final_chainlink_side or "",
                            "result": extra_result,
                        }
                    )
            # Near expiry the selected token can lose all offers.  Book
            # liquidity is transient, so a failed probe skips this attempt
            # only; the next scheduled attempt probes the live book again.
            selected_bids, selected_asks = final_bids, final_asks
            try:
                selected_bids, selected_asks = await asyncio.to_thread(
                    orderbook.validate_buy_liquidity,
                    up_id if side == "UP" else down_id,
                    config.BET_SIZE, config.MAX_BUY_PRICE, config.MAX_ALLOWED_SPREAD,
                    min_price=config.MIN_BUY_PRICE)
            except ValueError as exc:
                if not config.PHASE2_MUST_FIRE:
                    print(
                        f"{_ts()} [RISK] No order this attempt: "
                        f"{side} is not buyable - {exc}."
                    )
                    _append_trade(
                        {
                            "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                            "phase": "phase2",
                            "side": side,
                            "amount": config.BET_SIZE,
                            "price_side": final_price_side or "",
                            "book_side": final_book_side or "",
                            "chainlink_side": final_chainlink_side or "",
                            "result": "skipped_unfillable",
                        }
                    )
                    await _cooldown()
                    continue
                print(
                    f"{_ts()} [BOT] {side} probe unfillable ({exc}); "
                    f"PHASE2_MUST_FIRE still submits."
                )
            except Exception as exc:
                if not config.PHASE2_MUST_FIRE:
                    print(f"{_ts()} [MARKET] Liquidity probe failed: {type(exc).__name__}: {exc}")
                    await _cooldown(1.0)
                    continue
                print(
                    f"{_ts()} [MARKET] Liquidity probe failed: {type(exc).__name__}: {exc}; "
                    f"PHASE2_MUST_FIRE still submits."
                )

            validation_limit = min(
                config.BTC_STALE_AFTER,
                config.TWAP_STALE_AFTER,
                config.ORDERBOOK_MAX_AGE_SECONDS,
            )
            # The multi-signal legs run inside this window and each costs a
            # book read, a probe and a modeled fill - roughly 625ms apiece.
            # Left in, two of them eat 1.25s of a 3s budget and the price leg
            # loses its own trade to work done for other signals. They are
            # independent legs with their own guards, and everything the price
            # leg uses is re-read below at submission, so their time is
            # excluded rather than charged to it.
            validation_started += time.monotonic() - extras_started
            validation_age = time.monotonic() - validation_started
            try:
                from dashboard import probe as _probe
                _probe.publish_latency("validate", validation_age * 1000.0)
            except Exception:
                pass
            if validation_age > validation_limit and not config.PHASE2_MUST_FIRE:
                print(
                    f"{_ts()} [RISK] No order: validation took {validation_age:.3f}s "
                    f"(limit {validation_limit:.3f}s)."
                )
                await asyncio.sleep(0.2)
                continue

            submit_lp, _submit_lp_ts = price_ws.fresh_snapshot(config.BTC_STALE_AFTER)
            submit_cl = current_chainlink_twap()
            if ((submit_lp is None
                 and not config.SIGNAL_PRICE_FALLBACK_COMBINED)
                    or (submit_cl is None
                        and not config.PHASE2_PARTIAL_SIGNALS
                        and not config.SIGNAL_PRICE_FALLBACK_COMBINED)):
                if not config.PHASE2_MUST_FIRE:
                    print(f"{_ts()} [RISK] No order: a price feed went stale before submission.")
                    await asyncio.sleep(0.2)
                    continue
                print(f"{_ts()} [BOT] A price feed went stale; PHASE2_MUST_FIRE keeps {side}.")
            submit_book_side = (
                orderbook.liquidity_signal(selected_bids, selected_asks)
                if side == "UP" else final_book_side
            )
            submit_price_side = price_signal(active_window, start_price, submit_lp)
            submit_chainlink_side = chainlink_signal(
                active_window, start_chainlink_price, submit_cl)
            _submit_diagnostic_side = strategy.final_decision(
                submit_price_side, submit_book_side, submit_chainlink_side)
            submit_authority_side = _authority_side(
                submit_price_side, submit_book_side, submit_chainlink_side)
            if (submit_price_side is None
                    and not config.SIGNAL_PRICE_FALLBACK_COMBINED):
                signal_epoch.observe(None)
                if not config.PHASE2_MUST_FIRE:
                    print(f"{_ts()} [RISK] No order: SIG PRICE is neutral immediately before submission.")
                    await asyncio.sleep(0.2)
                    continue
                print(f"{_ts()} [BOT] SIG PRICE is neutral; PHASE2_MUST_FIRE keeps {side}.")
            if submit_authority_side is None and config.PHASE2_MUST_FIRE:
                submit_authority_side = side
            signal_epoch.observe(submit_authority_side)
            if submit_authority_side is None:
                print(f"{_ts()} [RISK] No order: deciding signals are tied or "
                      "unavailable immediately before submission.")
                await asyncio.sleep(0.2)
                continue
            if submit_authority_side != side:
                if config.PHASE2_MUST_FIRE:
                    print(
                        f"{_ts()} [BOT] Decision changed before submission "
                        f"({side} -> {submit_authority_side}); firing {submit_authority_side}."
                    )
                    side = submit_authority_side
                else:
                    print(
                        f"{_ts()} [RISK] No order: configured decision changed immediately "
                        f"before submission ({side} -> {submit_authority_side})."
                    )
                    await asyncio.sleep(0.2)
                    continue
            price_side = submit_price_side
            book_side = submit_book_side
            chainlink_side = submit_chainlink_side

            action_wall = timer.unix()
            if (timer.window_start(action_wall) != active_window
                    or action_wall >= round_end - config.MIN_SECONDS_TO_EXPIRY):
                print(f"{_ts()} [RISK] No order: round changed after final validation.")
                await asyncio.sleep(0.2)
                continue
            if not _execution_ready(mode, tokens["condition_id"]):
                print(f"{_ts()} [RISK] No order: private fill stream lost "
                      "readiness before submission.")
                await asyncio.sleep(0.2)
                continue

            # The cap was checked before the multi-signal legs ran, and each of
            # those spends against the same round budget. Without re-checking
            # here the price leg can push the round up to two extra entries
            # past MAX_ROUND_EXPOSURE - the exact limit this check exists to
            # enforce. Cheap to repeat, and it is the last point where it can
            # still be honoured.
            if round_exposure + entry_ceiling > config.MAX_ROUND_EXPOSURE + 1e-9:
                print(
                    f"{_ts()} [RISK] No order: round exposure cap reached after "
                    f"the multi-signal legs "
                    f"(${round_exposure:.2f}/${config.MAX_ROUND_EXPOSURE:.2f})."
                )
                await _cooldown()
                continue

            verb = "Simulating live-book FOK" if mode == "PAPER" else "Placing trade"
            print(f"{_ts()} [BOT] {verb}: {side} ${config.BET_SIZE}")
            if config.PHASE2_MUST_FIRE:
                pre_submit_guard = lambda: True
            else:
                pre_submit_guard = lambda: _fresh_authority_permit(
                    active_window, start_price, start_chainlink_price,
                    submit_book_side, side,
                    signal_observer=signal_epoch.observe)
            ok = await asyncio.to_thread(
                place_trade, side, config.BET_SIZE, up_id, down_id,
                tokens["condition_id"], round_end,
                pre_submit_guard=pre_submit_guard)
            if ok:
                round_exposure += entry_ceiling
                # Shared with durable restart recovery. LIVE and default PAPER
                # block the complement; the explicit PAPER experiment consults
                # the accepted-side epoch above.
                held_tokens.add(up_id if side == "UP" else down_id)
                signal_epoch.record_accepted(side)
                if mode == "PAPER":
                    result = "paper_filled"
                else:
                    result = (polymarket_trade.last_order_status or
                              "accepted_pending_confirmation").lower()
            else:
                result = "rejected_or_unsubmitted"
            if not ok:
                # Read the live module attribute. Importing this immutable
                # string directly leaves us holding its original None value.
                reason = polymarket_trade.last_order_error or "unknown"
                hint = ""
                if reason and "not enough balance" in reason.lower():
                    hint = " - Deposit USDC on Polygon and enable trading at polymarket.com"
                elif reason and "future-dated" in reason.lower():
                    hint = (
                        " - Local clock is behind the CLOB; sync Windows Time "
                        "(start the service, then `w32tm /resync`) and restart"
                    )
                print(f"{_ts()} [BOT] Order was NOT placed - reason: {reason}{hint}")

            _append_trade(
                {
                    "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                    "phase": "phase2",
                    "side": side,
                    "amount": config.BET_SIZE,
                    "price_side": price_side or "",
                    "book_side": book_side or "",
                    "chainlink_side": chainlink_side or "",
                    "result": result,
                }
            )
            if (ok and mode == "LIVE"
                    and not (polymarket_trade.last_order_receipt or {}).get(
                        "accounting_journaled")):
                print(f"{_ts()} [RISK] CRITICAL: order matched but its accounting authorization was not durable; stopping.")
                stop_event.set()

            print(
                f"{_ts()} [BOT] Trade call done ({result}). Sleeping "
                f"{config.TRADE_INTERVAL_SECONDS:g}s then continuing."
            )
            await _cooldown()
            continue

        elif _in_late_trim_window(exact_remaining):
            round_exposure, held_tokens = await _run_late_trim(
                mode, exact_remaining, active_window, round_end,
                start_price, start_chainlink_price, lp,
                round_exposure, held_tokens, signal_epoch, trim)
            continue

        await asyncio.sleep(0.2)


async def main():
    """Canonical safe entrypoint: paper mode unless ``run_feeds --live``."""
    from run_feeds import run
    await run(dash=False, paper=True)


if __name__ == "__main__":
    asyncio.run(main())
