"""Pure layout.

`build(snap, cols, rows, g, now)` returns exactly `rows` rows, each exactly
`cols` visible characters wide. No I/O, no clock reads, no bot imports — the
snapshot carries everything. That is what lets the tests assert on geometry
at 200 terminal sizes without a TTY.
"""
from __future__ import annotations

import copy
import math
import time
from collections.abc import Mapping
from typing import Any

from .state import MISSING, TerminalState
from .safety import terminal_text
from .theme import Glyphs, Style, pnl_style, state_fg, state_style
from .widgets import (DIM, FAINT, PAPER, RULE, Row, blank,
                      candles, chip, fit, giant_digits,
                      histogram, hsplit, join, kv, meter, pad, panel,
                      sparkline, table, trunc)

# ---------------------------------------------------------------- snapshot ---


def snapshot(st: TerminalState, session_trades: list | None = None) -> dict[str, Any]:
    """Copy everything the renderer needs under one lock acquisition."""
    with st.lock():
        now_wall = time.time()
        now_mono = time.monotonic()
        book = st.best_book()
        down_book = st.best_down_book()
        balance = copy.deepcopy(st.balance.value) if isinstance(st.balance.value, Mapping) else None
        tokens = copy.deepcopy(st.tokens.value) if isinstance(st.tokens.value, Mapping) else None
        last_order = (copy.deepcopy(st.last_order.value)
                      if isinstance(st.last_order.value, Mapping) else None)
        if last_order is not None:
            last_order = {
                "side": terminal_text(last_order.get("side"), 16),
                "amount": _finite(last_order.get("amount")),
                "ok": bool(last_order.get("ok", False)),
                "error": terminal_text(last_order.get("error"), 1000)
                if last_order.get("error") else None,
            }
        accounting = (copy.deepcopy(st.accounting)
                      if isinstance(st.accounting, Mapping) else {})
        trade_source = session_trades if session_trades is not None else st.trades
        trades = [copy.deepcopy(t) for t in list(trade_source) if isinstance(t, Mapping)]
        snap = {
            "now": now_wall,
            "mono": now_mono,
            "uptime": max(0.0, now_mono - st.started_mono),
            "round_label": str(st.round_label),
            "round_key": st.round_key,
            "seconds_left": (int(st.seconds_left)
                             if _finite(st.seconds_left) is not None else None),
            "health": st.feed_health(now_mono),
            "spot": _finite(st.spot.value),
            "spot_age": st.spot_changed.age_at(now_mono),
            "spot_status": st.spot_changed.status_at(5.0, 20.0, now_mono),
            "chainlink": _finite(st.chainlink.value),
            "chainlink_age": st.chainlink.age_at(now_mono),
            "chainlink_ms": _finite(st.chainlink.latency_ms),
            "chainlink_repeat": st.chainlink_repeat,
            "chainlink_calls": st.chainlink.count,
            "start_price": _finite(st.start_price.value),
            "start_price_src": st.start_price.source,
            "start_chainlink": _finite(st.start_chainlink.value),
            "start_chainlink_src": st.start_chainlink.source,
            "book": book,
            "down_book": down_book,
            "book_age": st.book.age_at(now_mono),
            "book_ms": _finite(st.book.latency_ms),
            "book_token": st.book_token,
            "down_book_token": st.down_book_token,
            "book_status": st.book.status_at(90.0, 400.0, now_mono),
            "sig_price": terminal_text(st.sig_price.value, 16) if st.sig_price.value else None,
            "sig_book": terminal_text(st.sig_book.value, 16) if st.sig_book.value else None,
            "sig_chainlink": terminal_text(st.sig_chainlink.value, 16) if st.sig_chainlink.value else None,
            "decision": terminal_text(st.decision.value, 16) if st.decision.value else None,
            "decision_forced": st.decision_forced,
            "last_order": last_order,
            "last_order_ms": _finite(st.last_order.latency_ms),
            "last_order_error": terminal_text(st.last_order_error, 1000)
            if st.last_order_error else None,
            "telemetry_error": terminal_text(st.telemetry_error, 1000)
            if st.telemetry_error else None,
            "orders_ok": st.orders_ok,
            "orders_fail": st.orders_fail,
            "staked": _finite(st.staked) or 0.0,
            "stake_curve": [number for _, value in st.stake_curve
                            if (number := _finite(value)) is not None],
            "cancel": st.cancel.value,
            "balance": balance,
            "balance_age": st.balance.age_at(now_mono),
            "tokens": tokens,
            "token_fallback": st.token_fallback,
            "candles": [(c.o, c.h, c.l, c.c) for c in st.candles],
            "candle_t": [c.t for c in st.candles],
            "events": copy.deepcopy(list(st.events)[-80:]),
            "trades": trades,
            "exits": [copy.deepcopy(e) for e in list(st.exits)[-40:]],
            "stop_status": copy.deepcopy(st.stop_status) if isinstance(st.stop_status, Mapping) else {},
            "late_trim": copy.deepcopy(st.late_trim) if isinstance(st.late_trim, Mapping) else {},
            "absent": dict(st.absent),
            "overlay": copy.deepcopy(st.overlay),
            "loop_status": st.loop_beat.status_at(3.0, 12.0, now_mono),
            "loop_age": st.loop_beat.age_at(now_mono),
            "bet_size": _finite(st.bet_size),
            "trade_window": st.trade_window,
            "max_buy_price": _finite(st.max_buy_price),
            "min_buy_price": _finite(st.min_buy_price),
            "render_ms": (_finite(sum(st.render_ms) / len(st.render_ms))
                          if st.render_ms else None),
            "latency": st.latency.snapshot(),
            "frames": st.frames,
            "mode": terminal_text(st.mode, 16),
            "accounting": accounting,
        }
    return snap


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _up_down_share(trades, field: str) -> str | None:
    """Count this-round journal sides for one signal. Missing sides stay out."""
    if not trades:
        return None
    up = sum(1 for t in trades if str(t.get(field) or "").upper() == "UP")
    down = sum(1 for t in trades if str(t.get(field) or "").upper() == "DOWN")
    if up + down == 0:
        return None
    return f"UP {up}  DOWN {down}"


def _fmt_pnl(value: Any) -> str | None:
    number = _finite(value)
    return None if number is None else f"${number:+,.4f}"


def _current_round_book(snap) -> Mapping | None:
    """Open inventory for the displayed round; else the largest still-open book."""
    books = [item for item in
             list((snap.get("accounting") or {}).get("round_books") or [])
             if isinstance(item, Mapping)]
    if not books:
        return None
    cond = (snap.get("tokens") or {}).get("condition_id")
    if cond:
        for book in books:
            if book.get("condition_id") == cond:
                return book
    return max(books, key=lambda item: abs(_finite(item.get("round_cost")) or 0.0))


def _legs_by_side(book: Mapping | None, tokens: Mapping) -> tuple[Mapping | None, Mapping | None]:
    if not book:
        return None, None
    up_id = str(tokens.get("up_token_id") or "")
    down_id = str(tokens.get("down_token_id") or "")
    up = down = None
    for leg in list(book.get("legs") or []):
        if not isinstance(leg, Mapping):
            continue
        tid = str(leg.get("token_id") or "")
        if up_id and tid == up_id:
            up = leg
        elif down_id and tid == down_id:
            down = leg
    return up, down


def _sh_cost(leg: Mapping | None) -> str | None:
    if not leg:
        return None
    shares = _finite(leg.get("shares"))
    cost = _finite(leg.get("cost"))
    if shares is None or cost is None:
        return None
    return f"{shares:.5f} / ${cost:.5f}"


# ------------------------------------------------------------------ sizing ---
class Sizing:
    """Row/column budget for the current terminal, recomputed every frame."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.wide = cols >= 150
        self.narrow = cols < 84
        self.stack = cols < 64
        self.status_rows = 3 if rows >= 20 else 1

        fixed = 1 + 1 + self.status_rows + 1        # header, health, status, footer
        avail = rows - fixed
        self.top = self.mid = self.bot = 0
        if avail >= 23:
            extra = avail - 23
            self.top = min(18, 11 + int(extra * 0.40))
            self.mid = min(16, 8 + int(extra * 0.35))
            self.bot = avail - self.top - self.mid
        elif avail >= 17:
            self.top = 9
            self.mid = avail - 9
        elif avail >= 8:
            self.top = avail
        else:
            self.top = max(0, avail)
        self.show_mid = self.mid >= 6
        self.show_bot = self.bot >= 6
        if not self.show_mid:
            self.top += self.mid
            self.mid = 0
        if not self.show_bot:
            self.top += self.bot // 2
            self.mid += self.bot - self.bot // 2
            self.bot = 0
        self.show_chart = cols >= 84 and self.top >= 8
        self.show_dist = cols >= 118


def L(short: str, long: str, s: Sizing) -> str:
    return long if not s.narrow else short


def _trade_success(result) -> bool:
    return str(result or "").lower() not in {
        "", "rejected_or_unsubmitted", "failed", "rejected",
    }


# ------------------------------------------------------------------ pieces ---
def _header(snap, cols: int, g: Glyphs, s: Sizing) -> Row:
    clk = time.strftime("%H:%M:%S", time.localtime(snap["now"]))
    secs = snap["seconds_left"]
    left = [
        (" BTC-5M CLOBv2 ", Style("white", "ink", bold=True)),
        (" ", PAPER),
        (f"{snap['mode']:<5}", Style("purple", bold=True)),
        (g.v, RULE),
        (" BTC/USDT UP-DOWN ", Style("ink")),
        (g.v, RULE),
        (f" {snap['round_label']} ", Style("blue", bold=True)),
        (g.v, RULE),
    ]
    if secs is not None:
        armed = snap["trade_window"] is not None and secs <= snap["trade_window"]
        left += [(f" T-{secs:03d} ", Style("white", "red" if armed else "blue", bold=True))]
    else:
        left += [(" T-??? ", FAINT)]
    right = [
        (f" render {snap['render_ms']:.0f}ms " if snap["render_ms"] else " render --  ", FAINT),
        (g.v, RULE),
        (f" {clk} ", Style("ink", "cream", bold=True)),
    ]
    rw = sum(len(t) for t, _ in right)
    lw = sum(len(t) for t, _ in left)
    mid = cols - lw - rw
    if mid < 0:
        return pad(left + right, cols)
    return pad(left + [(" " * mid, PAPER)] + right, cols)


def _health(snap, cols: int, g: Glyphs, s: Sizing) -> Row:
    order = ["BINANCE WS", "POLY WS", "POLY BOOK", "CHAINLINK", "USER WS",
             "DATABASE", "RECONCILE", "SETTLEMENT", "GAMMA API", "LOOP"]
    short = {"BINANCE WS": "BNC", "POLY WS": "PWS", "POLY BOOK": "BOOK",
             "CHAINLINK": "CHNL", "USER WS": "UWS", "DATABASE": "DB",
             "RECONCILE": "RECN", "SETTLEMENT": "SETL", "GAMMA API": "GMMA",
             "LOOP": "LOOP"}
    h = snap["health"]
    row: Row = []
    for name in order:
        st = h.get(name, "WAIT")
        label = short[name] if s.narrow else name
        mark = {"OK": "\u25cf", "STALE": "\u25d1", "DISCONNECTED": "\u25cb",
                "WAIT": "\u25cb", "ABSENT": "\u00b7"}.get(st, "\u00b7")
        row.append((f" {mark}{label} ", state_style(st)))
        row.append((" ", PAPER))
        if sum(len(t) for t, _ in row) > cols - 8:
            break
    return pad(row, cols)


def _kpi(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    bal = snap["balance"]
    acct = snap.get("accounting") or {}
    w = cols - 2
    body: list[Row] = []

    # Five-row bold block words. A terminal cannot change font size, so
    # height and heavy glyphs are what make the cash figure giant.
    cash = _finite(bal.get("balance")) if isinstance(bal, Mapping) else None
    if cash is not None:
        head = f"${cash:,.2f}".replace(",", "")
        hstyle = Style("green", bold=True)
    else:
        head, hstyle = MISSING, Style("faint")
    body += giant_digits(head, w, hstyle, g=g)
    body.append([(g.h * w, RULE)])

    ok, fail = snap["orders_ok"], snap["orders_fail"]
    tot = ok + fail
    body.append([
        (fit("ORDERS OK/FAIL", w - 12, "<"), DIM),
        (fit(str(ok), 6, ">"), Style("green", bold=True) if ok else DIM),
        (" /", DIM),
        (fit(str(fail), 4, ">"), Style("red", bold=True) if fail else DIM),
    ])
    body.append(kv("SEND RATE", f"{ok / tot * 100:.0f}%" if tot else MISSING, w,
                   Style("ink") if tot else FAINT))
    body.append(kv("CUM STAKE SENT", f"${snap['staked']:,.2f}", w, Style("blue", bold=True)))
    body.append(kv("BET SIZE", f"${snap['bet_size']:,.2f}" if snap["bet_size"] else MISSING, w))
    body.append([(g.h * w, RULE)])
    realized = _finite(acct.get("realized_pnl"))
    unreal = _finite(acct.get("unrealized_mark_to_bid"))
    total = _finite(acct.get("total_pnl", acct.get("equity_pnl")))
    exposure = _finite(acct.get("pending_cost"))
    win_rate = _finite(acct.get("win_rate"))
    if acct:
        # Total equity is withheld while any open position cannot be marked,
        # which is every position between its round ending and Polymarket
        # publishing the resolution. Say why it is blank instead of showing a
        # bare `--`, and never hide realized PnL behind it: that figure is
        # exact the moment a market resolves.
        pending = acct.get("unmarkable_positions") or 0
        body.append(kv("TOTAL PNL",
                       f"${total:+,.4f}" if total is not None else
                       (f"{MISSING} {pending} unsettled" if pending else MISSING),
                       w, pnl_style(total)))
        body.append(kv(
            "REALIZED / UNREAL" + (" (part)" if pending else ""),
            f"${realized:+,.4f} / ${unreal:+,.4f}"
            if realized is not None and unreal is not None else
            (f"${realized:+,.4f} / {MISSING}" if realized is not None else MISSING),
            w, pnl_style(realized)))
        body.append(kv(
            "EXPOSURE / WINRATE",
            f"${exposure:,.2f} / {win_rate * 100:.1f}%"
            if exposure is not None and win_rate is not None else
            (f"${exposure:,.2f} / {MISSING}" if exposure is not None else MISSING),
            w, Style("ink")))
        body.append(kv("WINS / LOSSES",
                       f"{acct.get('wins', 0)} / {acct.get('losses', 0)}", w))
    else:
        body.append(kv("TOTAL PNL", MISSING, w, FAINT))
        body.append(kv("REALIZED / UNREAL", MISSING, w, FAINT))
        body.append(kv("EXPOSURE / WINRATE", MISSING, w, FAINT))
        body.append(kv("WINS / LOSSES", MISSING, w, FAINT))

    age = snap["balance_age"]
    note = ("paper" if snap["mode"] == "PAPER" else
            (f"{age / 60:,.0f}m old" if (bal is not None and age is not None) else "no pnl"))
    title = "PAPER CASH" if snap["mode"] == "PAPER" else "ACCOUNT USDC"
    title_style = (Style("white", "ink", bold=True) if snap["mode"] == "PAPER"
                   else Style("ink", "cream", bold=True))
    note_style = Style("ink", bold=True) if snap["mode"] == "PAPER" else FAINT
    return panel(title, body, cols, rows, g, right_note=note,
                 title_style=title_style, note_style=note_style)


def _round_panel(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    b = snap["book"]
    down = snap["down_book"]
    spot = snap["spot"]
    price_to_beat = snap["start_chainlink"]
    running = snap["chainlink"]
    dist = (running - price_to_beat
            if running is not None and price_to_beat is not None else None)
    body: list[Row] = []
    body.append(kv("ROUND", snap["round_label"], w, Style("blue", bold=True)))
    body.append(kv("SECONDS LEFT", f"{snap['seconds_left']}s" if snap["seconds_left"] is not None else MISSING, w))
    body.append(kv("PRICE TO BEAT",
                   f"${price_to_beat:,.2f}" if price_to_beat is not None else MISSING,
                   w, Style("amber", bold=True) if price_to_beat is not None else FAINT))
    running_txt = f"${running:,.2f}" if running is not None else MISSING
    if running is not None and snap["chainlink_repeat"]:
        running_txt += f"  x{snap['chainlink_repeat'] + 1} same"
    body.append(kv("RUNNING PRICE", running_txt, w,
                   pnl_style(dist) if dist is not None else
                   (Style("ink", bold=True) if running is not None else FAINT)))
    body.append(kv("DIST TO BEAT", f"{dist:+,.2f}" if dist is not None else MISSING, w,
                   pnl_style(dist)))
    body.append(kv(L("BINANCE SPOT", "BINANCE SPOT (aux)", s),
                   f"${spot:,.2f}" if spot is not None else MISSING, w,
                   Style("ink") if spot is not None else FAINT))
    body.append([(g.h * w, RULE)])
    ask = f"{b['ask']:.3f}" if b.get("ask") is not None else MISSING
    bid = f"{b['bid']:.3f}" if b.get("bid") is not None else MISSING
    spr = f"{b['spread']:.3f}" if b.get("spread") is not None else MISSING
    body.append(kv(L("UP ASK/BID", "UP  ASK / BID", s), f"{ask} / {bid}", w,
                   Style("ink") if b else FAINT))
    body.append(kv(L("UP SPREAD", "UP  SPREAD", s), spr, w, Style("ink") if b else FAINT))
    down_ask = f"{down['ask']:.3f}" if down.get("ask") is not None else MISSING
    down_bid = f"{down['bid']:.3f}" if down.get("bid") is not None else MISSING
    body.append(kv(L("DN ASK/BID", "DOWN ASK / BID", s),
                   f"{down_ask} / {down_bid}", w,
                   Style("ink") if down else FAINT))
    body.append([(g.h * w, RULE)])
    d = snap["decision"]
    body.append(kv("CURRENT SIDE", d or MISSING, w,
                   Style("green" if d == "UP" else "red", bold=True) if d else FAINT))
    mom = snap["sig_price"]
    binance_move = (spot - snap["start_price"]
                    if spot is not None and snap["start_price"] is not None else None)
    if mom and binance_move is not None:
        mom_txt = f"{mom}  {binance_move:+,.2f}"
    elif mom:
        mom_txt = mom
    else:
        mom_txt = MISSING
    body.append(kv("MOMENTUM", mom_txt, w,
                   Style("green" if mom == "UP" else "red", bold=True) if mom else FAINT))
    tokens = snap.get("tokens") or {}
    book = _current_round_book(snap)
    up_leg, down_leg = _legs_by_side(book, tokens)
    up_txt = _sh_cost(up_leg)
    down_txt = _sh_cost(down_leg)
    pair = _finite(book.get("pair_entry_with_fees") if book else None)
    if pair is None:
        pair = _finite(book.get("pair_entry") if book else None)
    live = _finite(book.get("live_pnl") if book else None)
    live_txt = _fmt_pnl(live)
    pending = int((book or {}).get("unmarkable_legs") or 0)
    body.append(kv("UP SHARES / COST", up_txt or MISSING, w,
                   Style("ink") if up_txt else FAINT))
    body.append(kv("DN SHARES / COST", down_txt or MISSING, w,
                   Style("ink") if down_txt else FAINT))
    body.append(kv("PAIR PRICE",
                   f"{pair:.5f}" if pair is not None else MISSING, w,
                   Style("ink") if pair is not None else FAINT))
    body.append(kv("ROUND LIVE PNL",
                   live_txt if live_txt is not None else
                   (f"{MISSING} {pending} unmarkable" if pending else MISSING),
                   w, pnl_style(live) if live_txt is not None else FAINT))
    body.append(kv("ASK BAND (MIN-MAX)",
                   (f"{snap['min_buy_price']:.2f}-{snap['max_buy_price']:.2f}"
                    if snap.get("min_buy_price") is not None and snap["max_buy_price"]
                    else (f"{snap['max_buy_price']:.2f}" if snap["max_buy_price"] else MISSING)),
                   w, Style("amber", bold=True)))
    return panel("ROUND / POSITION", body, cols, rows, g, right_note="")


def _chart(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    inner_w, inner_h = cols - 2, rows - 2
    body = candles(snap["candles"], inner_w, inner_h, g,
                   ref=snap["start_price"], last=snap["spot"],
                   times=snap.get("candle_t"))
    spot, open_price = snap["spot"], snap["start_price"]
    secs = snap["seconds_left"]
    above = None if (spot is None or open_price is None) else spot >= open_price
    bits = [f"{TerminalState.CANDLE_SECONDS}s"]
    if secs is not None:
        bits.append(f"T-{secs:03d}")
    bits.append("ABOVE OPEN" if above else
                ("BELOW OPEN" if above is False else "no Binance open"))
    note_style = (Style("green", bold=True) if above else
                  (Style("red", bold=True) if above is False else FAINT))
    return panel("BTC/USDT  BINANCE @trade", body, cols, rows, g,
                 right_note="  ".join(bits), note_style=note_style)


def _status_strip(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    secs = snap["seconds_left"]
    armed = secs is not None and snap["trade_window"] is not None and secs <= snap["trade_window"]
    sides = [snap["sig_price"], snap["sig_book"], snap["sig_chainlink"]]
    named = [x for x in sides if x]
    agree = f"{max((named.count(x) for x in set(named)), default=0)}/3" if named else MISSING
    d = snap["decision"]
    lo = snap["last_order"]
    cap = snap["max_buy_price"]
    floor = snap.get("min_buy_price")
    acct = snap.get("accounting") or {}
    open_positions = acct.get("open_positions")
    settle_health = snap["health"].get("SETTLEMENT", "ABSENT")
    cells = [
        ("ROUND", "ROUND", f"T-{secs:03d}" if secs is not None else "--", "ARMED" if armed else "IDLE"),
        ("SIGNAL", "SIGNAL", agree, "OK" if named else "WAIT"),
        ("GATE", "ENTRY GATE", "ABSENT", "ABSENT"),
        ("SIDE", "SIDE", d or "--", "UP" if d == "UP" else ("DOWN" if d == "DOWN" else "WAIT")),
        ("MOM", "MOMENTUM", snap["sig_price"] or "--",
         snap["sig_price"] or "WAIT"),
        ("EDGE", "EDGE", "ABSENT", "ABSENT"),
        ("PAIR", "PAIR COST", "ABSENT", "ABSENT"),
        ("RISK", "RISK", "ABSENT", "ABSENT"),
        ("ASKCAP", "ASK GUARD",
         (f"{floor:.2f}-{cap:.2f}" if floor is not None and cap else
          (f"<={cap:.2f}" if cap else "--")),
         "OK" if cap else "WAIT"),
        ("BOOK", "BOOK FRESH", snap["book_status"], snap["book_status"]),
        ("EXEC", "EXECUTION", ("OK" if lo["ok"] else "FAIL") if lo else "IDLE",
         ("OK" if lo["ok"] else "FAIL") if lo else "IDLE"),
        ("POS", "POSITION", str(open_positions) if open_positions is not None else "ABSENT",
         "OK" if open_positions is not None else "ABSENT"),
        ("SETTLE", "SETTLEMENT", settle_health, settle_health),
    ]
    if s.stack:
        cells = [c for c in cells if c[3] != "ABSENT"]

    n = len(cells)
    base = max(6, cols // n)
    widths = [base] * n
    widths[-1] += cols - base * n
    if widths[-1] < 6:                       # never let the last cell collapse
        widths = [max(6, (cols - 6) // n)] * n
        widths[-1] = cols - sum(widths[:-1])
    use_long = base >= 12

    if rows == 1:
        row: Row = []
        for (short, long, val, st), wd in zip(cells, widths):
            row += chip(f"{trunc(short, max(1, wd - 4))}:{val}", st, wd)
        return [pad(row, cols)]

    top: Row = []
    label: Row = []
    value: Row = []
    for (short, long, val, st), wd in zip(cells, widths):
        style = state_style(st)
        name = long if use_long else short
        top += [(g.h * wd, RULE)]
        label += [(fit(" " + trunc(name, wd - 1), wd, "<"), Style("dim", "cream"))]
        value += [(fit(" " + trunc(val, wd - 1), wd, "<"), style)]
    return [pad(top, cols), pad(label, cols), pad(value, cols)][:rows]


def _pipeline(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    """The bot's real path, one node per row, connected by a left rail."""
    w = cols - 2
    b = snap["book"]
    tok = snap["tokens"] or {}

    def node(name, value, st, extra=""):
        return (name, value, st, extra)

    def px(value):
        """One side of the book can be empty; that is a state, not an error."""
        return f"{value:.3f}" if value is not None else MISSING

    spot_ok = snap["spot_status"]
    chainlink_status = snap["health"].get("CHAINLINK", "WAIT")
    nodes = [
        node("PRICE WS", f"${snap['spot']:,.2f}" if snap["spot"] is not None else "--", spot_ok,
             f"{snap['spot_age']:.1f}s" if snap["spot_age"] is not None else ""),
        node("RUNNING PRICE",
             f"${snap['chainlink']:,.2f}" if snap["chainlink"] is not None else "--",
             chainlink_status,
             f"same x{snap['chainlink_repeat'] + 1}" if snap["chainlink_repeat"] else
             (f"{snap['chainlink_ms']:.0f}ms" if snap["chainlink_ms"] is not None else "")),
        node("ROUND CLOCK", f"T-{snap['seconds_left']:03d}" if snap["seconds_left"] is not None else "--",
             "OK" if snap["seconds_left"] is not None else "WAIT", snap["round_label"]),
        node("PRICE TO BEAT",
             f"${snap['start_chainlink']:,.2f}" if snap["start_chainlink"] is not None else "--",
             "OK" if snap["start_chainlink"] is not None else "WAIT",
             snap["start_chainlink_src"]),
        node("MARKET DISC", trunc(str(tok.get("slug") or "--"), 26),
             "FAIL" if snap["token_fallback"] else ("OK" if tok else "WAIT"),
             "PREV WINDOW" if snap["token_fallback"] else ""),
        node("BOOK FETCH",
             f"a{px(b.get('ask'))} b{px(b.get('bid'))}" if b else "--",
             snap["book_status"], f"{snap['book_ms']:.0f}ms" if snap["book_ms"] else ""),
        node("SIG PRICE", snap["sig_price"] or "--", snap["sig_price"] or "WAIT"),
        node("SIG BOOK", snap["sig_book"] or "--", snap["sig_book"] or "WAIT"),
        node("SIG CHAINLINK", snap["sig_chainlink"] or "--", snap["sig_chainlink"] or "WAIT"),
        node("FINAL DECISION", snap["decision"] or "--", snap["decision"] or "WAIT",
             ""),
        node("CANCEL OPEN", "OK" if snap["cancel"] else ("FAIL" if snap["cancel"] is False else "--"),
             "OK" if snap["cancel"] else ("FAIL" if snap["cancel"] is False else "IDLE")),
        node("PAPER FOK" if snap["mode"] == "PAPER" else "PLACE FOK",
             (("FILLED" if snap["mode"] == "PAPER" else "SENT")
              if snap["last_order"]["ok"] else "REJECTED") if snap["last_order"] else "--",
             ("OK" if snap["last_order"]["ok"] else "FAIL") if snap["last_order"] else "IDLE",
             trunc(snap["last_order_error"] or "", 22) if snap["last_order"] and not snap["last_order"]["ok"] else
             (f"{snap['last_order_ms']:.0f}ms" if snap["last_order_ms"] else "")),
        node("TRADE LOG", f"{len(snap['trades'])} rows", "OK" if snap["trades"] else "IDLE", "csv"),
    ]

    # Collapse in a fixed order when the panel is short. The tail of the
    # pipeline (decision -> order) is what matters during a trade window, so
    # the head collapses first.
    room = rows - 2
    if len(nodes) > room:
        sigs = [n for n in nodes if n[0].startswith("SIG ")]
        merged = "/".join((n[1] if n[1] != "--" else "-") for n in sigs)
        nodes = [n for n in nodes if not n[0].startswith("SIG ")]
        nodes.insert(6, node("SIGNALS P/B/C", merged, snap["decision"] or "WAIT"))
    for name in ("CANCEL OPEN", "RUNNING PRICE", "TRADE LOG", "MARKET DISC", "ROUND CLOCK"):
        if len(nodes) <= room:
            break
        nodes = [n for n in nodes if n[0] != name]

    name_w = 14 if not s.narrow else 11
    val_w = max(6, min(20, w - name_w - 8))
    body: list[Row] = []
    for i, (name, value, st, extra) in enumerate(nodes):
        rail = g.tee_r if i else g.tl
        arrow = g.arrow_r
        style = state_fg(st)
        row: Row = [
            (rail + g.h, RULE), (arrow, Style("rule")),
            ("[", RULE), (fit(name, name_w, "<"), Style("ink", bold=True)), ("]", RULE),
            (" ", PAPER), (fit(value, val_w, "<"), style),
        ]
        used = sum(len(t) for t, _ in row)
        if extra and w - used > 4:
            row.append((" " + fit(extra, w - used - 1, "<"), FAINT))
        body.append(pad(row, w))

    absent = snap["absent"]
    if len(body) < rows - 2:
        body.append([(g.h * w, RULE)])
        body.append([(fit(" NOT IN THIS BUILD (rendered as ABSENT, never faked)", w, "<"),
                      Style("purple", bold=True))])
        keys = list(absent.keys())
        per = max(1, w // 15)
        for i in range(0, len(keys), per):
            chunk = " ".join(f"{k}" for k in keys[i:i + per])
            body.append([(fit("  " + chunk, w, "<"), FAINT)])

    return panel("DECISION PIPELINE  (observed)", body, cols, rows, g,
                 right_note=snap["decision"] or "no side")


def _matrix(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    b = snap["book"]
    down_book = snap["down_book"]
    all_hdr = ["SIDE", "BID", "ASK", "SPRD", "DEPTH", "FAIR", "EDGE", "POS", "COST", "PNL", "STATUS"]
    all_cw = [5, 6, 6, 5, 7, 5, 5, 4, 5, 5, 11]
    # Keep order, drop the least useful columns until the row fits exactly.
    drop_order = ["POS", "COST", "PNL", "EDGE", "FAIR", "DEPTH", "SPRD"]
    keep = list(all_hdr)
    def fits(cols_kept):
        return sum(all_cw[all_hdr.index(h)] + 1 for h in cols_kept) <= w
    for d in drop_order:
        if fits(keep):
            break
        keep.remove(d)
    hdr = keep
    cw = [all_cw[all_hdr.index(h)] for h in keep]
    idx = [all_hdr.index(h) for h in keep]

    def f(v, spec="{:.3f}"):
        value = _finite(v)
        return spec.format(value) if value is not None else MISSING

    up = [
        ("UP", Style("green", bold=True)),
        (f(b.get("bid")), Style("green")),
        (f(b.get("ask")), Style("red")),
        (f(b.get("spread")), Style("ink")),
        (f(b.get("depth_ask"), "{:,.0f}"), Style("blue")),
        (MISSING, FAINT), (MISSING, FAINT), (MISSING, FAINT),
        (MISSING, FAINT), (MISSING, FAINT),
        (snap["book_status"], state_fg(snap["book_status"])),
    ]
    dn = [
        ("DOWN", Style("red", bold=True)),
        (f(down_book.get("bid")), Style("green")),
        (f(down_book.get("ask")), Style("red")),
        (f(down_book.get("spread")), Style("ink")),
        (f(down_book.get("depth_ask"), "{:,.0f}"), Style("blue")),
        (MISSING, FAINT), (MISSING, FAINT),
        (MISSING, FAINT), (MISSING, FAINT), (MISSING, FAINT),
        (snap["book_status"], state_fg(snap["book_status"])),
    ]
    up = [up[i] for i in idx]
    dn = [dn[i] for i in idx]

    body = table(hdr, cw, [up, dn], w, max_rows=2)
    body.append([(g.h * w, RULE)])
    body.append(meter("FEED", 1.0 if snap["spot_status"] == "OK" else
                      (0.5 if snap["spot_status"] == "STALE" else 0.0), w, g))
    body.append(meter("BOOK", 1.0 if snap["book_status"] == "OK" else
                      (0.5 if snap["book_status"] == "STALE" else 0.0), w, g))
    body.append(meter("LOOP", 1.0 if snap["loop_status"] == "OK" else
                      (0.5 if snap["loop_status"] == "STALE" else 0.0), w, g))
    body.append([(fit(f" token {trunc(str(snap['book_token'] or '--'), max(6, w - 8))}", w, "<"), FAINT)])
    return panel("MARKET MATRIX", body, cols, rows, g,
                 right_note="UP + DOWN live books")


def _equity(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    curve = snap["stake_curve"]
    body: list[Row] = []
    h = max(2, rows - 5)
    body += sparkline(curve, w, h, g, baseline=0.0)
    body.append([(g.h * w, RULE)])
    body.append(kv("CUM STAKE SENT", f"${snap['staked']:,.2f}", w, Style("blue", bold=True)))
    acct = snap.get("accounting") or {}
    pnl = _finite(acct.get("total_pnl", acct.get("equity_pnl")))
    if pnl is not None:
        body.append(kv("TOTAL PNL", f"${pnl:+,.4f}", w, pnl_style(pnl)))
    else:
        # Same rule as the cash panel: report what has settled rather than
        # blanking the row for the minutes a resolution takes to publish.
        realized = _finite(acct.get("realized_pnl"))
        body.append(kv("REALIZED PNL",
                       f"${realized:+,.4f}" if realized is not None else MISSING,
                       w, pnl_style(realized)))

    return panel("STAKE / PNL", body, cols, rows, g,
                 right_note="paper mark-to-bid" if snap["mode"] == "PAPER" else "stake")


def _dist(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    trades = snap["trades"]
    ups = sum(1 for t in trades if str(t.get("side")).upper() == "UP")
    dns = sum(1 for t in trades if str(t.get("side")).upper() == "DOWN")
    oks = sum(1 for t in trades if _trade_success(t.get("result")))
    fails = len(trades) - oks
    decided, sent = ups + dns, oks + fails
    body = histogram([("DECIDE UP", ups), ("DECIDE DOWN", dns),
                      ("SENT OK", oks), ("SENT FAIL", fails)], w, 4, g,
                     totals=[decided, decided, sent, sent])
    body.append([(g.h * w, RULE)])
    pb = sum(1 for t in trades if t.get("price_side") and t.get("price_side") == t.get("book_side"))
    body.append(kv("PRICE=BOOK AGREE", f"{pb}/{len(trades)}" if trades else MISSING, w,
                   Style("ink") if trades else FAINT))
    body.append(kv("CHAINLINK UP SHARE",
                   f"{sum(1 for t in trades if t.get('chainlink_side') == 'UP')}/{len(trades)}"
                   if trades else MISSING, w, Style("amber") if trades else FAINT))
    mom_dist = _up_down_share(trades, "price_side")
    body.append(kv("MOMENTUM DIST", mom_dist or MISSING, w,
                   Style("ink") if mom_dist else FAINT))
    return panel("SIGNAL DISTRIBUTION", body, cols, rows, g, right_note="this round")


def _trades(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    all_hdr = ["TIME", "SIDE", "COST", "P", "B", "C", "RESULT", "PNL"]
    all_cw = [8, 5, 6, 3, 3, 3, 6, 4]
    keep = list(all_hdr)
    for d in ("PNL", "C", "B", "P", "COST"):
        if sum(all_cw[all_hdr.index(h)] + 1 for h in keep) <= w:
            break
        keep.remove(d)
    hdr = keep
    cw = [all_cw[all_hdr.index(h)] for h in keep]
    idx = [all_hdr.index(h) for h in keep]
    rows_data = []
    for t in reversed(snap["trades"][-40:]):
        ok = _trade_success(t.get("result"))
        cells = [
            (str(t.get("time_et", ""))[-11:-3] or "--", DIM),
            (str(t.get("side", "--")), Style("green" if t.get("side") == "UP" else "red", bold=True)),
            (f"${(_finite(t.get('amount')) or 0.0):.2f}", Style("ink")),
            (str(t.get("price_side") or "-")[:2], FAINT),
            (str(t.get("book_side") or "-")[:2], FAINT),
            (str(t.get("chainlink_side") or "-")[:2], FAINT),
            (("FILL" if snap["mode"] == "PAPER" else "SENT") if ok else "REJECT",
             Style("green" if ok else "red", bold=True)),
            (MISSING, FAINT),
        ]
        rows_data.append([cells[i] for i in idx])
    body = table(hdr, cw, rows_data, w, max_rows=max(1, rows - 3))
    return panel("RECENT TRADES", body, cols, rows, g,
                 right_note="this round | history: paper_orders.jsonl"
                 if snap["mode"] == "PAPER" else "this round | history: ledger")


def _round_book(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    """Combined UP+DOWN inventory, live mark-to-bid PnL, and a compact stop line.

    Replaces the old EXITS / STOP table: matched pairs lock $1.00 at
    settlement, leftover shares stay directional, and equity is withheld
    when a bid is missing rather than invented as flat.
    """
    w = cols - 2
    tokens = snap.get("tokens") or {}
    book = _current_round_book(snap)
    up_leg, down_leg = _legs_by_side(book, tokens)
    live = _finite(book.get("live_pnl") if book else None)
    live_txt = _fmt_pnl(live)
    pending = int((book or {}).get("unmarkable_legs") or 0)
    pair = _finite(book.get("pair_entry_with_fees") if book else None)
    if pair is None:
        pair = _finite(book.get("pair_entry") if book else None)
    pair_mark = _finite(book.get("pair_mark") if book else None)
    matched = _finite(book.get("matched_shares") if book else None)
    locked = _finite(book.get("locked_pnl") if book else None)
    leftover = _finite(book.get("leftover_shares") if book else None)
    leftover_pnl = _fmt_pnl(book.get("leftover_pnl") if book else None)
    leftover_id = str((book or {}).get("leftover_token_id") or "")
    leftover_side = ("UP" if leftover_id and leftover_id == str(tokens.get("up_token_id") or "")
                     else "DOWN" if leftover_id and leftover_id == str(tokens.get("down_token_id") or "")
                     else None)

    up_sh = _finite(up_leg.get("shares") if up_leg else None) or 0.0
    down_sh = _finite(down_leg.get("shares") if down_leg else None) or 0.0
    round_cost = _finite(book.get("round_cost") if book else None)
    labeled = up_leg is not None or down_leg is not None
    if_up = (up_sh - round_cost) if book and round_cost is not None and labeled else None
    if_dn = (down_sh - round_cost) if book and round_cost is not None and labeled else None

    body: list[Row] = []
    body.append(kv("LIVE PNL",
                   live_txt if live_txt is not None else
                   (f"{MISSING} {pending} unmarkable" if pending else MISSING),
                   w, pnl_style(live) if live_txt is not None else FAINT))
    body.append(kv(L("UP SH/COST", "UP SHARES / COST", s),
                   _sh_cost(up_leg) or MISSING, w,
                   Style("green", bold=True) if up_leg else FAINT))
    body.append(kv(L("DN SH/COST", "DN SHARES / COST", s),
                   _sh_cost(down_leg) or MISSING, w,
                   Style("red", bold=True) if down_leg else FAINT))
    if pair is not None and matched is not None and matched > 0:
        lock_txt = _fmt_pnl(locked) or MISSING
        mark_txt = f"{pair_mark:.3f}" if pair_mark is not None else MISSING
        body.append(kv("PAIR",
                       f"{matched:.2f}sh paid {pair:.3f} mark {mark_txt} lock {lock_txt}",
                       w, pnl_style(locked)))
    else:
        body.append(kv("PAIR", MISSING, w, FAINT))
    if leftover is not None and leftover > 1e-9:
        body.append(kv("LEFT",
                       f"{leftover:.2f} {leftover_side or 'sh'}  {leftover_pnl or MISSING}",
                       w, pnl_style(_finite(book.get("leftover_pnl") if book else None))))
    else:
        body.append(kv("LEFT", MISSING, w, FAINT))
    if_txt = None
    if if_up is not None and if_dn is not None:
        if_txt = f"{_fmt_pnl(if_up)} / {_fmt_pnl(if_dn)}"
    body.append(kv("IF UP / IF DN", if_txt or MISSING, w,
                   Style("ink") if if_txt else FAINT))
    trim = snap.get("late_trim") or {}
    hole = _finite(trim.get("hole"))
    if hole is None and if_up is not None and if_dn is not None:
        if if_up < 0 <= if_dn:
            hole = -if_up
        elif if_dn < 0 <= if_up:
            hole = -if_dn
    body.append(kv("HOLE",
                   f"${hole:,.2f} if {trim.get('side') or 'fav'}"
                   if hole is not None and hole > 0 else
                   (MISSING if hole is None else "none"),
                   w, Style("red", bold=True) if hole and hole > 0 else FAINT))
    if not trim.get("enabled"):
        trim_txt, tstyle = "off", FAINT
    elif trim.get("action") == "buy":
        trim_txt = (
            f"{trim.get('clips', 0)}/{trim.get('max_clips', 2)} "
            f"{trim.get('side') or '--'} "
            f"${_finite(trim.get('amount')) or 0:.2f} "
            f"@{(_finite(trim.get('ask')) or 0):.2f}"
        )
        tstyle = Style("amber", bold=True)
    else:
        trim_txt = str(trim.get("reason") or "idle")
        tstyle = FAINT
    body.append(kv("TRIM", trim_txt, w, tstyle))

    stat = snap.get("stop_status") or {}
    if not stat.get("enabled"):
        body.append(kv("STOP", "off", w, FAINT))
        note = "off"
    else:
        armed = bool(stat.get("armed"))
        trig = _finite(stat.get("trigger"))
        floor = _finite(stat.get("floor"))
        stop_txt = "ARMED" if armed else "waiting"
        if trig is not None:
            stop_txt += f"  trig {trig:.2f}"
        if floor is not None:
            stop_txt += f"  floor {floor:.2f}"
        body.append(kv("STOP", stop_txt, w,
                       Style("green", bold=True) if armed else FAINT))
        note = (f"T{int(_finite(stat.get('arm')) or 0)}-"
                f"{int(_finite(stat.get('cutoff')) or 0)}")
    cond = (snap.get("tokens") or {}).get("condition_id")
    this_round = bool(book and cond and book.get("condition_id") == cond)
    if book and not this_round and cond:
        note = "prior round open"
    elif this_round:
        note = "this round" if note == "off" else note
    return panel("ROUND BOOK / PNL", body, cols, rows, g, right_note=note)


def _events(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    lv = {"good": Style("green"), "bad": Style("red", bold=True),
          "warn": Style("amber"), "info": Style("ink")}
    body: list[Row] = []
    n = max(1, rows - 2)
    for e in list(snap["events"])[-n:]:
        ts = time.strftime("%H:%M:%S", time.localtime(e.wall))
        tag = fit(e.tag[:6], 6, "<")
        rep = f" x{e.repeat}" if e.repeat > 1 else ""
        head_w = 9 + 7
        body.append(pad([
            (ts + " ", DIM), (tag + " ", Style("blue", bold=True)),
            (fit(e.text + rep, max(1, w - head_w), "<"), lv.get(e.level, PAPER)),
        ], w))
    return panel("SYSTEM / EVENT FEED", body, cols, rows, g, right_note="stdout captured")


def _footer(snap, cols: int, g: Glyphs, s: Sizing) -> Row:
    up = snap["uptime"]
    left = [
        (" q ", Style("white", "ink", bold=True)), (" quit  ", DIM),
        (" r ", Style("white", "ink", bold=True)), (" repaint  ", DIM),
        (f"up {int(up // 3600):02d}:{int(up % 3600 // 60):02d}:{int(up % 60):02d}  ", DIM),
        (f"frames {snap['frames']}  ", DIM),
    ]
    right = [(" -- = no source in this build; never fabricated ", Style("purple", bold=True))]
    lw = sum(len(t) for t, _ in left)
    rw = sum(len(t) for t, _ in right)
    mid = cols - lw - rw
    if mid < 1:
        return pad(left, cols)
    return pad(left + [(" " * mid, PAPER)] + right, cols)


def _overlay(frame: list[Row], snap, cols: int, rows: int, g: Glyphs) -> None:
    """Transient centred notification, drawn into the frame.

    It is part of the frame, so the diff renderer handles its appearance and
    disappearance like any other change - nothing blocks and nothing is
    redrawn wholesale.
    """
    ov = snap["overlay"]
    if ov is None or not ov.alive(snap["mono"]):
        return
    inten = ov.intensity(snap["mono"])
    hue = {"good": "green", "bad": "red", "info": "blue"}.get(ov.level, "blue")
    if inten >= 0.35:                       # glow phase
        box = Style("white", hue, bold=True)
        edge = Style("white", hue)
    else:                                   # fade phase
        box = Style(hue, "paper2", bold=True)
        edge = Style(hue, "paper2")

    box_w = min(cols - 4, max(30, len(ov.big) + 12, len(ov.sub) + 12))
    x = max(0, (cols - box_w) // 2)
    y = max(0, rows // 2 - 3)
    inner = box_w - 2
    lines = [
        ([(g.tl + g.h * inner + g.tr, edge)]),
        ([(g.v, edge), (fit(ov.big, inner, "^"), box), (g.v, edge)]),
        ([(g.v, edge), (fit(ov.sub, inner, "^"), box), (g.v, edge)]),
        ([(g.bl + g.h * inner + g.br, edge)]),
    ]
    for i, seg in enumerate(lines):
        yy = y + i
        if not (0 <= yy < rows):
            continue
        flat = "".join(t for t, _ in pad(frame[yy], cols))
        frame[yy] = pad([(flat[:x], Style("faint"))] + seg +
                        [(flat[x + box_w:], Style("faint"))], cols)


# ------------------------------------------------------------------- build ---
def build(snap: dict, cols: int, rows: int, g: Glyphs) -> list[Row]:
    cols = max(20, cols)
    rows = max(4, rows)
    s = Sizing(cols, rows)
    frame: list[Row] = [_header(snap, cols, g, s), _health(snap, cols, g, s)]

    # ---- top band ----
    if s.top > 0:
        if s.stack:
            parts = [_kpi(snap, cols, s.top, g, s)]
            widths = [cols]
        elif not s.show_chart:
            w1, w2 = hsplit(cols, [0.50, 0.50], [36, 30])
            parts = [_kpi(snap, w1, s.top, g, s), _round_panel(snap, w2, s.top, g, s)]
            widths = [w1, w2]
        else:
            w1, w2, w3 = hsplit(cols, [0.34, 0.26, 0.40], [38, 30, 30])
            parts = [_kpi(snap, w1, s.top, g, s),
                     _round_panel(snap, w2, s.top, g, s),
                     _chart(snap, w3, s.top, g, s)]
            widths = [w1, w2, w3]
        frame += join(parts, widths, s.top)

    # ---- status strip ----
    frame += _status_strip(snap, cols, s.status_rows, g, s)

    # ---- mid band ----
    if s.show_mid:
        if s.stack:
            frame += _pipeline(snap, cols, s.mid, g, s)
        else:
            w1, w2 = hsplit(cols, [0.52, 0.48], [40, 34])
            left = _pipeline(snap, w1, s.mid, g, s)
            h1 = max(6, s.mid // 2)
            right = _matrix(snap, w2, h1, g, s) + _equity(snap, w2, s.mid - h1, g, s)
            frame += join([left, right], [w1, w2], s.mid)

    # ---- bottom band ----
    if s.show_bot:
        if s.stack:
            frame += _events(snap, cols, s.bot, g, s)
        elif not s.show_dist:
            w1, w2 = hsplit(cols, [0.5, 0.5], [30, 30])
            frame += join([_trades(snap, w1, s.bot, g, s), _events(snap, w2, s.bot, g, s)],
                          [w1, w2], s.bot)
        elif cols >= 120:
            w1, w2, w3, w4 = hsplit(cols, [0.20, 0.26, 0.26, 0.28],
                                    [26, 28, 28, 28])
            frame += join([_dist(snap, w1, s.bot, g, s),
                           _trades(snap, w2, s.bot, g, s),
                           _round_book(snap, w3, s.bot, g, s),
                           _events(snap, w4, s.bot, g, s)],
                          [w1, w2, w3, w4], s.bot)
        else:
            # Narrower than four columns: the round-book panel displaces the
            # P&L histogram rather than the trade or event feeds. Combined
            # inventory has to be visible while the round is still open.
            w1, w2, w3 = hsplit(cols, [0.30, 0.30, 0.40], [28, 28, 30])
            frame += join([_trades(snap, w1, s.bot, g, s),
                           _round_book(snap, w2, s.bot, g, s),
                           _events(snap, w3, s.bot, g, s)], [w1, w2, w3], s.bot)

    frame.append(_footer(snap, cols, g, s))

    # exact geometry, always
    while len(frame) < rows:
        frame.append(blank(cols))
    frame = [pad(r, cols) for r in frame[:rows]]
    _overlay(frame, snap, cols, rows, g)
    return frame
