#!/usr/bin/env python3
"""Run the bot on the hardened feed layer.

    python run_feeds.py                 # PAPER: live data + simulated FOK
    python run_feeds.py --dash          # PAPER + terminal dashboard
    python run_feeds.py --live --dash   # explicit live-wallet mode
    python run_feeds.py --health        # print feed health as JSON, no trading
    python main_bot.py                  # direct bot entrypoint

Configuration (see feeds/adapters.py for the full contract):

    BOOK_SOURCE=ws_shadow|rest|ws   default ws_shadow
    PRICE_STALE_POLICY=keep|none    default none
    BTC_FEED=ws|legacy              default ws
    USER_WS=on|off                  default on
    RECONCILE=auto|off              default auto

The order path independently rejects stale data even if a display retains it.
`BOOK_SOURCE=ws` changes a decision input and is announced on startup.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feeds import FeedHub, RestReconciler  # noqa: E402
from feeds import adapters  # noqa: E402
from accounting import Ledger  # noqa: E402
from chainlink_strike import ChainlinkStrike  # noqa: E402
from accounting.settlement import SettlementWorker  # noqa: E402
from feeds.health import safe_log_text  # noqa: E402

EVENTS: list[tuple[float, str, str, str]] = []
_EV_LOCK = threading.Lock()


def _state_path(env_name: str, default_name: str) -> Path:
    """Resolve one runtime-state path relative to this source directory."""
    root = Path(__file__).resolve().parent
    raw = os.environ.get(env_name) or default_name
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _paper_state_paths() -> dict[str, Path]:
    """Resolve every mutable PAPER artifact through its isolated profile."""
    return {
        "ledger": _state_path("PAPER_LEDGER_PATH", "paper_ledger.json"),
        "account": _state_path("PAPER_ACCOUNT_PATH", "paper_account.json"),
        "audit": _state_path("PAPER_AUDIT_PATH", "paper_orders.jsonl"),
        "trade_log": _state_path(
            "PAPER_TRADE_LOG_PATH", "paper_trade_log.csv"),
    }


class _ProcessLock:
    """Prevent two bot processes from sharing one wallet/paper ledger."""

    # Windows byte-range locks are mandatory, not advisory: a lock over a byte
    # that carries content makes the journal unreadable and the file
    # undeletable for every other handle, including this process' own
    # diagnostics. Lock one sentinel byte far past end-of-file instead.
    # Windows permits it, appended metadata never reaches it, and readers are
    # unaffected while exclusion is still enforced.
    _NT_LOCK_OFFSET = 1 << 30

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None
        # Imported here, not in release(). release() runs on the shutdown path,
        # where a fresh import raises "sys.meta_path is None, Python is likely
        # shutting down" and prints a traceback over an otherwise clean exit.
        # The OS lock is freed by closing the handle regardless, so this only
        # ever cost noise - but the noise looked like a failed release.
        self._locker = None
        if os.name == "posix":
            import fcntl
            self._locker = fcntl
        elif os.name == "nt":
            import msvcrt
            self._locker = msvcrt

    def acquire(self) -> None:
        if self._file is not None:
            raise RuntimeError("process lock is already held by this instance")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        handle = os.fdopen(fd, "a+", encoding="utf-8")
        try:
            if os.name == "posix":
                fcntl = self._locker
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif os.name == "nt":
                msvcrt = self._locker
                # msvcrt locks bytes from the current position, so seek to the
                # sentinel byte first. Append mode ignores this position for
                # writes, so the journal below is still appended, not truncated.
                handle.seek(self._NT_LOCK_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                raise RuntimeError(
                    f"single-process locking is unsupported on {os.name!r}")
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise RuntimeError(
                f"cannot acquire {self.path.name}; another bot may be running "
                f"({type(exc).__name__})") from None
        except Exception:
            handle.close()
            raise
        try:
            handle.seek(0, os.SEEK_END)
            handle.write(f"pid={os.getpid()} started={time.time():.3f}\n")
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            # Closing the descriptor releases flock()/locking() as well.
            handle.close()
            raise
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if os.name == "posix" and self._locker is not None:
                self._locker.flock(self._file.fileno(), self._locker.LOCK_UN)
            elif os.name == "nt" and self._locker is not None:
                self._file.seek(self._NT_LOCK_OFFSET)
                self._locker.locking(self._file.fileno(),
                                     self._locker.LK_UNLCK, 1)
        except Exception:
            # Closing the handle below releases the OS lock anyway; a noisy
            # traceback during interpreter teardown helps nobody.
            pass
        finally:
            self._file.close()
            self._file = None


def on_event(source: str, text: str, level: str = "info") -> None:
    """Cheap by contract - feeds call this from receive paths."""
    safe_source = safe_log_text(source, limit=40) or "unknown"
    safe_text = safe_log_text(text, limit=1000)
    safe_level = level if level in ("info", "good", "warn", "bad") else "warn"
    with _EV_LOCK:
        EVENTS.append((time.time(), safe_source, safe_text, safe_level))
        if len(EVENTS) > 2000:
            del EVENTS[:1000]


def creds_from(obj) -> dict:
    """Normalise whatever the SDK hands back into the WS auth shape.

    Field naming differs between the credential object, its dict form and the
    websocket payload, so probe all three rather than assume one.
    """
    if obj is None:
        return {}
    get = obj.get if isinstance(obj, dict) else (lambda k: getattr(obj, k, None))
    out = {
        "apiKey": get("api_key") or get("apiKey") or get("key"),
        "secret": get("api_secret") or get("secret"),
        "passphrase": get("api_passphrase") or get("passphrase"),
    }
    return out if all(out.values()) else {}


def derive_creds() -> dict:
    """L2 API credentials for the user channel.

    `_get_client` has already derived and installed these, so read them off
    the client first; calling `create_or_derive_api_key` again doubles the
    timeout-sensitive round trips that were failing auth at startup. The
    documented call remains the fallback for a client that exposes no
    recognisable attribute.

    Never logged, never written to disk, never put in an exception message.
    """
    if (os.environ.get("USER_WS") or "on").lower() == "off":
        return {}
    try:
        import polymarket_trade
        client = polymarket_trade._get_client()
    except Exception as exc:
        on_event("user_ws", f"no CLOB client: {type(exc).__name__}", "warn")
        return {}

    # `_get_client` already derived and installed creds. Re-calling
    # create_or_derive here doubled the timeout-sensitive round trips.
    for attr in ("creds", "api_creds", "_api_creds"):
        out = creds_from(getattr(client, attr, None))
        if out:
            return out
    for attempt in ("create_or_derive_api_key", "create_or_derive_api_creds",
                    "derive_api_key"):
        fn = getattr(client, attempt, None)
        if not callable(fn):
            continue
        try:
            out = creds_from(fn())
            if out:
                return out
        except Exception as exc:
            on_event("user_ws", f"{attempt} failed: {type(exc).__name__}", "warn")
            continue
    for attr in ("creds", "api_creds", "_api_creds"):
        out = creds_from(getattr(client, attr, None))
        if out:
            return out
    on_event("user_ws", "could not derive L2 credentials; user channel will stay idle. "
                        "Verify the wallet/signature configuration without printing credentials.",
             "warn")
    return {}


def build_hub(*, paper: bool = False, read_only: bool = False):
    """Wire the hub. Order matters: capture the REAL REST book function before
    adapters patch it, or the resync loop would call its own replacement.

    Paper and health-only launches never derive wallet credentials.  The user
    channel remains deliberately idle and reconcile is disabled, because both
    are private-account surfaces rather than public market data.
    """
    import orderbook
    rest_book = orderbook.get_orderbook          # captured pre-patch

    # Parse the adapter configuration before constructing the feeds so the
    # advertised freshness thresholds reach the objects that enforce them.
    cfg = adapters.AdapterConfig()
    if paper or read_only:
        cfg.user_ws = "off"
        cfg.reconcile = "off"
    if not (paper or read_only) and cfg.user_ws == "off" and cfg.reconcile == "off":
        raise RuntimeError(
            "live mode requires USER_WS=on or RECONCILE=auto for fill accounting")
    credentials = {} if (paper or read_only) else derive_creds()
    if not (paper or read_only) and cfg.user_ws == "on" and not credentials:
        raise RuntimeError(
            "live USER_WS is enabled but L2 credentials could not be derived "
            "(CLOB create/derive timed out or failed). Check connectivity to "
            "CLOB_HOST and retry.")
    hub = FeedHub(creds=credentials, on_event=on_event,
                  btc_stale_after=cfg.btc_stale_after,
                  book_stale_after=cfg.book_stale_after,
                  rest_book_fetch=lambda token: rest_book(token))
    cfg, agreement = adapters.install(hub, cfg, on_event=on_event)
    return hub, cfg, agreement


def _start_feed_tasks(hub, cfg):
    # USER_WS=off is a hard boundary: do not even create the private-channel
    # supervisor.  In paper/health mode cfg is forced off before hub creation.
    tasks = hub.start(user=cfg.user_ws == "on", binance=cfg.btc_feed == "ws")
    if cfg.btc_feed == "legacy":
        # Exactly one BTC socket is allowed.  Mirror the legacy producer's
        # atomic public snapshot into the hub for dashboard/health consistency
        # instead of also starting the hub's Binance connection.
        import price_ws
        tasks.append(asyncio.create_task(price_ws.stream_price(),
                                         name="feed:binance-legacy"))
        tasks.append(asyncio.create_task(_legacy_price_mirror(hub),
                                         name="feed:binance-legacy-mirror"))
    return tasks


async def _legacy_price_mirror(hub) -> None:
    import price_ws
    previous_mono = object()
    hub.binance.health.status = "STALE"
    while True:
        price, observed, exchange_ts = price_ws.latest_snapshot()
        if observed != previous_mono:
            previous_mono = observed
            hub.binance.price = price
            hub.binance.price_mono = observed
            hub.binance.trade_ms = exchange_ts
            if price is not None and observed is not None:
                hub.binance.health.mark_message()
        hub.binance.health.status = (
            "LIVE" if price_ws.fresh_snapshot(hub.binance.stale_after)[0] is not None
            else "STALE")
        await asyncio.sleep(0.05)


def fetch_trades(markets=()):
    """REST backup source for fills.

    Every request carries an explicit recent bot condition ID. An unfiltered
    account query would contaminate this strategy's PnL with manual trades.
    """
    selected = tuple(dict.fromkeys(str(m) for m in markets if m))
    if not selected:
        on_event("reconcile", "no explicit market filters; REST reconcile skipped", "warn")
        return []
    try:
        import polymarket_trade
        from py_clob_client_v2 import TradeParams
        client = polymarket_trade._get_client()
    except Exception as exc:
        detail = f"cannot open CLOB client: {type(exc).__name__}"
        on_event("reconcile", detail, "warn")
        # A transport/client failure is not an authoritative empty result.
        # Raising lets RestReconciler keep a subscription generation due and
        # retry on the short cadence instead of waiting for the healthy audit.
        raise RuntimeError(detail) from exc
    fn = getattr(client, "get_trades", None)
    if callable(fn):
        merged = {}
        try:
            after = int(time.time()) - int(Ledger.RECOVERY_LOOKBACK_SECONDS)
            rejected = 0
            for condition in selected:
                rows = fn(TradeParams(market=condition, after=after))
                if rows is not None and isinstance(rows, (str, bytes, dict)):
                    raise TypeError("get_trades() returned a non-list payload")
                for row in rows or ():
                    if isinstance(row, dict):
                        # Never trust a server/client filter as an accounting
                        # boundary.  A cross-market row would contaminate this
                        # strategy's fill store with manual wallet activity.
                        if str(row.get("market") or "") != condition:
                            rejected += 1
                            continue
                        key = str(row.get("id") or row.get("trade_id") or "")
                        if key:
                            merged[key] = row
                    else:
                        rejected += 1
            if rejected:
                on_event("reconcile", f"rejected {rejected} malformed/cross-market row(s)",
                         "warn")
        except Exception as exc:
            detail = f"get_trades() failed: {type(exc).__name__}: {exc}"
            on_event("reconcile", detail, "warn")
            raise RuntimeError(detail) from exc
        return list(merged.values())
    detail = ("no trades method on the CLOB client - REST backup is INERT, "
              "fills rely on the user socket alone")
    on_event("reconcile", detail, "bad")
    raise RuntimeError(detail)


def _reconcile_markets(ledger, hub) -> tuple[str, ...]:
    """Stable union of durable authorizations and in-process discovery."""
    return tuple(dict.fromkeys((
        *ledger.recovery_conditions(
            lookback_s=Ledger.RECOVERY_LOOKBACK_SECONDS),
        *hub.recent_markets(),
    )))


async def _recover_startup_fills(reconciler, ledger, store) -> tuple[int, int]:
    """Finish one REST→FillStore→Ledger transaction before trading starts."""
    if reconciler is None:
        return 0, 0
    recovered = await reconciler.run_once()
    ingested = _persist_fill_drain(ledger, store)
    if reconciler.last_error:
        on_event("reconcile", "startup audit incomplete; background retries remain "
                 f"armed: {reconciler.last_error}", "warn")
    elif recovered or ingested:
        on_event("reconcile", f"startup audit recovered {recovered} trade record(s), "
                 f"booked {ingested} authorized fill(s)", "warn")
    return recovered, ingested


def _persist_fill_drain(ledger, store) -> int:
    """Synchronously ingest a feed snapshot and durably save all counters."""
    ingested = ledger.ingest_fill_store(store)
    # record_fill_durable saves each accepted fill, but this final snapshot is
    # still required for rejection/dedup counters and a zero-fill drain.
    if not ledger.save():
        raise RuntimeError("ledger save failed after fill-store drain")
    return ingested


def _require_live_fill_coverage(*, paper: bool, user_ws: str) -> None:
    """Require the private stream as LIVE's primary fill source."""
    if paper or user_ws == "on":
        return
    # A fresh process has no current condition filter, so an empty startup
    # REST result does not prove the authenticated trade endpoint works. REST
    # remains a backup/audit path; it cannot replace the private user stream.
    raise RuntimeError(
        "live trading requires USER_WS=on; REST reconcile is backup-only")


def _install_io_executor():
    """Size the thread pool for I/O, not for CPU count.

    ``asyncio.to_thread`` defaults to ``min(32, cpu_count + 4)`` workers, which
    is sized for CPU-bound work. Every to_thread call here is a blocking
    network read - book fetches, discovery, the liquidity probe, the modeled
    fill, the settlement lookup - and there are two dozen such call sites.

    On a 1-vCPU VPS that default is FIVE workers. The settlement poll can hold
    one for seconds at a time while it walks the open conditions, and a single
    trade cycle wants several at once for its legs. The pool saturates, further
    to_thread calls queue behind it, and the bot stops making progress for a
    while - which looks exactly like a freeze. A 16-core dev box gets 20
    workers and never shows it.

    Threads blocked on a socket cost almost nothing, so size by concurrent
    reads instead. Returns the executor so the caller can shut it down.
    """
    try:
        from concurrent.futures import ThreadPoolExecutor
        loop = asyncio.get_running_loop()
        workers = max(32, (os.cpu_count() or 1) + 4)
        executor = ThreadPoolExecutor(max_workers=workers,
                                      thread_name_prefix="btcbot-io")
        loop.set_default_executor(executor)
        on_event("startup", f"io thread pool sized to {workers} workers "
                            f"({os.cpu_count() or 1} cpu)", "info")
        return executor
    except Exception as exc:
        # Never block startup over a tuning knob; the default pool still works.
        on_event("startup", f"could not resize io pool: {type(exc).__name__}", "warn")
        return None


async def run(dash: bool = False, *, paper: bool = True,
              paper_balance: float | None = None) -> None:
    root = Path(__file__).resolve().parent
    configured = os.environ.get("BOT_LOCK_PATH")
    lock_path = (Path(configured) if configured else
                 root / (".btc_bot_paper.lock" if paper else ".btc_bot_live.lock"))
    if not lock_path.is_absolute():
        lock_path = root / lock_path
    process_lock = _ProcessLock(lock_path)
    process_lock.acquire()
    executor = _install_io_executor()
    try:
        await _run_inner(dash=dash, paper=paper, paper_balance=paper_balance)
    finally:
        process_lock.release()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


async def _run_inner(dash: bool = False, *, paper: bool = True,
                     paper_balance: float | None = None) -> None:
    hub, cfg, agreement = build_hub(paper=paper)
    try:
        await _run_configured(hub, cfg, agreement, dash=dash, paper=paper,
                              paper_balance=paper_balance)
    finally:
        # The adapters monkey-patch process-global read points.  A failed
        # startup or an in-process restart must restore them deterministically.
        adapters.uninstall()


async def _run_configured(hub, cfg, agreement, *, dash: bool = False,
                          paper: bool = True,
                          paper_balance: float | None = None) -> None:
    stop = threading.Event()

    # The Chainlink-computed 60-second TWAP used by the five-minute market.
    # A dead/stale RTDS feed withholds the Chainlink decision leg; it never
    # substitutes the ordinary Chainlink spot aggregator.
    import config
    strike = ChainlinkStrike(on_event=on_event, stale_after=config.TWAP_STALE_AFTER)
    import main_bot as _mb
    _mb._strike = strike

    import main_bot
    main_bot.stop_event.clear()
    main_bot.session_trades.clear()
    main_bot.execution_mode = "LIVE"
    main_bot._paper_broker = None
    main_bot._accounting_enabled = True
    main_bot._round_exposure_provider = None
    main_bot._round_held_tokens_provider = None
    main_bot._execution_ready_provider = None

    broker = None
    if paper:
        import config
        from paper_trade import PaperBroker, fetch_executable_book, install_paper_execution

        paper_paths = _paper_state_paths()
        ledger = Ledger(
            path=str(paper_paths["ledger"]),
            category=os.environ.get("MARKET_CATEGORY", "crypto"),
            # Exits stay refused unless a stop or take-profit is actually
            # running, so the buy-and-hold contract is unchanged for every
            # existing config.
            allow_sells=(config.STOP_LOSS_ENABLED
                         or config.TAKE_PROFIT_ENABLED))
        starting = (paper_balance if paper_balance is not None else
                    float(os.environ.get("PAPER_START_BALANCE", "1000")))
        broker = PaperBroker(
            ledger,
            market_context=lambda: {
                "condition_id": hub.condition_id,
                "up_token_id": hub.up_token,
                "down_token_id": hub.down_token,
            },
            host=config.CLOB_HOST,
            max_buy_price=config.MAX_BUY_PRICE,
            min_buy_price=config.MIN_BUY_PRICE,
            start_balance=starting,
            account_path=paper_paths["account"],
            audit_path=paper_paths["audit"],
            category=os.environ.get("MARKET_CATEGORY", "crypto"),
            latency_ms=config.PAPER_LATENCY_MS,
            max_book_age_s=config.ORDERBOOK_MAX_AGE_SECONDS,
            max_quiet_s=config.ORDERBOOK_MAX_QUIET_SECONDS,
            future_tol_s=config.ORDERBOOK_FUTURE_TOLERANCE_SECONDS,
            max_spread=config.MAX_ALLOWED_SPREAD,
            min_seconds_to_expiry=config.MIN_SECONDS_TO_EXPIRY,
            # Must span every enabled phase, not just phase 2's window.
            trade_window_seconds=config.EXECUTION_WINDOW_SECONDS,
            book_fetch=lambda token: fetch_executable_book(
                token, host=config.CLOB_HOST, ws_view=hub.book.view(str(token))),
            on_event=on_event,
        )
        install_paper_execution(
            main_bot, broker, log_path=paper_paths["trade_log"])
        main_bot._round_exposure_provider = (
            lambda _window, condition: ledger.confirmed_cost_for_condition(condition))
        main_bot._round_held_tokens_provider = (
            lambda _window, condition: ledger.held_tokens_for_condition(condition))
        # PAPER only. The live ledger tracks authorized orders per window, not
        # confirmed per-leg cost, so it cannot answer "what did this leg cost".
        # Left unset for LIVE, which keeps the pair-lock closed there.
        main_bot._round_leg_basis_provider = (
            lambda condition, token: ledger.open_leg_basis(condition, token))
        main_bot._round_inventory_provider = (
            lambda condition: ledger.open_inventory_for_condition(condition))
    else:
        import polymarket_trade
        # Live exits arrive on the private fill stream like any other fill.
        # Without this the ledger refuses them as skipped_side, and the book
        # keeps showing a position that has already been sold - a silent
        # divergence between accounting and the chain.
        ledger = Ledger(path=str(_state_path("LEDGER_PATH", "ledger.json")),
                        category=os.environ.get("MARKET_CATEGORY", "crypto"),
                        fee_resolver=polymarket_trade.market_fee_parameters,
                        allow_sells=(config.STOP_LOSS_ENABLED
                                     or config.TAKE_PROFIT_ENABLED))

        def journal_live_order(receipt: dict) -> bool:
            order_id = receipt.get("order_id")
            ledger.authorize_order(
                order_id, receipt,
                venue_min_shares=config.VENUE_MIN_SHARES,
                price_cap=config.MAX_BUY_PRICE,
            )
            if not ledger.save():
                raise RuntimeError("authorized-order ledger save failed")
            return True

        polymarket_trade.set_order_observer(journal_live_order)
        main_bot._round_exposure_provider = (
            lambda window, _condition: ledger.authorized_cost_for_window(window))
        main_bot._round_held_tokens_provider = (
            lambda window, _condition: ledger.authorized_tokens_for_window(window))
        # PHASE2_MULTI_SIGNAL runs in LIVE too, and there a complement leg is
        # only permitted where the pair-lock proves the finished pair cannot
        # lose - so the lock needs a real cost basis here, not just in PAPER.
        # open_leg_basis reads confirmed positions, which LIVE populates from
        # the private fill stream via ingest_fill_store -> record_fill_durable.
        # Unfilled authorizations are deliberately not a basis: reserving an
        # order is not owning a leg, and pricing a pair off one would complete
        # a pair whose first half never existed.
        main_bot._round_leg_basis_provider = (
            lambda condition, token: ledger.open_leg_basis(condition, token))
        main_bot._execution_ready_provider = hub.user.ready_for_market
        main_bot._round_inventory_provider = (
            lambda condition: ledger.open_inventory_for_condition(condition))

    # Prove the accounting directory is writable before any feed or order task
    # starts. A bot that cannot durably journal fills is not safe to run.
    if not ledger.save():
        raise RuntimeError("accounting ledger is not writable; refusing to start")

    # Settlement is independent of the strategy/order loop.  Poll official
    # venue finality once per second so a resolved payout reaches derived paper
    # cash on the dashboard's next frame.
    settler = SettlementWorker(ledger, interval=1.0, on_event=on_event)

    reconciler = None
    if cfg.reconcile == "auto":
        reconciler = RestReconciler(hub.fill_store,
                                    lambda: fetch_trades(
                                        _reconcile_markets(ledger, hub)),
                                    user_feed=hub.user,
                                    known_trade=ledger.rest_trade_is_booked,
                                    on_event=on_event)

    mode = "PAPER (NO WALLET / NO SIGNATURE / NO LIVE ORDERS)" if paper else "LIVE"
    banner = f"[FEEDS] MODE={mode} | {cfg.describe()}"
    print(banner)
    if not cfg.decisions_unchanged:
        print("[FEEDS] WARNING: this configuration changes what the strategy reads. "
              "Set BOOK_SOURCE=ws_shadow to restore the audited REST decision source. "
              "PRICE_STALE_POLICY affects only the legacy display value.")
    on_event("config", cfg.describe(), "info" if cfg.decisions_unchanged else "warn")

    # Paint the terminal as soon as the alt-screen can come up. Clock sync and
    # fill recovery still finish before the bot places anything; they used to
    # also block the first dashboard frame for the whole HTTP round-trip.
    dash_task = None
    if dash:
        dash_task = asyncio.create_task(
            _dashboard(hub, cfg, agreement, reconciler, stop,
                       ledger=ledger, settler=settler, broker=broker))
        dash_task.add_done_callback(lambda task: _task_failure_event(task, "dashboard"))

    # Measure CLOB time before any feed timestamps are judged, so a local
    # clock that is a few seconds behind does not drop live Binance prints.
    import timer
    clock_ok, clock_detail, clock_drift = await asyncio.to_thread(
        timer.check_clock, config.CLOB_HOST, config.CLOCK_MAX_DRIFT_SECONDS)
    if clock_drift is not None:
        on_event("clock", f"{clock_detail}; offset {timer.clock_offset():+.3f}s",
                 "info" if clock_ok else "warn")
    else:
        on_event("clock", clock_detail, "warn")

    # A restart can occur after an order was durably authorized but before its
    # CONFIRMED user event reached the ledger.  Recover and synchronously book
    # that fill before the strategy gets any opportunity to place another
    # order.  A transient REST failure is retained on the reconciler and retried
    # after subscription and on the healthy cadence.
    await _recover_startup_fills(reconciler, ledger, hub.fill_store)
    _require_live_fill_coverage(paper=paper, user_ws=cfg.user_ws)

    tasks = _start_feed_tasks(hub, cfg)
    if reconciler:
        tasks.append(reconciler.start())
    tasks.append(asyncio.create_task(
        adapters.price_staleness_watchdog(hub, cfg, stop, on_event)))
    tasks.append(asyncio.create_task(_rotation_loop(hub, stop), name="rotation"))
    # A single exit broker serves BOTH stop-loss and take-profit. LIVE has no
    # PaperBroker, so build one adapter here and pass it to whichever exit
    # tasks are enabled; live must never silently skip an exit loop.
    exit_broker = None
    if config.STOP_LOSS_ENABLED or config.TAKE_PROFIT_ENABLED:
        exit_broker = broker if broker is not None else _LiveExitBroker()
    if config.STOP_LOSS_ENABLED:
        tasks.append(asyncio.create_task(
            _stop_loss_loop(hub, exit_broker, ledger, stop, on_event),
            name="stoploss"))
    if config.TAKE_PROFIT_ENABLED:
        tasks.append(asyncio.create_task(
            _take_profit_loop(hub, exit_broker, ledger, stop, on_event),
            name="takeprofit"))
    if config.CHEAP_HEDGE_ENABLED:
        tasks.append(asyncio.create_task(
            _cheap_hedge_loop(hub, ledger, stop, on_event),
            name="cheaphedge"))
    tasks.append(asyncio.create_task(
        adapters.agreement_sampler(hub, cfg, agreement, stop, on_event), name="audit"))
    tasks.append(strike.start())
    tasks.append(settler.start())
    if not paper:
        tasks.append(asyncio.create_task(_ledger_loop(hub, ledger, stop), name="ledger"))
    if not dash:
        threading.Thread(target=main_bot._kill_switch, daemon=True).start()
    bot_task = asyncio.create_task(main_bot.run_bot(), name="bot")
    tasks.append(bot_task)

    if dash_task is not None:
        tasks.append(dash_task)
    else:
        tasks.append(asyncio.create_task(
            _health_log(hub, cfg, agreement, reconciler, stop, ledger,
                        broker=broker)))

    try:
        await bot_task
    finally:
        stop.set()
        main_bot.stop_event.set()
        if not paper and config.CANCEL_OPEN_BEFORE_TRADE:
            try:
                cancelled = await asyncio.wait_for(
                    asyncio.to_thread(main_bot.cancel_all_open_orders), timeout=10.0)
                if not cancelled:
                    on_event("shutdown", "cancel-all failed during shutdown", "bad")
            except Exception as exc:
                on_event("shutdown", f"cancel-all error: {type(exc).__name__}", "bad")
        if reconciler:
            await reconciler.stop()
        await strike.stop()
        await settler.stop()
        await hub.stop()
        if not paper:
            import polymarket_trade
            polymarket_trade.set_order_observer(None)
        main_bot._round_exposure_provider = None
        main_bot._round_held_tokens_provider = None
        main_bot._round_leg_basis_provider = None
        main_bot._execution_ready_provider = None
        main_bot._accounting_enabled = False
        main_bot._strike = None
        for t in tasks:
            t.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results):
            if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError):
                on_event("shutdown", f"task {task.get_name()} failed: "
                         f"{type(result).__name__}: {result}", "bad")
        print("\n[FEEDS] final health:")
        try:
            drained = _persist_fill_drain(ledger, hub.fill_store)
            if drained:
                on_event("shutdown", f"durably booked {drained} late fill(s)", "warn")
        except Exception as exc:
            detail = f"final fill drain failed: {type(exc).__name__}: {exc}"
            on_event("shutdown", detail, "bad")
            print(f"[FEEDS] CRITICAL: {detail}")
        try:
            final_report = _report(hub, cfg, agreement, reconciler, ledger, settler,
                                   broker=broker)
        except Exception as exc:
            on_event("shutdown", f"final report failed: {type(exc).__name__}: {exc}",
                     "bad")
            final_report = {"error": safe_log_text(
                f"final report failed: {type(exc).__name__}: {exc}")}
        print(json.dumps(final_report, indent=2, default=str))


def _report(hub, cfg, agreement, reconciler, ledger=None, settler=None,
            broker=None) -> dict:
    out = {"config": cfg.describe(), "feeds": hub.health(),
           "book_agreement": agreement.summary(),
           "fills": hub.fill_store.summary()}
    with _EV_LOCK:
        out["recent_events"] = [
            {"wall": wall, "source": source, "text": text, "level": level}
            for wall, source, text, level in EVENTS[-50:]
        ]
    if reconciler:
        out["reconcile"] = reconciler.summary()
    if ledger is not None:
        def mark(token):
            v = hub.book.view(token)
            return v.best_bid if v.status == "LIVE" else None
        if broker is not None:
            out["paper_account"] = broker.summary(mark=mark)
            out["pnl"] = ledger.summary(mark=mark)
            out["balance_check"] = {
                "status": "PAPER",
                "detail": "cash is derived from simulated fills and resolved payouts",
            }
        else:
            out["pnl"] = ledger.summary(mark=mark)
            out["balance_check"] = ledger.reconcile_balance()
    if settler is not None:
        out["settlement"] = settler.summary()
    return out


async def _ledger_loop(hub, ledger, stop) -> None:
    """Pull venue-reported fills into the ledger and mark the wallet.

    Runs on its own task: no accounting happens in a receive callback or in
    the order path. The balance read is what makes `reconcile_balance()`
    possible, and it is the only independent check on our own bookkeeping.
    """
    last_save = 0.0
    last_balance = 0.0
    while not stop.is_set():
        await asyncio.sleep(2.0)
        try:
            if ledger.ingest_fill_store(hub.fill_store):
                if not ledger.save():
                    raise RuntimeError("ledger save failed after confirmed fill")
                last_save = time.monotonic()
            if time.monotonic() - last_balance > 120:
                last_balance = time.monotonic()
                try:
                    import polymarket_trade
                    bal = await asyncio.to_thread(polymarket_trade.get_balance_allowance)
                    if bal and bal.get("balance") is not None:
                        ledger.mark_balance(bal["balance"])
                except Exception as exc:
                    on_event("ledger", f"balance read failed: {type(exc).__name__}", "warn")
            if time.monotonic() - last_save > 60:
                if not ledger.save():
                    raise RuntimeError("periodic ledger save failed")
                last_save = time.monotonic()
        except Exception as exc:
            on_event("ledger", f"{type(exc).__name__}: {exc}", "warn")


class _LiveExitBroker:
    """Adapts the live CLOB to the exit interface the stop loop expects.

    The shapes differ in one way that matters: a PAPER exit books its own fill
    synchronously, while a LIVE exit only *submits* and the fill arrives later
    on the private stream. So this returns shares SUBMITTED, and the caller
    must not treat a return value as a settled position change.
    """

    mode = "LIVE"

    def __init__(self) -> None:
        import polymarket_trade
        self._pt = polymarket_trade
        self.last_error = None

    def sell_shares(self, token_id, shares, *, min_price=0.0,
                    condition_id=None, window_end=None,
                    exit_cutoff_seconds=0.0) -> float:
        import timer as _timer
        if window_end is not None:
            cutoff = float(window_end) - float(exit_cutoff_seconds)
            if _timer.unix() >= cutoff:
                self.last_error = "exit cutoff reached"
                return 0.0
        submitted = self._pt.sell_shares(
            str(token_id), float(shares), min_price=float(min_price),
            condition_id=condition_id, window_end=window_end)
        self.last_error = self._pt.last_order_error
        return float(submitted or 0.0)


# Exit-loop back-off: after this many consecutive sell failures on the same
# (window, token) the loop stops retrying for `_EXIT_BACKOFF_SECONDS`. The
# venue almost never fixes a repeated rejection on the next poll - what usually
# fixes it is round rotation, and the round already rotates on its own schedule.
# Hammering CLOB at 1 Hz for a full round has been shown to burn API budget
# and can get the wallet's keys flagged; a per-token cool-down keeps a broken
# sell from becoming a broken account.
_EXIT_MAX_FAILS_BEFORE_BACKOFF = 3
_EXIT_BACKOFF_SECONDS = 60.0


async def _stop_loss_loop(hub, broker, ledger, stop, on_event) -> None:
    """Watch held legs and exit any whose BID reaches the stop.

    Deliberately its own task rather than a step inside the trading loop. The
    entry path costs about four seconds per attempt - discovery, a clock check
    and several book reads - and an exit needs none of that: the token is
    already known. Sharing the entry pipeline would mean the stop could only
    fire as often as the bot decides to buy, which is exactly backwards.
    """
    import config
    import timer
    fired: set[tuple[int, str]] = set()
    # A LIVE exit only submits; its fill lands later on the private stream.
    # Without a grace window the next poll still sees the full position and
    # fires again, selling the same shares twice. PAPER books synchronously and
    # is already flat by then, so the guard simply never triggers there.
    inflight: dict[tuple[int, str], float] = {}
    fail_streak: dict[tuple[int, str], int] = {}
    backoff_until: dict[tuple[int, str], float] = {}
    LIVE_FILL_GRACE_S = 10.0
    while not stop.is_set():
        try:
            sampled = timer.unix()
            window = timer.window_start(sampled)
            remain = (window + 300) - sampled
            armed = (config.STOP_LOSS_EXIT_CUTOFF_SECONDS < remain
                     <= config.STOP_LOSS_ARM_SECONDS)
            condition = hub.condition_id
            if armed and condition:
                held = []
                with ledger._lock:
                    for token, pos in ledger.positions.items():
                        if (not pos.settled and pos.shares > 1e-9
                                and pos.condition_id == condition):
                            held.append((token, pos.shares))
                now_mono = time.monotonic()
                for token, shares in held:
                    key = (window, token)
                    if key in fired:
                        continue
                    if now_mono < inflight.get(key, 0.0):
                        continue
                    if now_mono < backoff_until.get(key, 0.0):
                        continue
                    view = hub.book.view(str(token))
                    bid = getattr(view, "best_bid", None) if view else None
                    if bid is None or float(bid) > config.STOP_LOSS_PRICE:
                        continue
                    on_event("stoploss",
                             f"bid {float(bid):.3f} <= {config.STOP_LOSS_PRICE:.3f} "
                             f"with {remain:.0f}s left; exiting {shares:.4f} sh",
                             "warn")
                    sold = await asyncio.to_thread(
                        broker.sell_shares, str(token), float(shares),
                        min_price=config.STOP_LOSS_FLOOR_PRICE,
                        condition_id=condition,
                        window_end=window + 300,
                        exit_cutoff_seconds=config.STOP_LOSS_EXIT_CUTOFF_SECONDS)
                    if sold > 0:
                        inflight[key] = time.monotonic() + LIVE_FILL_GRACE_S
                        fail_streak.pop(key, None)
                        backoff_until.pop(key, None)
                        on_event("stoploss", f"exited {sold:.4f} sh", "good")
                        # Only stop watching once the leg is actually flat. A
                        # partial fill leaves real exposure, and marking it
                        # done here would abandon the remainder.
                        with ledger._lock:
                            left = ledger.positions.get(token)
                            if left is None or left.shares <= 1e-9:
                                fired.add(key)
                    else:
                        streak = fail_streak.get(key, 0) + 1
                        fail_streak[key] = streak
                        on_event("stoploss",
                                 f"exit did not fill ({streak}x): {broker.last_error}",
                                 "warn")
                        if streak >= _EXIT_MAX_FAILS_BEFORE_BACKOFF:
                            backoff_until[key] = time.monotonic() + _EXIT_BACKOFF_SECONDS
                            on_event("stoploss",
                                     f"backing off {_EXIT_BACKOFF_SECONDS:.0f}s "
                                     f"after {streak} consecutive failures", "bad")
            elif not armed:
                fired = {k for k in fired if k[0] == window}
                inflight = {k: v for k, v in inflight.items() if k[0] == window}
                fail_streak = {k: v for k, v in fail_streak.items() if k[0] == window}
                backoff_until = {k: v for k, v in backoff_until.items() if k[0] == window}
        except Exception as exc:
            on_event("stoploss", f"{type(exc).__name__}: {exc}", "warn")
        await asyncio.sleep(config.STOP_LOSS_POLL_SECONDS)


async def _take_profit_loop(hub, broker, ledger, stop, on_event) -> None:
    """Watch held legs and exit any whose BID reaches TAKE_PROFIT_PRICE.

    Mirror of _stop_loss_loop with three deliberate differences:

    1. No arm/cutoff window. A leg trading at 0.98 the moment it's held is
       already at take-profit — waiting for the last minute would just donate
       spread to whoever gets there first. If the operator wants a windowed
       take-profit, this loop is the wrong place; a windowed one would need
       its own config.
    2. Trigger direction is BID >= threshold, not BID <=. Same "whichever
       side gets there first" semantics as the stop, just on the winning
       tail.
    3. The exit's min_price defaults to TAKE_PROFIT_PRICE itself (no walking
       down), because a fill below 0.98 defeats the whole reason for the
       trigger. TAKE_PROFIT_FLOOR_PRICE overrides that only when the
       operator wants to accept slippage.
    """
    import config
    import timer
    fired: set[tuple[int, str]] = set()
    inflight: dict[tuple[int, str], float] = {}
    fail_streak: dict[tuple[int, str], int] = {}
    backoff_until: dict[tuple[int, str], float] = {}
    LIVE_FILL_GRACE_S = 10.0
    while not stop.is_set():
        try:
            sampled = timer.unix()
            window = timer.window_start(sampled)
            condition = hub.condition_id
            if condition:
                held = []
                with ledger._lock:
                    for token, pos in ledger.positions.items():
                        if (not pos.settled and pos.shares > 1e-9
                                and pos.condition_id == condition):
                            held.append((token, pos.shares))
                now_mono = time.monotonic()
                for token, shares in held:
                    key = (window, token)
                    if key in fired:
                        continue
                    if now_mono < inflight.get(key, 0.0):
                        continue
                    if now_mono < backoff_until.get(key, 0.0):
                        continue
                    view = hub.book.view(str(token))
                    bid = getattr(view, "best_bid", None) if view else None
                    if bid is None or float(bid) < config.TAKE_PROFIT_PRICE:
                        continue
                    on_event("takeprofit",
                             f"bid {float(bid):.3f} >= "
                             f"{config.TAKE_PROFIT_PRICE:.3f}; "
                             f"exiting {shares:.4f} sh",
                             "good")
                    sold = await asyncio.to_thread(
                        broker.sell_shares, str(token), float(shares),
                        min_price=config.TAKE_PROFIT_FLOOR_PRICE,
                        condition_id=condition,
                        window_end=window + 300,
                        # TP has no cutoff of its own; pass 0 so the exit only
                        # refuses once the round has actually ended.
                        exit_cutoff_seconds=0.0)
                    if sold > 0:
                        inflight[key] = time.monotonic() + LIVE_FILL_GRACE_S
                        fail_streak.pop(key, None)
                        backoff_until.pop(key, None)
                        on_event("takeprofit", f"exited {sold:.4f} sh @ "
                                 f">=${config.TAKE_PROFIT_PRICE:.2f}", "good")
                        with ledger._lock:
                            left = ledger.positions.get(token)
                            if left is None or left.shares <= 1e-9:
                                fired.add(key)
                    else:
                        streak = fail_streak.get(key, 0) + 1
                        fail_streak[key] = streak
                        on_event("takeprofit",
                                 f"exit did not fill ({streak}x): "
                                 f"{broker.last_error}", "warn")
                        if streak >= _EXIT_MAX_FAILS_BEFORE_BACKOFF:
                            backoff_until[key] = time.monotonic() + _EXIT_BACKOFF_SECONDS
                            on_event("takeprofit",
                                     f"backing off {_EXIT_BACKOFF_SECONDS:.0f}s "
                                     f"after {streak} consecutive failures", "bad")
            # A round rollover invalidates the per-round bookkeeping. Prune
            # entries for prior windows so a very long-running process cannot
            # accumulate them, and so a token that reappears in a later round
            # is not silently blocked by a stale fired-set entry.
            fired = {k for k in fired if k[0] == window}
            inflight = {k: v for k, v in inflight.items() if k[0] == window}
            fail_streak = {k: v for k, v in fail_streak.items() if k[0] == window}
            backoff_until = {k: v for k, v in backoff_until.items() if k[0] == window}
        except Exception as exc:
            on_event("takeprofit", f"{type(exc).__name__}: {exc}", "warn")
        await asyncio.sleep(config.TAKE_PROFIT_POLL_SECONDS)


async def _cheap_hedge_loop(hub, ledger, stop, on_event) -> None:
    """Once per round, insure a large one-sided position with a cheap
    underdog buy. Fires at most once per (window, condition); guarded by
    inventory size, underdog price band, remaining time and signal
    agreement per cheap_hedge.evaluate_cheap_hedge().

    On its own task rather than inside the strategy loop for the same reason
    the stop-loss and take-profit loops are: the entry pipeline is expensive
    (discovery, clock check, book reads) and this trigger needs none of
    that. Sharing that pipeline would mean the hedge could only fire as
    often as the bot decides to enter, which is exactly the wrong cadence
    for insurance.
    """
    import cheap_hedge
    import config
    import main_bot
    import timer
    fired: set[tuple[int, str]] = set()   # (window, condition_id)
    fail_streak: dict[tuple[int, str], int] = {}
    backoff_until: dict[tuple[int, str], float] = {}
    last_heartbeat_mono = 0.0
    HEARTBEAT_EVERY_S = 60.0
    while not stop.is_set():
        try:
            sampled = timer.unix()
            window = timer.window_start(sampled)
            remaining = (window + 300) - sampled
            condition = hub.condition_id
            if not condition or hub.up_token is None or hub.down_token is None:
                # No round yet - do not log heartbeat here or restart will
                # print a status before the first strategy loop even runs.
                await asyncio.sleep(config.CHEAP_HEDGE_POLL_SECONDS)
                continue
            key = (window, str(condition))
            skip_reason = None
            if key in fired:
                skip_reason = "already hedged this round"
            elif time.monotonic() < backoff_until.get(key, 0.0):
                skip_reason = "in back-off after repeated failures"

            # Inventory per token for THIS round's condition.
            up_shares = up_cost = 0.0
            down_shares = down_cost = 0.0
            up_token = str(hub.up_token)
            down_token = str(hub.down_token)
            with ledger._lock:
                for tid, pos in ledger.positions.items():
                    if pos.settled or pos.condition_id != condition:
                        continue
                    tid_s = str(tid)
                    if tid_s == up_token:
                        up_shares += float(pos.shares or 0.0)
                        up_cost += float(pos.cost or 0.0) + float(pos.fees or 0.0)
                    elif tid_s == down_token:
                        down_shares += float(pos.shares or 0.0)
                        down_cost += float(pos.cost or 0.0) + float(pos.fees or 0.0)

            up_view = hub.book.view(up_token)
            down_view = hub.book.view(down_token)
            up_ask = getattr(up_view, "best_ask", None) if up_view else None
            down_ask = getattr(down_view, "best_ask", None) if down_view else None

            # The direction the strategy is currently trading. Sourced from
            # state.decision, which the probe now updates from BOTH
            # strategy.final_decision AND strategy.minority_decision so
            # SIGNAL_MINORITY_RULE reflects the true traded side (fixing
            # the earlier "any signal disagrees" bug that refused every
            # legit hedge under minority rule).
            current_traded_side = None
            try:
                from dashboard import probe as _probe
                probe_state = _probe._sink.state if _probe._sink else None
                if probe_state is not None:
                    stamped = getattr(probe_state, "decision", None)
                    v = stamped.value if stamped is not None else None
                    if v in ("UP", "DOWN"):
                        current_traded_side = v
            except Exception:
                current_traded_side = None

            decision = cheap_hedge.evaluate_cheap_hedge(
                enabled=config.CHEAP_HEDGE_ENABLED,
                remaining=remaining,
                start=config.CHEAP_HEDGE_START_SECONDS,
                cutoff=config.CHEAP_HEDGE_CUTOFF_SECONDS,
                up_shares=up_shares, up_cost=up_cost,
                down_shares=down_shares, down_cost=down_cost,
                up_ask=up_ask, down_ask=down_ask,
                ask_min=config.CHEAP_HEDGE_ASK_MIN,
                ask_max=config.CHEAP_HEDGE_ASK_MAX,
                min_held_cost=config.CHEAP_HEDGE_MIN_HELD_COST,
                loss_cap=config.CHEAP_HEDGE_LOSS_CAP,
                max_hedge_cost=config.CHEAP_HEDGE_MAX_HEDGE_COST,
                current_traded_side=current_traded_side,
                require_strong_signal=config.CHEAP_HEDGE_REQUIRE_STRONG_SIGNAL,
                already_hedged=bool(skip_reason),
            )

            # Heartbeat: once per HEARTBEAT_EVERY_S emit a status line even
            # when not firing, so the operator can SEE the loop is alive and
            # what is blocking it. Without this the loop is silent unless
            # it fires or errors, which turned the last debugging session
            # into an unnecessary detective story.
            now_mono = time.monotonic()
            if now_mono - last_heartbeat_mono >= HEARTBEAT_EVERY_S:
                last_heartbeat_mono = now_mono
                held_txt = (f"{decision['held_side']} "
                            f"${decision['held_cost']:.2f}"
                            if decision["held_side"] else "no clear held side")
                underdog = "UP" if decision["held_side"] == "DOWN" else (
                    "DOWN" if decision["held_side"] == "UP" else None)
                udog_ask = (up_ask if underdog == "UP"
                            else down_ask if underdog == "DOWN" else None)
                udog_txt = (f"{underdog} ask "
                            f"{float(udog_ask):.3f}"
                            if udog_ask is not None and underdog else "n/a")
                status = (skip_reason if skip_reason
                          else ("fires" if decision["action"] == "buy"
                                else decision["reason"]))
                on_event("cheaphedge",
                         f"watch T-{remaining:.0f} | held {held_txt} | "
                         f"udog {udog_txt} | traded {current_traded_side or '-'}"
                         f" | {status}",
                         "info")

            if skip_reason or decision["action"] != "buy":
                await asyncio.sleep(config.CHEAP_HEDGE_POLL_SECONDS)
                continue

            side = decision["side"]
            amount = float(decision["amount"] or 0.0)
            max_price = float(decision["max_price"] or config.CHEAP_HEDGE_ASK_MAX)
            on_event("cheaphedge",
                     f"{side} ${amount:.2f} @<={max_price:.2f} "
                     f"(held {decision['held_side']} ${decision['held_cost']:.2f}, "
                     f"underdog ask {decision['ask']:.3f}, "
                     f"target loss cap ${config.CHEAP_HEDGE_LOSS_CAP:.2f})",
                     "warn")
            # place_trade is the same entry the strategy loop uses; patched
            # for paper by install_paper_execution, real for live. It handles
            # its own preflight, guard and back-off.
            ok = await asyncio.to_thread(
                main_bot.place_trade, side, amount, up_token, down_token,
                condition, window + 300, max_price)
            if ok:
                fired.add(key)
                fail_streak.pop(key, None)
                backoff_until.pop(key, None)
                on_event("cheaphedge",
                         f"hedge {side} filled: ${amount:.2f} "
                         f"({decision.get('shares', 0.0):.2f} shares @ "
                         f"~{decision['ask']:.3f})", "good")
            else:
                streak = fail_streak.get(key, 0) + 1
                fail_streak[key] = streak
                on_event("cheaphedge",
                         f"hedge not placed ({streak}x): "
                         f"{main_bot.polymarket_trade.last_order_error or 'unknown'}",
                         "warn")
                # Back off the same way the sell loops do. A repeatedly
                # failing hedge is either an ineligible book or an auth
                # issue; either way, hammering will not fix it.
                if streak >= _EXIT_MAX_FAILS_BEFORE_BACKOFF:
                    backoff_until[key] = time.monotonic() + _EXIT_BACKOFF_SECONDS
                    on_event("cheaphedge",
                             f"backing off {_EXIT_BACKOFF_SECONDS:.0f}s "
                             f"after {streak} consecutive failures", "bad")

            # Prune stale per-round bookkeeping at rollover, exactly like
            # the take-profit loop does.
            fired = {k for k in fired if k[0] == window}
            fail_streak = {k: v for k, v in fail_streak.items() if k[0] == window}
            backoff_until = {k: v for k, v in backoff_until.items() if k[0] == window}
        except Exception as exc:
            on_event("cheaphedge", f"{type(exc).__name__}: {exc}", "warn")
        await asyncio.sleep(config.CHEAP_HEDGE_POLL_SECONDS)


async def _rotation_loop(hub, stop) -> None:
    """Discover this round's tokens EARLY.

    The bot only resolves the market inside the last TRADE_LAST_SECONDS, so if the
    socket waited for it the book would have under a minute to sync and would
    fall back to REST on the first call of every single round - which defeats
    the point of running a socket at all. This task subscribes at round start
    instead, so the book is warm long before the strategy reads it.

    It calls the same discovery function the bot calls and does not modify or
    cache its result for the bot; the bot's own call is untouched.
    """
    import market_discovery
    import config
    import timer
    # Prewarming is an optimisation; rotating on time is not. A discovery call
    # can block for ~21s (10s timeout, two attempts, plus the retry pause), so
    # starting one inside this many seconds of the boundary risks landing on
    # top of the rotation it exists to precede.
    PREPARE_FLOOR_SECONDS = 25.0
    last_key = None
    cleared_key = None
    prepared_key = None
    while not stop.is_set():
        try:
            sampled = timer.unix()
            remain = timer.seconds_left(sampled)
            window = timer.window_start(sampled)
            key = window
            if key != cleared_key:
                # Immediately remove the previous round from every current-
                # round read surface. A successfully prewarmed next book stays
                # subscribed but is not promoted until fresh discovery agrees.
                hub.set_round(None, None, None, window, window + 300)
                cleared_key = key
                last_key = None
                prepared_key = None
            if key != last_key or not hub.up_token or not hub.down_token:
                tokens = await asyncio.to_thread(
                    market_discovery.get_tokens_for_current_round, window)
                now_window = timer.window_start()
                if (tokens and int(tokens.get("window_start") or 0) == window
                        and now_window == window):
                    changed = hub.set_round(tokens.get("up_token_id"),
                                            tokens.get("down_token_id"),
                                            tokens.get("condition_id"),
                                            tokens.get("window_start"),
                                            tokens.get("window_end"))
                    if changed:
                        # `remain` predates the discovery call above, which can
                        # block for seconds. Report the clock as it is now, or
                        # the line understates how late the rotation landed.
                        on_event("rotation",
                                 f"round tokens subscribed with "
                                 f"{timer.seconds_left()}s left", "info")
                    last_key = key
            # Re-read the clock: current-round discovery above may have
            # blocked for seconds, and a stale `remain` was letting this branch
            # start a ~21s call with the boundary already in reach. A failed
            # attempt leaves prepared_key unset and would otherwise retry that
            # call on every poll, straight through the rotation.
            remain_now = timer.seconds_left()
            if (PREPARE_FLOOR_SECONDS <= remain_now
                    <= config.ROUND_PREPARE_LEAD_SECONDS
                    and prepared_key != window + 300):
                next_window = window + 300
                prepared = await asyncio.to_thread(
                    market_discovery.get_tokens_for_current_round, next_window)
                if (prepared
                        and int(prepared.get("window_start") or 0) == next_window):
                    if hub.prepare_round(prepared):
                        on_event("rotation",
                                 f"next round books pre-subscribed with "
                                 f"{timer.seconds_left()}s left", "good")
                    prepared_key = next_window
        except Exception as exc:
            on_event("rotation", f"{type(exc).__name__}: {exc}", "warn")
        # The opening print can only be latched inside the first 5s of a round,
        # so a rotation landing 6s late costs the whole round. Poll every second
        # across the boundary and back off in mid-round, where there is nothing
        # to win and gamma-api rate-limits.
        #
        # BUGFIX: `remain` is sampled at the TOP of the loop, before up to two
        # discovery calls that each block for as long as ~21s. Choosing the
        # cadence from that stale value meant a call spanning the boundary was
        # followed by a further ROUND_POLL_SECONDS of sleep - so rotation woke
        # ~10s INTO the new round, past the 5s window in which the opening
        # print can be latched, and the round was unrecoverable. Re-sample the
        # clock after the awaits, and never sleep past a boundary however long
        # discovery took.
        sampled_now = timer.unix()
        to_boundary = (timer.window_start(sampled_now) + 300) - sampled_now
        near_boundary = to_boundary <= 10.0 or to_boundary >= 290.0
        delay = 1.0 if near_boundary else config.ROUND_POLL_SECONDS
        await asyncio.sleep(max(0.1, min(delay, to_boundary + 0.05)))


async def _health_log(hub, cfg, agreement, reconciler, stop, ledger=None,
                      broker=None) -> None:
    """One compact line a minute. Not a dashboard; safe for a log file."""
    while not stop.is_set():
        await asyncio.sleep(60)
        try:
            h = hub.health()
            b = h["binance"]
            m = h["poly_market"]
            pnl = (broker.summary() if broker is not None else ledger.summary())
            balance_status = ("PAPER" if broker is not None else
                              ledger.reconcile_balance()["status"])
            agree = agreement.summary()
            print(f"[FEEDS] btc={b['status']}/{_ms(b['last_message_age_ms'])} "
                  f"rc={b['reconnect_count']} lat={_ms(b['latency_ms'])} | "
                  f"book={h['book']['status']} rc={m['reconnect_count']} "
                  f"resync={h['book']['rest_resyncs']} "
                  f"drop={h['book']['dropped_inactive']} | "
                  f"user={h['poly_user']['status']} "
                  f"fills={hub.fill_store.summary()['fills']} "
                  f"| agree={_pct(agree['agree_rate'])}"
                  f"/{_pct(agree['agree_rate_timed'])} "
                  f"n={agree['compared']} | "
                  f"pnl={_money(pnl['realized_pnl'])} "
                  f"pend={pnl['open_positions']} "
                  f"fees={_money(pnl['fees_paid'])} "
                  f"bal={balance_status}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            on_event("health", f"health log failed: {type(exc).__name__}: {exc}",
                     "warn")


def _money(v):
    return "--" if v is None else f"${v:+.4f}"


def _pct(v):
    return "--" if v is None else f"{v * 100:.1f}%"


def _ms(v):
    return "--" if v is None else f"{v:.0f}ms"


async def _dashboard(hub, cfg, agreement, reconciler, stop, *, ledger=None,
                     settler=None, broker=None) -> None:
    try:
        await _dashboard_inner(hub, cfg, agreement, reconciler, stop,
                               ledger=ledger, settler=settler, broker=broker)
    finally:
        # probe.install() replaces stdout and process-global functions.  A
        # cancelled or failed render task must restore all of them before the
        # runner emits its final diagnostics or attempts an in-process restart.
        try:
            from dashboard import probe
            probe.uninstall()
        except Exception as exc:
            on_event("dashboard", f"probe cleanup failed: {type(exc).__name__}: {exc}",
                     "bad")


def _task_failure_event(task: asyncio.Task, name: str) -> None:
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except (asyncio.CancelledError, Exception) as inspect_exc:
        on_event(name, f"cannot inspect task result: {type(inspect_exc).__name__}: "
                 f"{inspect_exc}", "bad")
        return
    if exc is not None:
        on_event(name, f"task stopped unexpectedly: {type(exc).__name__}: {exc}", "bad")


async def _dashboard_inner(hub, cfg, agreement, reconciler, stop, *, ledger=None,
                           settler=None, broker=None) -> None:
    """Attach the terminal dashboard if it is present in this tree."""
    try:
        from dashboard import TerminalState, build, glyphs, make_renderer, snapshot
        from dashboard import probe
    except Exception as exc:
        print(f"[FEEDS] dashboard unavailable: {exc}")
        return
    state = TerminalState()
    real_stdout = probe.install(state)          # probes AFTER adapters, by design
    try:
        probe.attach_asyncio(asyncio.get_running_loop(), state)
    except Exception as exc:
        state.event("DASH", f"asyncio hook attach failed: {type(exc).__name__}: {exc}",
                    "warn")
    renderer = make_renderer(real_stdout)
    _latency_next_emit = 0.0
    _LATENCY_EMIT_EVERY_S = 60.0
    g = glyphs()
    render_failed = False
    import config
    import main_bot
    import timer
    with renderer:
        while not stop.is_set():
            sampled_wall = timer.unix()
            round_window = timer.window_start(sampled_wall)
            state.set_round_context(
                round_window,
                timer.current_round_window_et(sampled_wall),
                timer.seconds_left(sampled_wall),
            )
            feed_snapshot = hub.snapshot()
            state.push_spot(feed_snapshot.btc_price)
            strike_service = getattr(main_bot, "_strike", None)
            state.push_chainlink(
                main_bot.current_chainlink_twap(),
                getattr(strike_service, "age_ms", None),
                getattr(strike_service, "value_ts_ms", None),
            )
            state.push_price_to_beat(
                main_bot.chainlink_twap_for_round(round_window),
                source="Chainlink strike service",
                round_key=round_window,
            )
            if feed_snapshot.up is not None:
                state.push_book(feed_snapshot.up_token,
                                *feed_snapshot.up.as_rest())
            if feed_snapshot.down is not None:
                state.push_down_book(feed_snapshot.down_token,
                                     *feed_snapshot.down.as_rest())
            token_state = {
                "condition_id": hub.condition_id,
                "up_token_id": hub.up_token,
                "down_token_id": hub.down_token,
                "window_start": hub.window_start,
                "window_end": hub.window_end,
                "slug": (f"btc-updown-5m-{hub.window_start}"
                         if hub.window_start else None),
            }
            if state.tokens.value != token_state:
                state.tokens.set(token_state)
            def mark(token):
                view = hub.book.view(token)
                return view.best_bid if view.status == "LIVE" else None

            accounting = (broker.summary(mark=mark) if broker is not None else
                          (ledger.summary(mark=mark) if ledger is not None else {}))

            # ---- what the stop loss is watching, and what it has sold -------
            exits = []
            held_legs = []
            if ledger is not None:
                label = {str(hub.up_token): "UP", str(hub.down_token): "DOWN"}
                with ledger._lock:
                    for token, pos in ledger.positions.items():
                        for lot in (pos.lots or []):
                            if str(lot.side or "").upper() != "SELL":
                                continue
                            exits.append({
                                "time": time.strftime("%H:%M:%S",
                                                      time.localtime(lot.wall)),
                                "wall": lot.wall,
                                "side": label.get(str(token), "--"),
                                "shares": lot.shares,
                                "price": lot.price,
                                "proceeds": lot.shares * lot.price - lot.fee,
                            })
                        if (not pos.settled and pos.shares > 1e-9
                                and pos.condition_id == hub.condition_id):
                            view = hub.book.view(str(token))
                            held_legs.append({
                                "side": label.get(str(token), "--"),
                                "shares": pos.shares,
                                "bid": (view.best_bid if view
                                        and view.status == "LIVE" else None),
                            })
                exits.sort(key=lambda e: e["wall"])
            remain = None
            if hub.window_end:
                remain = float(hub.window_end) - timer.unix()
            stop_status = {
                "enabled": bool(config.STOP_LOSS_ENABLED),
                "armed": bool(
                    config.STOP_LOSS_ENABLED and remain is not None
                    and config.STOP_LOSS_EXIT_CUTOFF_SECONDS < remain
                    <= config.STOP_LOSS_ARM_SECONDS),
                "trigger": config.STOP_LOSS_PRICE,
                "floor": config.STOP_LOSS_FLOOR_PRICE,
                "arm": config.STOP_LOSS_ARM_SECONDS,
                "cutoff": config.STOP_LOSS_EXIT_CUTOFF_SECONDS,
                "held": held_legs,
            }
            with state.lock():
                state.accounting = accounting
                state.exits = exits[-40:]
                state.stop_status = stop_status
                state.late_trim = dict(getattr(main_bot, "_late_trim_status", None) or {})
                if broker is not None:
                    state.balance.set({
                        "balance": accounting.get("cash", 0.0),
                        "allowance": accounting.get("cash", 0.0),
                        "paper": True,
                    })
                elif ledger.balance_marks:
                    state.balance.set({
                        "balance": ledger.balance_marks[-1][1],
                        "allowance": None,
                        "paper": False,
                    })
            snap = snapshot(state, session_trades=main_bot.session_trades)
            fh = hub.health()
            strike_health = main_bot._strike.health() if main_bot._strike else {}
            settlement_state = (
                "ABSENT" if settler is None else
                settler.health_status()
            )
            snap["health"].update({
                "BINANCE WS": fh["binance"]["status"],
                "POLY WS": fh["poly_market"]["status"],
                "POLY BOOK": fh["book"]["status"],
                "CHAINLINK": strike_health.get("status", "DISCONNECTED"),
                "USER WS": ("ABSENT" if broker is not None else
                            fh["poly_user"]["status"]),
                "DATABASE": ("ABSENT" if ledger is None else
                             "ERROR" if ledger.last_persistence_error else "OK"),
                "RECONCILE": ("ABSENT" if broker is not None else
                              ("OK" if (reconciler and not reconciler.armed) else "WAIT")),
                "SETTLEMENT": settlement_state,
            })
            if getattr(renderer, "interactive", False):
                try:
                    renderer.cols, renderer.rows = renderer.size()
                    renderer.draw(build(snap, renderer.cols, renderer.rows, g))
                    with state.lock():
                        state.render_ms.append(renderer.last_ms)
                        state.frames += 1
                    try:
                        state.latency.observe("frame", renderer.last_ms)
                    except Exception:
                        pass
                    now_mono = time.monotonic()
                    if now_mono >= _latency_next_emit:
                        _latency_next_emit = now_mono + _LATENCY_EMIT_EVERY_S
                        try:
                            snaps = state.latency.snapshot()
                        except Exception:
                            snaps = []
                        parts: list[str] = []
                        for h in snaps:
                            if h.n <= 0 or h.p95 is None:
                                continue
                            parts.append(
                                f"{h.stage} n={h.n} p50={h.p50:.0f} "
                                f"p95={h.p95:.0f} p99={h.p99:.0f} "
                                f"max={h.max:.0f}ms"
                            )
                        if parts:
                            state.event("LAT", " | ".join(parts), "info")
                except Exception as exc:
                    # A layout bug must not take the trading loop with it, and
                    # it must not stop at a frozen screen either: this task is
                    # never awaited, so an escape here would die unheard.
                    state.telemetry_error = f"render: {type(exc).__name__}"
                    state.event("DASH", f"render failed: {type(exc).__name__}: {exc}", "bad")
                    if not render_failed:
                        render_failed = True
                        _record_exit(f"dashboard render failed (bot continues): "
                                     f"{type(exc).__name__}: {exc}",
                                     traceback.format_exc())
            await asyncio.sleep(1 / 6)


async def health_only() -> None:
    """Connect the feeds, print health for 30s, never touch the bot."""
    hub, cfg, agreement = build_hub(read_only=True)
    print(f"[FEEDS] MODE=HEALTH-ONLY (NO WALLET AUTH) | {cfg.describe()}")
    tasks = _start_feed_tasks(hub, cfg)
    try:
        for _ in range(6):
            await asyncio.sleep(5)
            print(json.dumps(hub.health(), indent=2, default=str))
    finally:
        try:
            await hub.stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            adapters.uninstall()


def _record_exit(reason: str, detail: str = "") -> None:
    """Append why this process stopped, and never fail while doing it.

    While the dashboard has captured stderr, a raw stderr write would land on
    top of the alt-screen frame. Route through captured stdout instead — the
    probe funnels it to the event feed. Once the dashboard has uninstalled
    (post-shutdown), stderr is safe to use again.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    line = f"[{stamp}] pid={os.getpid()} {reason}\n"
    try:
        with (Path(__file__).parent / "bot_exit.log").open("a", encoding="utf-8") as fh:
            fh.write(line + (detail.rstrip() + "\n" if detail else ""))
    except OSError as exc:
        try:
            from dashboard import probe as _probe
            dashboard_up = _probe.is_installed()
        except Exception:
            dashboard_up = False
        target = sys.stdout if dashboard_up else sys.stderr
        print(f"[FEEDS] could not append bot_exit.log: {type(exc).__name__}: {exc}",
              file=target)
    try:
        from dashboard import probe as _probe
        dashboard_up = _probe.is_installed()
    except Exception:
        dashboard_up = False
    target = sys.stdout if dashboard_up else sys.stderr
    print(f"[FEEDS] stopped - {reason}", file=target)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dash", action="store_true", help="attach the terminal dashboard")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--health", action="store_true",
                      help="public feeds only; no wallet auth and no trading")
    mode.add_argument("--paper", action="store_true",
                      help="paper mode (default): live public data with simulated FOK fills")
    mode.add_argument("--live", action="store_true",
                      help="explicitly enable authenticated live-wallet order submission")
    ap.add_argument("--paper-balance", type=float,
                    help="starting paper cash (first launch only; default PAPER_START_BALANCE or 1000)")
    args = ap.parse_args()
    if args.paper_balance is not None and args.live:
        ap.error("--paper-balance cannot be combined with --live")
    if args.health and args.dash:
        ap.error("--dash cannot be combined with --health")
    # Every exit gets a line on disk. Ctrl+C used to be swallowed silently and
    # the dashboard restores the screen on its way out, so a stop left no
    # trace anywhere - the console scrollback, the only witness, was gone.
    reason, detail = "clean shutdown: run() returned", ""
    try:
        asyncio.run(health_only() if args.health else
                    run(dash=args.dash, paper=not args.live,
                        paper_balance=args.paper_balance))
    except KeyboardInterrupt:
        reason = "KeyboardInterrupt: Ctrl+C, or the console sent an interrupt"
    except BaseException as exc:
        _record_exit(f"{type(exc).__name__}: {exc}", traceback.format_exc())
        raise
    _record_exit(reason, detail)
