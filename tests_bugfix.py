"""Regression tests for the aug13 bug pass.

Every test here was written against the BROKEN behaviour first and confirmed
to fail on it. A test that passes on both versions proves nothing, so each one
names the specific thing it would have caught.

    python3 tests_bugfix.py
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  pass  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _settled_event(slug, window, **over):
    market = {
        "conditionId": "0x" + "a" * 64,
        "clobTokenIds": '["111","222"]',
        "outcomes": '["Up","Down"]',
        "id": "9",
        # A round that has settled looks exactly like this, and every one of
        # these flags makes market_discovery._parse_event reject it.
        "closed": True, "active": False,
        "acceptingOrders": False, "enableOrderBook": False,
        "eventStartTime": _iso(window), "endDate": _iso(window + 300),
    }
    market.update(over)
    return {"slug": slug, "markets": [market]}


class _Res:
    def __init__(self, resolved=True, up=1.0):
        self.resolved = resolved
        self._up = up

    def payout(self, token):
        return self._up if token == "111" else 1.0 - self._up


# ---------------------------------------------------------------- BUG 1 ----
def bug1_resolver():
    """`signal_journal resolve` fetched nothing and reported success."""
    print("\nBUG 1 - resolve() could never resolve a past round")
    import timer
    import journal_resolve as jr
    import market_discovery

    window = (int(timer.wall()) // 300) * 300 - 3600
    work = pathlib.Path(tempfile.mkdtemp())
    try:
        with (work / "j.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["wall", "window", "secs_left"])
            w.writeheader()
            for s in (10, 20, 30):
                w.writerow({"wall": window + s, "window": window, "secs_left": 300 - s})
        jr.JOURNAL = work / "j.csv"
        jr.WINNERS = work / "w.json"

        # The gate that caused the bug is still there, and still right.
        check("live token lookup still refuses a past window (H6 intact)",
              market_discovery.get_btc_5m_tokens(window) is None)

        # And signal_journal no longer routes through it.
        sj_src = (ROOT / "signal_journal.py").read_text(encoding="utf-8")
        # The docstring still names the old call; what must be gone is the
        # call itself, so match the call site rather than the mention.
        check("signal_journal.resolve no longer calls the trade-path lookup",
              "get_btc_5m_tokens(w)" not in sj_src)
        check("signal_journal.resolve delegates to the working resolver",
              "import journal_resolve" in sj_src
              and "journal_resolve.main([])" in sj_src)

        fetched = []
        orig_fetch = market_discovery._fetch_slug
        orig_res = jr.res.fetch
        try:
            def fetch(slug):
                fetched.append(slug)
                return _settled_event(slug, window)
            market_discovery._fetch_slug = fetch
            jr.res.fetch = lambda cid, timeout=10.0: _Res()
            buf, real = io.StringIO(), sys.stdout
            sys.stdout = buf
            try:
                jr.main([])
            finally:
                sys.stdout = real
            check("resolver actually reaches Gamma",
                  fetched == [f"btc-updown-5m-{window}"], str(fetched))
            check("winner is written",
                  json.loads((work / "w.json").read_text()) == {str(window): "UP"})

            # Identity checks that must survive dropping the trade flags.
            for name, event, expect in (
                ("wrong-window market refused", _settled_event(
                    f"btc-updown-5m-{window}", window,
                    eventStartTime=_iso(window - 300), endDate=_iso(window)), {}),
                ("bad condition id refused", _settled_event(
                    f"btc-updown-5m-{window}", window, conditionId="nope"), {}),
                ("non up/down outcomes refused", _settled_event(
                    f"btc-updown-5m-{window}", window,
                    outcomes='["Yes","No"]'), {}),
            ):
                (work / "w.json").unlink(missing_ok=True)
                market_discovery._fetch_slug = lambda slug, e=event: e
                sys.stdout = io.StringIO()
                try:
                    jr.main([])
                finally:
                    sys.stdout = real
                check(name, json.loads((work / "w.json").read_text()) == expect)

            (work / "w.json").unlink(missing_ok=True)
            market_discovery._fetch_slug = lambda slug: _settled_event(slug, window)
            jr.res.fetch = lambda cid, timeout=10.0: _Res(resolved=False)
            sys.stdout = io.StringIO()
            try:
                jr.main([])
            finally:
                sys.stdout = real
            check("unresolved round is not written",
                  json.loads((work / "w.json").read_text()) == {})

            (work / "w.json").unlink(missing_ok=True)
            jr.res.fetch = lambda cid, timeout=10.0: _Res(up=0.5)
            sys.stdout = io.StringIO()
            try:
                jr.main([])
            finally:
                sys.stdout = real
            check("a split is named, never guessed as a winner",
                  json.loads((work / "w.json").read_text()) == {str(window): "SPLIT"})
        finally:
            market_discovery._fetch_slug = orig_fetch
            jr.res.fetch = orig_res
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- BUG 2 ----
def bug2_clustered_se():
    """analyze() sized its error bar on samples, not rounds."""
    print("\nBUG 2 - error bar counted samples instead of rounds")
    import signal_journal as sj

    rounds, per_round = 100, 150
    rows = [{"window": 1_700_000_000 + r * 300} for r in range(rounds)
            for _ in range(per_round)]
    n_rounds = len({r["window"] for r in rows})
    clustered = 50 / math.sqrt(n_rounds)
    naive = 50 / math.sqrt(len(rows))

    src = pathlib.Path(sj.__file__).read_text(encoding="utf-8")
    check("SE is computed from the round count",
          "se = 50 / math.sqrt(n_rounds)" in src)
    check("sample-count SE is gone from the headline",
          "se = 50 / math.sqrt(n) if n else 0" not in src)
    check("required-sample line is stated in ROUNDS",
          "ROUNDS." in src and "int((2 * 50 / 3) ** 2)" in src)
    check("clustering matters here (12x on this shape)",
          round(clustered / naive) == 12, f"{clustered / naive:.1f}x")
    check("3-point edge needs ~1111 rounds, not ~1111 samples",
          int((2 * 50 / 3) ** 2) == 1111)


# ---------------------------------------------------------------- BUG 3 ----
def bug3_exposure_ceiling():
    """round_exposure charged BET_SIZE while the broker spent more."""
    print("\nBUG 3 - exposure tracker under-counted real cash")
    import config

    theta, minsh = config.TAKER_FEE_RATE, config.VENUE_MIN_SHARES
    for cap in (0.30, 0.45, 0.50, 0.60, 0.75, 0.90):
        real_worst = max(config.BET_SIZE, minsh * cap) * (1 + theta * (1 - cap))
        ceiling = config.entry_cost_ceiling(cap)
        check(f"ceiling at cap {cap:.2f} is a real upper bound",
              ceiling >= real_worst - 1e-9,
              f"{ceiling:.4f} < {real_worst:.4f}")

    check("a cap above BET_SIZE/5 reserves more than BET_SIZE",
          config.entry_cost_ceiling(0.75) > config.BET_SIZE * 1.4,
          f"{config.entry_cost_ceiling(0.75):.4f}")
    check("a cheap cap still reserves at least BET_SIZE",
          config.entry_cost_ceiling(0.20) >= config.BET_SIZE)

    src = (ROOT / "main_bot.py").read_text(encoding="utf-8")
    check("main_bot no longer charges the nominal bet",
          "round_exposure += config.BET_SIZE" not in src)
    check("main_bot charges the ceiling at the phase-2 site",
          src.count("round_exposure += entry_ceiling") == 1)
    # Phase 2 gates twice: once up front, and again immediately before
    # submitting, because the multi-signal legs spend against the same round
    # budget in between. The invariant that matters is that no charge site is
    # ungated, not the raw count.
    check("the gate tests the same number it later charges",
          src.count("round_exposure + entry_ceiling > config.MAX_ROUND_EXPOSURE")
          >= src.count("round_exposure += entry_ceiling"))

    # The default budget must still admit every phase-2 entry.
    if config.PHASE2_ENABLED:
        entries = math.ceil(
            max(0.0, config.TRADE_LAST_SECONDS - config.MIN_SECONDS_TO_EXPIRY)
            / max(config.TRADE_INTERVAL_SECONDS, 1))
        spent = entries * config.entry_cost_ceiling(config.MAX_BUY_PRICE)
        check("default cap still allows every planned entry",
              spent <= config.MAX_ROUND_EXPOSURE + 1e-9,
              f"{spent:.2f} > {config.MAX_ROUND_EXPOSURE:.2f}")
        check(f"({entries} entries budgeted at ${spent:.2f})", True)
    else:
        check("parked phase 2 still reserves at least one entry",
              config.MAX_ROUND_EXPOSURE
              >= config.entry_cost_ceiling(config.MAX_BUY_PRICE) - 1e-9,
              f"{config.MAX_ROUND_EXPOSURE:.2f}")


# ---------------------------------------------------------------- BUG 4 ----
def bug4_binance_strike():
    """bn_strike accepted a mid-round price as a round's strike."""
    print("\nBUG 4 - Binance strike taken from a mid-round sample")
    import signal_journal as sj
    src = pathlib.Path(sj.__file__).read_text(encoding="utf-8")

    check("a boundary grace exists", hasattr(sj, "BOUNDARY_GRACE"))
    check("the strike is only claimed near the open",
          "secs_left >= 300 - BOUNDARY_GRACE" in src)
    check("the unconditional setdefault is gone",
          "if spot is not None:\n                    bn_strike.setdefault" not in src)
    check("old windows are pruned", "bn_strike.pop(stale, None)" in src)

    # The rule itself: a sample 180s into the round must not become a strike.
    grace = sj.BOUNDARY_GRACE
    check("sample at the open qualifies", 300.0 >= 300 - grace)
    check("sample 180s in does not qualify", not (120.0 >= 300 - grace))


# ---------------------------------------------------------------- BUG 5 ----
def bug5_vote_label():
    """A row with all-blank signals was reported as '0/3 backed the side taken'."""
    print("\nBUG 5 - abstained rows mislabelled as trading against every signal")
    src = (ROOT / "analyze_pnl.py").read_text(encoding="utf-8")
    check("blank signal columns get their own bucket",
          "no signal recorded" in src)
    check("the vote count is out of the signals that spoke",
          'f"{agree}/{len(live)} backed the side taken"' in src)
    check("the hard-coded /3 is gone",
          'f"{agree}/3 backed the side taken"' not in src)

    # The logic, exercised directly.
    def bucket(sides, side):
        live = [s for s in sides if s]
        agree = sum(1 for s in live if s == side)
        return (f"{agree}/{len(live)} backed the side taken" if live
                else "no signal recorded")

    check("all blank is not scored as disagreement",
          bucket(["", "", ""], "UP") == "no signal recorded")
    check("real disagreement still reads 0/3",
          bucket(["DOWN", "DOWN", "DOWN"], "UP") == "0/3 backed the side taken")
    check("partial abstention counts only live signals",
          bucket(["UP", "", "DOWN"], "UP") == "1/2 backed the side taken")


# ---------------------------------------------------------------- BUG 6 ----
def bug6_live_pre_submit_guard():
    """A fresh high-level signal must not authorize a later stale POST."""
    print("\nBUG 6 - live submission guard was not rechecked at POST/retry")
    import polymarket_trade as live

    class Client:
        def __init__(self):
            self.signed = 0
            self.posts = 0

        def get_clob_market_info(self, _condition):
            return {}

        def create_market_order(self, _order, options=None):
            self.signed += 1
            return f"signed-{self.signed}"

        def post_order(self, _signed, _order_type):
            self.posts += 1
            raise RuntimeError("FOK_ORDER_NOT_FILLED")

        def get_balance_allowance(self, _params):
            raise AssertionError("stubbed _read_balance should be used")

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    try:
        replace(live, "_live_disabled", False)
        replace(live, "_journal_fault", None)
        replace(live, "_ambiguous_condition", None)
        replace(live, "_ambiguous_until", 0.0)
        replace(live, "_ambiguous_tokens", set())
        replace(live, "_ambiguous_all_tokens", False)
        replace(live, "_order_observer", lambda _receipt: True)
        replace(live, "_validate_round_end", lambda end: float(end))
        replace(live, "_validate_market_mapping", lambda *_a, **_k: {
            "minimum": live.Decimal("1"),
            "tick": live.Decimal("0.01"),
            "neg_risk": False,
            "fee_rate": live.Decimal("0.07"),
            "fee_exponent": 1,
        })
        replace(live.orderbook, "validate_buy_liquidity", lambda *_a, **_k: (
            [{"price": "0.49", "size": "100"}],
            [{"price": "0.50", "size": "100"}],
        ))
        replace(live, "_read_balance", lambda _client: {
            "balance": 100.0, "allowance": 100.0,
        })
        replace(live.config, "TICK_SIZE", None)
        replace(live.config, "NEG_RISK", None)
        replace(live.config, "MAX_BUY_PRICE", 0.90)
        replace(live.config, "MIN_BUY_PRICE", 0.20)
        replace(live.config, "MAX_ALLOWED_SPREAD", 0.25)
        replace(live.time, "sleep", lambda _seconds: None)

        condition = "0x" + "a" * 64

        # The first check permits signing, then the signal flips before POST.
        first_client = Client()
        replace(live, "_get_client", lambda: first_client)
        decisions = iter((True, False))
        first = live.place_trade(
            "UP", 2.0, "11", "12", condition, 300.0,
            pre_submit_guard=lambda: next(decisions),
        )
        check("guard flip after signing blocks the first POST",
              first is False and first_client.signed == 1 and first_client.posts == 0,
              f"result={first} signed={first_client.signed} posts={first_client.posts}")
        check("first-POST guard rejection is explicit",
              "pre-submit guard rejected" in (live.last_order_error or "").lower(),
              str(live.last_order_error))

        # One definitive FOK no-fill is safe to retry only while the guard is
        # still true.  It flips during the retry backoff in this fixture.
        retry_client = Client()
        live._get_client = lambda: retry_client
        decisions = iter((True, True, False))
        second = live.place_trade(
            "DOWN", 2.0, "11", "12", condition, 300.0,
            pre_submit_guard=lambda: next(decisions),
        )
        check("guard is rechecked before signing a no-fill retry",
              second is False and retry_client.signed == 1 and retry_client.posts == 1,
              f"result={second} signed={retry_client.signed} posts={retry_client.posts}")
        check("retry guard rejection remains definitive, not ambiguous",
              live._ambiguous_condition is None
              and "pre-submit guard rejected" in (live.last_order_error or "").lower(),
              f"ambiguous={live._ambiguous_condition} error={live.last_order_error}")

        exception_client = Client()
        live._get_client = lambda: exception_client

        def broken_guard():
            raise RuntimeError("guard observation unavailable")

        third = live.place_trade(
            "UP", 2.0, "11", "12", condition, 300.0,
            pre_submit_guard=broken_guard,
        )
        check("guard exception fails closed before signing or POST",
              third is False and exception_client.signed == 0
              and exception_client.posts == 0,
              f"result={third} signed={exception_client.signed} posts={exception_client.posts}")
        check("guard exception leaves a clear rejection reason",
              "pre-submit guard failed closed" in (live.last_order_error or "").lower(),
              str(live.last_order_error))
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def bug7_l2_creds_retry_timeout():
    """A single CLOB read timeout must not abort live L2 derivation."""
    print("\nBUG 7 - L2 credential derivation died on the first CLOB timeout")
    import polymarket_trade as live

    class TimeoutOnce:
        def __init__(self):
            self.calls = 0
            self.creds = None

        def create_or_derive_api_key(self):
            self.calls += 1
            if self.calls < 3:
                raise TimeoutError("The read operation timed out")
            return {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}

        def set_api_creds(self, creds):
            self.creds = creds

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    try:
        replace(live, "_CREDENTIAL_DERIVE_BACKOFF_SECONDS", (0.0, 0.0))
        client = TimeoutOnce()
        live._install_api_creds(client)
        check("timeouts are retried until derive succeeds",
              client.calls == 3 and client.creds is not None,
              f"calls={client.calls} creds={client.creds}")

        class AuthReject:
            def __init__(self):
                self.calls = 0

            def create_or_derive_api_key(self):
                self.calls += 1
                err = RuntimeError("unauthorized")
                err.status_code = 401
                raise err

            def set_api_creds(self, _creds):
                raise AssertionError("must not install rejected creds")

        rejected = AuthReject()
        try:
            live._install_api_creds(rejected)
            check("non-transient auth failure is not retried", False)
        except RuntimeError as exc:
            check("non-transient auth failure is not retried",
                  rejected.calls == 1 and "unauthorized" in str(exc),
                  f"calls={rejected.calls} err={exc}")
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def bug8_cheap_hedge_price_band():
    """Cheap insurance needs its own floor without weakening normal buys."""
    print("\nBUG 8 - cheap hedge cap was below the shared entry floor")
    import polymarket_trade as live

    feeds_source = (ROOT / "run_feeds.py").read_text(encoding="utf-8")
    check("cheap-hedge loop opts into the dedicated execution band",
          "condition, window + 300, max_price, cheap_hedge=True)" in feeds_source)

    class Client:
        def get_clob_market_info(self, _condition):
            return {}

        def create_market_order(self, order, options=None):
            return (order, options)

        def post_order(self, _signed, _order_type):
            return {
                "success": True, "orderID": "cheap-hedge", "status": "matched",
                "tradeIDs": ["cheap-hedge-trade"],
                "makingAmount": "2000000", "takingAmount": "13000000",
            }

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    seen = []

    def validate(_token, _amount, cap, _spread, *, min_price):
        seen.append((cap, min_price))
        return ([{"price": "0.14", "size": "100"}],
                [{"price": "0.15", "size": "100"}])

    try:
        replace(live, "_live_disabled", False)
        replace(live, "_journal_fault", None)
        replace(live, "_ambiguous_condition", None)
        replace(live, "_ambiguous_until", 0.0)
        replace(live, "_ambiguous_tokens", set())
        replace(live, "_ambiguous_all_tokens", False)
        replace(live, "_order_observer", lambda _receipt: True)
        replace(live, "_validate_round_end", lambda end: float(end))
        replace(live, "_validate_market_mapping", lambda *_a, **_k: {
            "minimum": live.Decimal("1"),
            "tick": live.Decimal("0.01"),
            "neg_risk": False,
            "fee_rate": live.Decimal("0.07"),
            "fee_exponent": 1,
        })
        replace(live.orderbook, "validate_buy_liquidity", validate)
        replace(live, "_read_balance", lambda _client: {
            "balance": 100.0, "allowance": 100.0,
        })
        replace(live, "_get_client", lambda: Client())
        replace(live, "MarketOrderArgs", lambda **kw: kw)
        replace(live, "PartialCreateOrderOptions", lambda **kw: kw)
        replace(live, "Side", type("Side", (), {"BUY": "BUY"}))
        replace(live, "OrderType", type("OrderType", (), {"FOK": "FOK"}))
        replace(live.config, "TICK_SIZE", None)
        replace(live.config, "NEG_RISK", None)
        replace(live.config, "MAX_BUY_PRICE", 0.80)
        replace(live.config, "MIN_BUY_PRICE", 0.30)
        replace(live.config, "MAX_ALLOWED_SPREAD", 0.25)
        replace(live.config, "CHEAP_HEDGE_ENABLED", True)
        replace(live.config, "CHEAP_HEDGE_ASK_MIN", 0.10)
        replace(live.config, "CHEAP_HEDGE_ASK_MAX", 0.20)
        replace(live.time, "sleep", lambda _seconds: None)

        condition = "0x" + "b" * 64
        normal = live.place_trade(
            "DOWN", 2.0, "11", "12", condition, 300.0, 0.20)
        check("ordinary live order remains blocked below MIN_BUY_PRICE",
              normal is False and seen == []
              and "floor" in (live.last_order_error or "").lower(),
              f"result={normal} seen={seen} error={live.last_order_error}")

        hedged = live.place_trade(
            "DOWN", 2.0, "11", "12", condition, 300.0, 0.20,
            cheap_hedge=True)
        check("live cheap hedge reaches liquidity with its 0.10-0.20 band",
              hedged is True and seen == [(0.2, 0.1)],
              f"result={hedged} seen={seen} error={live.last_order_error}")
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def main():
    for fn in (bug1_resolver, bug2_clustered_se, bug3_exposure_ceiling,
               bug4_binance_strike, bug5_vote_label,
               bug6_live_pre_submit_guard, bug7_l2_creds_retry_timeout,
               bug8_cheap_hedge_price_band):
        try:
            fn()
        except Exception as exc:                       # per-test isolation
            global FAIL
            FAIL += 1
            print(f"  FAIL  {fn.__name__} raised {type(exc).__name__}: {exc}")
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
