#!/usr/bin/env python3
"""Strategy PnL report, read from the paper ledger. Read-only.

    python analyze_pnl.py                      # everything in the ledger
    python analyze_pnl.py --since "2026-08-16 00:30"
    python analyze_pnl.py --since 1786829936   # unix seconds

Nothing here imports the trading loop or writes to any bot file; it opens
`paper_ledger.json`, `paper_orders.jsonl` and `paper_trade_log.csv` for
reading only, so it is safe to run while the bot is trading.

The one number to read first is the EDGE TEST. A strategy with no skill wins
at roughly the price it pays and loses only the fee. Winning *below* the price
paid means the signal is worse than the market's own quote, and no parameter
setting fixes that.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import statistics
import sys
from collections import defaultdict
from datetime import datetime

ROOT = pathlib.Path(__file__).parent

# This tool does not import config, so nothing else loads .env for it. Without
# this the env lookups below silently see nothing and fall back to the default
# names - which is how a redirected profile reported "no ledger" while its real
# one sat in the same directory. override=False keeps a real environment
# variable authoritative over the file, matching config.py.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False, encoding="utf-8")
except Exception:
    pass


def _state_path(env_name: str, default_name: str) -> pathlib.Path:
    """Resolve a paper artifact the same way run_feeds.py does.

    These were hardcoded, so a run that redirected its ledger through
    PAPER_LEDGER_PATH could not be analysed at all - the report simply said
    "no ledger at .../paper_ledger.json" while the real one sat beside it
    under a different name. Reading the same environment variables as the bot
    keeps the report pointed at whatever profile actually ran.
    """
    raw = os.environ.get(env_name) or default_name
    candidate = pathlib.Path(raw)
    return candidate if candidate.is_absolute() else ROOT / candidate


LEDGER = _state_path("PAPER_LEDGER_PATH", "paper_ledger.json")
ACCOUNT = _state_path("PAPER_ACCOUNT_PATH", "paper_account.json")
ORDERS = _state_path("PAPER_AUDIT_PATH", "paper_orders.jsonl")
TRADES = _state_path("PAPER_TRADE_LOG_PATH", "paper_trade_log.csv")


def parse_since(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    raise SystemExit(f"could not read --since {raw!r}; use unix seconds or 'YYYY-MM-DD HH:MM'")


def load_fills(since: float) -> tuple[list[dict], dict]:
    """Every fill in the ledger, tagged with the outcome it settled to."""
    led = json.loads(LEDGER.read_text(encoding="utf-8"))
    fills = []
    for pos in led["positions"].values():
        payout = pos.get("payout_per_share")
        for lot in pos["lots"]:
            if lot["wall"] < since:
                continue
            shares, price, fee = lot["shares"], lot["price"], lot["fee"]
            cost = shares * price + fee
            fills.append({
                "wall": lot["wall"], "price": price, "shares": shares, "fee": fee,
                "cost": cost, "token": pos["token_id"],
                "condition": pos.get("condition_id"),
                "settled": bool(pos.get("settled")),
                "pnl": (shares * payout - cost) if pos.get("settled") else None,
                "won": (payout == 1.0) if pos.get("settled") else None,
                "secs_left": (int(lot["wall"]) // 300 + 1) * 300 - lot["wall"],
            })
    fills.sort(key=lambda f: f["wall"])
    return fills, led


def rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def account(fills: list[dict], led: dict, since: float) -> None:
    rule("ACCOUNT")
    positions = list(led["positions"].values())
    if not since:
        start = json.loads(ACCOUNT.read_text(encoding="utf-8"))
        start = start["starting_balance"]
        cost_all = sum(p["cost"] for p in positions)
        payouts = sum((p.get("payout_per_share") or 0.0) * p["shares"]
                      for p in positions if p.get("settled"))
        print(f"  starting balance        ${start:>12,.2f}")
        print(f"  cash now                ${start - cost_all + payouts:>12,.2f}")
        print(f"  open positions          ${sum(p['cost'] for p in positions if not p.get('settled')):>12,.2f}")
        print("  ---------------------------------------")

    done = [f for f in fills if f["settled"]]
    if not done:
        print("  no settled fills in range yet")
        return
    turnover = sum(f["cost"] for f in fills)
    fees = sum(f["fee"] for f in fills)
    pnl = sum(f["pnl"] for f in done)
    print(f"  realized PnL            ${pnl:>12,.2f}")
    print(f"  fees paid               ${fees:>12,.2f}   ({fees / turnover * 100:.2f}% of turnover)")
    print(f"  realized before fees    ${pnl + fees:>12,.2f}")
    print(f"  turnover                ${turnover:>12,.2f}   ({len(fills)} fills, {len(done)} settled)")

    rounds: dict = defaultdict(float)
    for f in done:
        rounds[f["condition"]] += f["pnl"]
    won = sum(1 for v in rounds.values() if v > 0)
    print(f"\n  settled rounds          {len(rounds):>13}   ({won} up / {len(rounds) - won} down)")
    print(f"  round win rate          {won / len(rounds) * 100:>12.1f}%")
    print(f"  avg PnL per round       ${statistics.mean(rounds.values()):>12,.4f}")


def edge_test(fills: list[dict]) -> None:
    done = [f for f in fills if f["settled"]]
    if not done:
        return
    rule("EDGE TEST  (the number that decides whether tuning can help)")
    # Weight both sides by SHARES. The payout is $1.00 per share, so shares are
    # the unit both numbers have to be measured in for the comparison to mean
    # anything. Taking a plain mean over fills counted a 5-share fill at 0.90
    # and a 10-share fill at 0.20 equally, and counted a position built from
    # ten fills ten times - which on a both-legged round, where one leg always
    # wins and one always loses, manufactured an edge out of fill counts. It
    # read +8.4 points on data whose share-weighted edge was negative.
    total_shares = sum(f["shares"] for f in done)
    if total_shares <= 0:
        return
    paid = sum(f["shares"] * f["price"] for f in done) / total_shares
    won = sum(f["shares"] for f in done if f["won"]) / total_shares
    turnover = sum(f["cost"] for f in done)
    fees = sum(f["fee"] for f in done)
    positions = len({(f["token"], f["settled"]) for f in done})
    print(f"  average price paid        {paid * 100:>6.1f}%   <- the market's implied odds")
    print(f"  actual win rate           {won * 100:>6.1f}%   <- what really happened")
    print(f"  edge                      {(won - paid) * 100:>+6.1f} points")
    print(f"  (share-weighted over {total_shares:,.0f} shares in {positions} positions, "
          f"{len(done)} fills)")
    print(f"  fee drag                  {-fees / turnover * 100:>+6.2f} points of turnover")
    if won < paid:
        print("\n  Losing below the price paid means the signal is worse than the")
        print("  market's own quote. That is a selection problem, not a settings problem.")


def by_price(fills: list[dict]) -> None:
    done = [f for f in fills if f["settled"]]
    if not done:
        return
    rule("BY ENTRY PRICE   (`need` = the win rate the price and fee demand)")
    print(f"{'price':>11} {'fills':>6} {'staked':>9} {'fees':>7} {'won':>8} {'need':>7} {'net PnL':>10}")
    for lo, hi in ((0, .30), (.30, .50), (.50, .70), (.70, .85), (.85, .95), (.95, .98), (.98, 1.01)):
        grp = [f for f in done if lo <= f["price"] < hi]
        if not grp:
            continue
        need = statistics.mean(f["cost"] / f["shares"] for f in grp) * 100
        print(f"{lo:>5.2f}-{hi:<5.2f} {len(grp):>6} {sum(f['cost'] for f in grp):>9,.2f} "
              f"{sum(f['fee'] for f in grp):>7.2f} "
              f"{sum(1 for f in grp if f['won']) / len(grp) * 100:>7.1f}% {need:>6.1f}% "
              f"{sum(f['pnl'] for f in grp):>+10.2f}")


def by_timing(fills: list[dict]) -> None:
    done = [f for f in fills if f["settled"]]
    if not done:
        return
    rule("BY SECONDS LEFT IN ROUND AT ENTRY")
    print(f"{'window':>14} {'fills':>6} {'avg px':>7} {'won':>7} {'net PnL':>10}")
    for lo, hi, label in ((0, 30, "0-30s left"), (30, 60, "30-60s"), (60, 120, "60-120s"),
                          (120, 240, "120-240s"), (240, 301, "240-300s")):
        grp = [f for f in done if lo <= f["secs_left"] < hi]
        if not grp:
            continue
        print(f"{label:>14} {len(grp):>6} {statistics.mean(f['price'] for f in grp):>7.3f} "
              f"{sum(1 for f in grp if f['won']) / len(grp) * 100:>6.1f}% "
              f"{sum(f['pnl'] for f in grp):>+10.2f}")


def bands(fills: list[dict]) -> None:
    done = [f for f in fills if f["settled"]]
    if len(done) < 20:
        return
    rule("PRICE BAND  (floor <= price <= cap), net PnL per $100 staked")
    caps = (.6, .7, .8, .9, .95, .99)
    for floor in (0.0, .2, .3, .4, .5):
        cells = []
        for cap in caps:
            grp = [f for f in done if floor <= f["price"] <= cap]
            if len(grp) < 5:
                cells.append("     -")
                continue
            staked = sum(f["cost"] for f in grp)
            cells.append(f"{sum(f['pnl'] for f in grp) / staked * 100:>+6.1f}")
        print(f"{floor:>5.2f} | " + " ".join(cells))
    print("floor | " + " ".join(f"{c:>6.2f}" for c in caps) + "   <- cap")


def by_signal(fills: list[dict], since: float) -> None:
    """Join filled orders to their signal row on the clock, never on sequence."""
    if not ORDERS.exists() or not TRADES.exists():
        return
    sys.path.insert(0, str(ROOT))
    try:
        import timer
    except Exception as exc:                       # pragma: no cover
        print(f"\n(signal join unavailable: {type(exc).__name__}: {exc})")
        return

    payout = {f["token"]: f for f in fills if f["settled"]}
    orders = []
    for line in ORDERS.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("status") == "FILLED" and row.get("wall", 0) >= since:
            orders.append(row)

    # Match the result exactly. A substring test is a trap here: "fill" is
    # inside "skipped_unfillable", which would pool gate-skips with real
    # fills and mis-join their signals to somebody else's order.
    filled = {"paper_filled", "matched", "accepted_pending_confirmation"}
    stamp: dict = defaultdict(list)
    with TRADES.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("result") or "").strip().lower() in filled:
                stamp[row["time_et"]].append(row)

    pairs = []
    for order in orders:
        for delta in (0, 1, 2, 3, -1, 4, 5):
            key = timer.now_et(order["wall"] + delta).strftime("%b %d %H:%M:%S ET")
            rows = stamp.get(key) or []
            hit = next((r for r in rows if r["side"] == order["side"]), None)
            if hit is not None:
                rows.remove(hit)
                pairs.append((order, hit))
                break

    if len(pairs) < 20:
        return
    mismatch = sum(1 for o, c in pairs if o["side"] != c["side"])
    if mismatch:                                    # never report a bad join
        print(f"\n(signal join rejected: {mismatch} side mismatches)")
        return

    combos: dict = defaultdict(lambda: {"n": 0, "cost": 0.0, "pnl": 0.0, "wins": 0, "px": 0.0})
    votes: dict = defaultdict(lambda: {"n": 0, "cost": 0.0, "pnl": 0.0, "wins": 0, "px": 0.0})
    graded = 0
    for order, row in pairs:
        pos = payout.get(order["token_id"])
        if pos is None:
            continue                                # not settled yet
        graded += 1
        won = pos["won"]
        pnl = order["shares"] * (1.0 if won else 0.0) - order["total_cost"]
        sides = [row["price_side"], row["book_side"], row["chainlink_side"]]
        live = [s for s in sides if s]
        agree = sum(1 for s in live if s == order["side"])
        vote_key = (f"{agree}/{len(live)} backed the side taken" if live
                    else "no signal recorded")
        for bucket, key in ((combos, tuple(s or "-" for s in sides)),
                            (votes, vote_key)):
            b = bucket[key]
            b["n"] += 1
            b["cost"] += order["total_cost"]
            b["pnl"] += pnl
            b["px"] += order["average_price"]
            b["wins"] += 1 if won else 0

    if graded < 20:
        print(f"\n(only {graded} joined fills have settled; skipping the signal tables)")
        return

    rule("BY HOW MANY SIGNALS BACKED THE SIDE TRADED")
    print(f"{'':<30}{'fills':>6}{'avg px':>8}{'won':>8}{'staked':>10}{'net PnL':>10}{'/$100':>8}")
    for key in sorted(votes):
        b = votes[key]
        print(f"{key:<30}{b['n']:>6}{b['px'] / b['n']:>8.3f}{b['wins'] / b['n'] * 100:>7.1f}%"
              f"{b['cost']:>10,.0f}{b['pnl']:>+10.2f}{b['pnl'] / b['cost'] * 100:>+8.1f}")

    rule("BY SIGNAL COMBINATION   (price / book / chainlink; '-' = abstained)")
    print(f"{'price':>6}{'book':>7}{'chain':>7}{'fills':>7}{'avg px':>8}{'won':>8}"
          f"{'staked':>10}{'net PnL':>10}{'/$100':>8}")
    for key, b in sorted(combos.items(), key=lambda kv: kv[1]["pnl"] / kv[1]["cost"]):
        if b["n"] < 8:
            continue
        print(f"{key[0]:>6}{key[1]:>7}{key[2]:>7}{b['n']:>7}{b['px'] / b['n']:>8.3f}"
              f"{b['wins'] / b['n'] * 100:>7.1f}%{b['cost']:>10,.0f}{b['pnl']:>+10.2f}"
              f"{b['pnl'] / b['cost'] * 100:>+8.1f}")
    print("\nRows under 8 fills are hidden; anything under ~100 is noise, not a finding.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="only fills at or after this time "
                                    "(unix seconds, or 'YYYY-MM-DD HH:MM')")
    args = ap.parse_args()
    since = parse_since(args.since)

    if not LEDGER.exists():
        raise SystemExit(f"no ledger at {LEDGER}")
    fills, led = load_fills(since)
    if not fills:
        raise SystemExit("no fills in range")
    if since:
        stamp = datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M:%S")
        print(f"filtered to fills at or after {stamp}")

    unsettled = sum(1 for f in fills if not f["settled"])
    if unsettled:
        print(f"note: {unsettled} fill(s) not yet settled are excluded from every PnL table")

    account(fills, led, since)
    edge_test(fills)
    by_price(fills)
    by_timing(fills)
    bands(fills)
    by_signal(fills, since)
    return 0


if __name__ == "__main__":
    sys.exit(main())
