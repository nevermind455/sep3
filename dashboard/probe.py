"""Read-only instrumentation.

Every wrapper here calls the original function with the original arguments
and returns the original return value, unmodified. Telemetry is recorded in
a try/except so that a bug in the dashboard can never propagate into a
trading call. Nothing in this file changes a decision, a price, a size, an
order type, or the control flow of run_bot().

Attribute-binding note (this is why some patches target main_bot and some
target the source module):

    main_bot does `import orderbook`      -> patch orderbook.get_orderbook
    main_bot does `from polymarket_trade import place_trade`
                                          -> the name is already bound in
                                             main_bot, so patch main_bot.place_trade
"""
from __future__ import annotations

import io
import logging
import os
import re
import stat
import sys
import threading
import time
import traceback
from pathlib import Path

from .safety import exception_summary, terminal_text
from .state import TerminalState

_installed = False
_orig_stdout = None
_orig_stderr = None
_orig_sys_excepthook = None
_orig_threading_excepthook = None
_originals: dict[tuple[object, str], object] = {}
_sink = None
_stderr_sink = None
_log_handler = None
_asyncio_bindings: list[tuple[object, object]] = []
_lifecycle_lock = threading.RLock()


def _telemetry_failed(state: TerminalState, surface: str, exc: Exception) -> None:
    """Record a probe failure without letting dashboard code reach trading."""
    detail = f"{surface}: {exception_summary(exc)}"
    with state.lock():
        state.telemetry_error = detail
        state.event("DASH", detail, "warn")


# --------------------------------------------------------------- stdout ---
class EventSink(io.TextIOBase):
    """Swallows the bot's print() output and turns it into feed rows.

    Without this the bot's prints land in the middle of the frame and every
    later cursor write is off by one row — the failure mode looks like the
    dashboard corrupting itself, and it shows up exactly during an incident
    when the bot is printing most.
    """

    TAG = re.compile(r"\[(?P<tag>[A-Z]+)\]\s*(?P<msg>.*)$")
    LEVELS = {
        "error": "bad", "fail": "bad", "not placed": "bad", "warn": "warn",
        "could not": "warn", "reconnect": "warn", "skipping": "warn",
        "placed": "good", "connected": "good", "cancelled": "info",
    }
    MAX_LINE = 8192

    def __init__(self, state: TerminalState, mirror=None, *,
                 default_tag: str = "LOG", default_level: str = "info",
                 force_level: str | None = None) -> None:
        super().__init__()
        self.state = state
        self.mirror = mirror          # optional tee to a file
        self.default_tag = default_tag
        self.default_level = (default_level
                              if default_level in ("info", "good", "warn", "bad")
                              else "info")
        # Sinks capturing stderr/logging want every line at the same severity
        # regardless of what its text looks like: a stderr line with the word
        # "connected" in it is still an error line, not a good one.
        self.force_level = (force_level
                            if force_level in ("info", "good", "warn", "bad")
                            else None)
        # Per-thread partial-line buffers.  A shared buffer would let two
        # concurrent print() calls (write body / write "\n") from different
        # threads splice each other's lines together in the event feed.
        self._bufs: dict[int, list[str]] = {}
        self._truncated: dict[int, bool] = {}
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not s:
            return 0
        if not isinstance(s, str):
            raise TypeError(f"write() argument must be str, not {type(s).__name__}")
        tid = threading.get_ident()
        completed: list[str] = []
        with self._lock:
            buf = self._bufs.get(tid)
            if buf is None:
                buf = []
                self._bufs[tid] = buf
            truncated = self._truncated.get(tid, False)
            pieces = s.split("\n")
            for index, piece in enumerate(pieces):
                used = sum(len(p) for p in buf)
                room = max(0, self.MAX_LINE - used)
                if piece:
                    buf.append(piece[:room])
                    if len(piece) > room:
                        truncated = True
                if index < len(pieces) - 1:
                    suffix = " ...<truncated>" if truncated else ""
                    completed.append("".join(buf) + suffix)
                    buf.clear()
                    truncated = False
            if buf:
                self._truncated[tid] = truncated
            else:
                # No partial line pending — drop the per-thread bookkeeping so
                # short-lived worker threads do not leak entries into _bufs.
                self._bufs.pop(tid, None)
                self._truncated.pop(tid, None)
        for line in completed:
            self._emit(line)
        return len(s)

    def flush(self) -> None:
        if self.mirror:
            try:
                self.mirror.flush()
            except Exception as exc:
                _telemetry_failed(self.state, "log flush", exc)

    def finish(self) -> None:
        """Emit any per-thread partial lines and close the optional mirror."""
        pending: list[str] = []
        with self._lock:
            for tid, buf in list(self._bufs.items()):
                if buf:
                    suffix = " ...<truncated>" if self._truncated.get(tid) else ""
                    pending.append("".join(buf) + suffix)
            self._bufs.clear()
            self._truncated.clear()
            mirror, self.mirror = self.mirror, None
        for line in pending:
            self._emit(line)
        if mirror is not None:
            try:
                mirror.flush()
                mirror.close()
            except Exception as exc:
                _telemetry_failed(self.state, "log close", exc)

    def _emit(self, line: str) -> None:
        raw = terminal_text(line.rstrip(), self.MAX_LINE)
        if not raw:
            return
        if self.mirror:
            try:
                self.mirror.write(raw + "\n")
            except Exception as exc:
                _telemetry_failed(self.state, "log mirror", exc)
        # strip the bot's own "[Aug 08 12:00:00 ET]" prefix, the panel has a clock
        body = re.sub(r"^\[[A-Za-z]{3} \d{2} \d{2}:\d{2}:\d{2} ET\]\s*", "", raw)
        m = self.TAG.match(body)
        tag, msg = (m.group("tag"), m.group("msg")) if m else (self.default_tag, body)
        if self.force_level is not None:
            level = self.force_level
        else:
            low = msg.lower()
            level = self.default_level
            for needle, lv in self.LEVELS.items():
                if needle in low:
                    level = lv
                    break
        self.state.event(tag, msg, level)
        _parse_round_state(self.state, tag, msg)


class _StateLogHandler(logging.Handler):
    """logging.Handler that routes records through the state event feed.

    Without this, `logging.getLogger(...).exception(...)` prints a traceback
    to stderr while the alt screen is displayed — the exact way a settlement
    or feed exception used to shred the dashboard.
    """

    _LEVEL_MAP = {
        logging.DEBUG: "info", logging.INFO: "info",
        logging.WARNING: "warn", logging.ERROR: "bad",
        logging.CRITICAL: "bad",
    }

    def __init__(self, state: TerminalState) -> None:
        super().__init__(level=logging.INFO)
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = self._LEVEL_MAP.get(record.levelno, "info")
            msg = record.getMessage()
            if record.exc_info:
                # One-line summary; the panel is not a stack-trace viewer.
                exc_type = record.exc_info[0]
                exc_val = record.exc_info[1]
                type_name = exc_type.__name__ if exc_type else "Exception"
                msg = f"{msg}: {type_name}: {exc_val}"
            tag = terminal_text(record.name.split(".")[-1], 12).upper() or "LOG"
            self.state.event(tag, msg, level)
        except Exception:
            # A logging handler that raises is worse than one that swallows —
            # the logging module would print the failure to stderr, which is
            # what this handler exists to prevent.
            pass


def _install_excepthooks(state: TerminalState) -> None:
    """Route uncaught main-thread and worker-thread exceptions to the panel.

    Python's defaults print a full traceback to stderr, which - while the alt
    screen is active - lands directly on top of the dashboard frame.
    """
    global _orig_sys_excepthook, _orig_threading_excepthook

    _orig_sys_excepthook = sys.excepthook

    def _sys_hook(exc_type, exc, tb):
        try:
            summary = f"{exc_type.__name__}: {exc}" if exc_type else str(exc)
            state.event("PANIC", terminal_text(summary, 480), "bad")
            # Keep the full traceback in the private mirror if one exists.
            if _sink is not None and _sink.mirror is not None:
                for line in traceback.format_exception(exc_type, exc, tb):
                    for chunk in line.rstrip("\n").split("\n"):
                        try:
                            _sink.mirror.write(chunk + "\n")
                        except Exception:
                            break
        except Exception:
            # Fall back to the previous hook only when nothing dashboard-side
            # can capture the crash. That path writes to stderr and bleeds
            # into the frame — a knowingly-degraded state, but better than
            # eating the traceback silently.
            try:
                if _orig_sys_excepthook is not None:
                    _orig_sys_excepthook(exc_type, exc, tb)
            except Exception:
                pass

    sys.excepthook = _sys_hook

    if hasattr(threading, "excepthook"):
        _orig_threading_excepthook = threading.excepthook

        def _thread_hook(args) -> None:
            try:
                name = getattr(args.thread, "name", "thread")
                exc_type = args.exc_type
                exc = args.exc_value
                summary = (f"{name}: {exc_type.__name__}: {exc}"
                           if exc_type else f"{name}: {exc}")
                state.event("THREAD", terminal_text(summary, 480), "bad")
                if _sink is not None and _sink.mirror is not None:
                    for line in traceback.format_exception(exc_type, exc,
                                                           args.exc_traceback):
                        for chunk in line.rstrip("\n").split("\n"):
                            try:
                                _sink.mirror.write(chunk + "\n")
                            except Exception:
                                break
            except Exception:
                try:
                    if _orig_threading_excepthook is not None:
                        _orig_threading_excepthook(args)
                except Exception:
                    pass

        threading.excepthook = _thread_hook


def _restore_excepthooks() -> None:
    global _orig_sys_excepthook, _orig_threading_excepthook
    if _orig_sys_excepthook is not None:
        sys.excepthook = _orig_sys_excepthook
        _orig_sys_excepthook = None
    if _orig_threading_excepthook is not None and hasattr(threading, "excepthook"):
        threading.excepthook = _orig_threading_excepthook
        _orig_threading_excepthook = None


def attach_asyncio(loop, state: TerminalState) -> None:
    """Route asyncio's default 'exception in task' printout into the panel.

    Must be called from the loop's own thread. Safe to call again; the previous
    handler is remembered so uninstall() can restore it.
    """
    if loop is None or state is None:
        return
    with _lifecycle_lock:
        prev = None
        try:
            prev = loop.get_exception_handler()
        except Exception:
            prev = None

        def _handler(_loop, context):
            try:
                msg = context.get("message") or ""
                exc = context.get("exception")
                if exc is not None:
                    summary = f"{type(exc).__name__}: {exc}"
                    if msg:
                        summary = f"{msg}: {summary}"
                else:
                    summary = msg or "asyncio exception"
                state.event("ASYNC", terminal_text(summary, 480), "bad")
            except Exception:
                pass

        try:
            loop.set_exception_handler(_handler)
        except Exception as exc:
            _telemetry_failed(state, "asyncio hook", exc)
            return
        _asyncio_bindings.append((loop, prev))


def _detach_asyncio() -> None:
    for loop, prev in reversed(_asyncio_bindings):
        try:
            loop.set_exception_handler(prev)
        except Exception:
            pass
    _asyncio_bindings.clear()


_RE_START = re.compile(r"start_price=\$([0-9,]+\.?[0-9]*)")
_RE_CL_START = re.compile(
    r"Chainlink(?: 60s TWAP)? start_price=\$([0-9,]+\.?[0-9]*)")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _parse_round_state(state: TerminalState, tag: str, msg: str) -> None:
    """Keep log-derived opening observations as a telemetry fallback."""
    try:
        if tag == "ROUND":
            m = _RE_CL_START.search(msg)
            if m:
                import timer

                price = _num(m.group(1))
                bot_round = timer.window_start(timer.unix())
                state.mark_strategy_round(bot_round)
                accepted = state.push_price_to_beat(
                    price, source="ROUND log line", round_key=bot_round)
                if accepted:
                    state.flash("NEW ROUND", f"PRICE TO BEAT ${price:,.2f}",
                                "info", ttl=1.8)
                return
            m = _RE_START.search(msg)
            if m:
                with state.lock():
                    state.start_price.set(_num(m.group(1)), source="ROUND log line")
    except Exception as exc:
        _telemetry_failed(state, "round parser", exc)


# -------------------------------------------------------------- wrappers ---
def _patch(obj, name: str, factory) -> None:
    key = (obj, name)
    if key in _originals:
        raise RuntimeError(f"probe target already patched: {getattr(obj, '__name__', obj)!s}.{name}")
    try:
        original = getattr(obj, name)
        wrapped = factory(original)
        _originals[key] = original
        setattr(obj, name, wrapped)
    except Exception:
        raise


def _restore_patches() -> list[str]:
    failures: list[str] = []
    for (obj, name), original in reversed(list(_originals.items())):
        try:
            setattr(obj, name, original)
        except Exception as exc:
            failures.append(
                f"probe restore {getattr(obj, '__name__', type(obj).__name__)}.{name}: "
                f"{exception_summary(exc)}"
            )
            continue
    _originals.clear()
    return failures


def _open_private_mirror(path_value: str):
    """Open an append-only, non-inheritable, regular log file.

    O_NOFOLLOW blocks a pre-planted symlink where the platform supports it;
    mode 0600 prevents other local users from reading live-wallet diagnostics.
    """
    path = Path(path_value).expanduser()
    if path.exists() and path.is_symlink():
        raise OSError("refusing symlink log mirror")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("log mirror must be a regular file")
        os.set_inheritable(fd, False)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8", buffering=1)
    except Exception:
        os.close(fd)
        raise


def install(state: TerminalState, mirror_path: str | None = None):
    """Atomically install probes; roll back every side effect on failure."""
    global _installed, _orig_stdout, _orig_stderr, _sink, _stderr_sink, _log_handler
    with _lifecycle_lock:
        try:
            return _install_locked(state, mirror_path)
        except Exception as exc:
            failures = _restore_patches()
            _detach_asyncio()
            _restore_excepthooks()
            if _log_handler is not None:
                try:
                    logging.getLogger().removeHandler(_log_handler)
                except Exception:
                    pass
                _log_handler = None
            if _stderr_sink is not None:
                if sys.stderr is _stderr_sink and _orig_stderr is not None:
                    sys.stderr = _orig_stderr
                _stderr_sink.finish()
                _stderr_sink = None
            if _sink is not None:
                if sys.stdout is _sink and _orig_stdout is not None:
                    sys.stdout = _orig_stdout
                _sink.finish()
            _sink = None
            _orig_stdout = None
            _orig_stderr = None
            _installed = False
            state.event("DASH", f"probe install rolled back: {exception_summary(exc)}", "bad")
            for failure in failures:
                state.event("DASH", failure, "bad")
            raise


def _install_locked(state: TerminalState, mirror_path: str | None = None):
    """Install probes. Returns the saved real stdout for the renderer."""
    global _installed, _orig_stdout, _orig_stderr, _sink, _stderr_sink, _log_handler
    if _installed:
        if _sink is not None and _sink.state is not state:
            raise RuntimeError("dashboard probes are already attached to another state")
        return _orig_stdout

    import config
    import main_bot
    import market_discovery
    import orderbook
    import polymarket_trade
    import strategy

    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr

    with state.lock():
        state.bet_size = config.BET_SIZE
        state.trade_window = config.TRADE_LAST_SECONDS
        state.max_buy_price = config.MAX_BUY_PRICE
        state.min_buy_price = config.MIN_BUY_PRICE
        state.mode = str(getattr(main_bot, "execution_mode", "LIVE") or "LIVE").upper()

    # ---- orderbook -------------------------------------------------------
    def wrap_book(orig):
        def inner(token_id, *a, **kw):
            t0 = time.monotonic()
            out = orig(token_id, *a, **kw)
            try:
                bids, asks = out
                state.push_book(token_id, bids, asks, (time.monotonic() - t0) * 1000.0)
            except Exception as exc:
                _telemetry_failed(state, "book probe", exc)
            return out
        return inner
    _patch(orderbook, "get_orderbook", wrap_book)

    def wrap_liq(orig):
        def inner(bids, asks):
            out = orig(bids, asks)
            try:
                with state.lock():
                    if (state.round_key is not None
                            and state.strategy_round_key == state.round_key):
                        state.sig_book.set(out)
            except Exception as exc:
                _telemetry_failed(state, "liquidity probe", exc)
            return out
        return inner
    _patch(orderbook, "liquidity_signal", wrap_liq)

    # ---- strategy --------------------------------------------------------
    # Price and Chainlink signals are explicitly tagged with their bot round.
    # Inferring the source from alternating decide() calls breaks as soon as a
    # phase abstains or performs an extra pre-submit validation.
    def wrap_price_signal(orig):
        def inner(round_key, start, current):
            out = orig(round_key, start, current)
            try:
                with state.lock():
                    if state.round_key is not None and round_key == state.round_key:
                        if start is not None:
                            state.start_price.set(
                                start, source="main_bot.price_signal arg")
                        state.sig_price.set(out)
            except Exception as exc:
                _telemetry_failed(state, "price signal probe", exc)
            return out
        return inner
    _patch(main_bot, "price_signal", wrap_price_signal)

    def wrap_chainlink_signal(orig):
        def inner(round_key, start, current):
            out = orig(round_key, start, current)
            try:
                with state.lock():
                    if state.round_key is not None and round_key == state.round_key:
                        if start is not None:
                            state.push_price_to_beat(
                                start, source="main_bot.chainlink_signal arg",
                                round_key=round_key)
                        state.sig_chainlink.set(out)
            except Exception as exc:
                _telemetry_failed(state, "Chainlink signal probe", exc)
            return out
        return inner
    _patch(main_bot, "chainlink_signal", wrap_chainlink_signal)

    def wrap_final(orig):
        def inner(price_side, book_side, chainlink_side):
            out = orig(price_side, book_side, chainlink_side)
            try:
                with state.lock():
                    if (state.round_key is not None
                            and state.strategy_round_key == state.round_key):
                        state.decision.set(out)
                        state.decision_forced = False
            except Exception as exc:
                _telemetry_failed(state, "decision probe", exc)
            return out
        return inner
    _patch(strategy, "final_decision", wrap_final)

    # ---- market discovery ------------------------------------------------
    def wrap_tokens(orig):
        def inner(*a, **kw):
            t0 = time.monotonic()
            out = orig(*a, **kw)
            try:
                with state.lock():
                    state.tokens.set(out, latency_ms=(time.monotonic() - t0) * 1000.0)
                    # H6: a previous-round market is older than the live window.
                    # Prewarming the *next* slug is expected and is not a fallback.
                    got = out.get("window_start") if isinstance(out, dict) else None
                    want = market_discovery._current_5m_window_start_unix()
                    try:
                        state.token_fallback = got is not None and int(got) < int(want)
                    except (TypeError, ValueError, OverflowError) as exc:
                        state.token_fallback = False
                        _telemetry_failed(state, "market window", exc)
                    if state.token_fallback:
                        state.event("MARKET", f"PREV-WINDOW FALLBACK {out.get('slug')}", "bad")
            except Exception as exc:
                _telemetry_failed(state, "market probe", exc)
            return out
        return inner
    _patch(market_discovery, "get_tokens_for_current_round", wrap_tokens)

    # ---- loop heartbeat --------------------------------------------------
    # run_bot calls seconds_left() once per iteration, so wrapping it gives a
    # true liveness signal and the round clock exactly as the BOT sees it —
    # not a second clock computed by the renderer.
    def wrap_secs(orig):
        def inner(*a, **kw):
            out = orig(*a, **kw)
            try:
                with state.lock():
                    state.loop_beat.set(out)
                    state.seconds_left = out
            except Exception as exc:
                _telemetry_failed(state, "clock probe", exc)
            return out
        return inner
    _patch(main_bot, "seconds_left", wrap_secs)

    # ---- execution (bound into main_bot at import time) -------------------
    def wrap_place(orig):
        def inner(side, amount, up_id=None, down_id=None, *args, **kwargs):
            t0 = time.monotonic()
            ok = orig(side, amount, up_id, down_id, *args, **kwargs)
            try:
                ms = (time.monotonic() - t0) * 1000.0
                try:
                    state.latency.observe("total", ms)
                except Exception:
                    pass
                # main_bot's own `last_order_error` copy is bound at import and
                # stays None (C3). Read the live module attribute instead.
                err = getattr(polymarket_trade, "last_order_error", None)
                with state.lock():
                    paper = state.mode == "PAPER"
                state.record_order(side, amount, bool(ok), err, ms,
                                   count_stake=paper)
                if ok:
                    suffix = "PAPER FILLED" if paper else "SENT FOK"
                    clean_amount = TerminalState._finite(amount, nonnegative=True)
                    amount_text = f"${clean_amount:.2f}" if clean_amount is not None else "$--"
                    state.flash(f"ENTRY {side}", f"{amount_text} {suffix}", "good", 2.4)
                else:
                    state.flash("ORDER REJECTED", terminal_text(err or "unknown", 44), "bad", 3.0)
            except Exception as exc:
                _telemetry_failed(state, "order probe", exc)
            return ok
        return inner
    _patch(main_bot, "place_trade", wrap_place)

    def wrap_cancel(orig):
        def inner(*a, **kw):
            t0 = time.monotonic()
            out = orig(*a, **kw)
            try:
                with state.lock():
                    state.cancel.set(bool(out), latency_ms=(time.monotonic() - t0) * 1000.0)
            except Exception as exc:
                _telemetry_failed(state, "cancel probe", exc)
            return out
        return inner
    _patch(main_bot, "cancel_all_open_orders", wrap_cancel)

    def wrap_balance(orig):
        def inner(*a, **kw):
            out = orig(*a, **kw)
            try:
                with state.lock():
                    state.balance.set(out)
            except Exception as exc:
                _telemetry_failed(state, "balance probe", exc)
            return out
        return inner
    _patch(main_bot, "get_balance_allowance", wrap_balance)

    # ---- honest gap register --------------------------------------------
    gaps = {
        "REDEMPTION": (
            "official outcomes settle the local ledger, but this build does not "
            "submit an on-chain redeem transaction"
        ),
    }
    for key, why in gaps.items():
        state.note_absent(key, why)

    mirror = _open_private_mirror(mirror_path) if mirror_path else None
    _sink = EventSink(state, mirror)
    # stderr shares the mirror file (one journal per process) but tags every
    # captured line as an error and never lets a "connected"-ish substring
    # promote the level to good/info.
    _stderr_sink = EventSink(state, mirror, default_tag="STDERR",
                             default_level="bad", force_level="bad")
    sys.stdout = _sink
    sys.stderr = _stderr_sink
    _install_excepthooks(state)
    _log_handler = _StateLogHandler(state)
    _log_handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(_log_handler)
    # If nothing else set a level, INFO is the sensible floor: anything the
    # bot chose to log about is worth surfacing to the operator.
    if root.level == logging.WARNING or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    _installed = True
    return _orig_stdout


def uninstall() -> None:
    """Restore every patched attribute and stdout. Used by the tests."""
    global _installed, _orig_stdout, _orig_stderr, _sink, _stderr_sink, _log_handler
    with _lifecycle_lock:
        state = _sink.state if _sink is not None else None
        _detach_asyncio()
        _restore_excepthooks()
        if _log_handler is not None:
            try:
                logging.getLogger().removeHandler(_log_handler)
            except Exception:
                pass
            _log_handler = None
        failures = _restore_patches()
        if _stderr_sink is not None:
            if sys.stderr is _stderr_sink and _orig_stderr is not None:
                sys.stderr = _orig_stderr
            _stderr_sink.finish()
        _stderr_sink = None
        if _sink is not None:
            if sys.stdout is _sink and _orig_stdout is not None:
                sys.stdout = _orig_stdout
            _sink.finish()
        _sink = None
        _orig_stdout = None
        _orig_stderr = None
        _installed = False
        if state is not None:
            for failure in failures:
                state.event("DASH", failure, "bad")


def is_installed() -> bool:
    """Whether the dashboard has captured stdout/stderr/logging in this process."""
    return _installed


def publish_latency(stage: str, ms: float) -> None:
    """Record one submit-path stage timing on the installed state, if any.

    Callers do not have to know whether the dashboard is attached: this
    silently no-ops if no probe is installed, so the hot path can call it
    unconditionally.
    """
    sink = _sink
    if sink is None:
        return
    registry = getattr(sink.state, "latency", None)
    if registry is None:
        return
    try:
        registry.observe(stage, ms)
    except Exception:
        # A metrics failure must never propagate into the trading loop.
        pass
