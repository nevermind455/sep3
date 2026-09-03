"""Resolve recorded journal rounds. Replaces `signal_journal.py resolve`.

Why this file exists
--------------------
`signal_journal.resolve()` asks `market_discovery.get_btc_5m_tokens(window)`
for the tokens of a PAST round. That function refuses any window before the
current one on purpose - it is the H6 fix, the guard that stops the trading
path buying an already-resolving market - so it returns None every time and
`resolve` writes an empty winners file while printing "0 resolved, N still
pending". It never resolves anything, however long you wait.

The guard is right for the order path and wrong for a read-only resolver.
This does the same job by fetching the round's slug directly. It cannot place
an order, so there is nothing for the guard to protect.

    python3 journal_resolve.py            # resolve everything closed
    python3 journal_resolve.py --dry-run  # show what it would fetch

Writes the same file `signal_journal.py analyze` reads.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

import market_discovery                       # noqa: E402
import timer                                  # noqa: E402
from accounting import resolution as res      # noqa: E402

JOURNAL = ROOT / "signal_journal.csv"
WINNERS = ROOT / "signal_journal_winners.json"
WINDOW_SECONDS = 300
# Resolution is not instant. Anything younger than this is simply not ready.
SETTLE_GRACE_SECONDS = 300


def tokens_for_past_window(window: int):
    """The round's tokens, fetched by slug, with the window verified.

    market_discovery._parse_event cannot be reused here. It requires
    `not closed`, `active`, `acceptingOrders` and `enableOrderBook` - the
    flags that say "you may place an order right now". A round that has
    settled fails all four, so the live parser rejects exactly the rounds a
    resolver needs. That is the second half of the same bug.

    Every check that establishes IDENTITY is kept: the slug must match, the
    market's own start/end timestamps must match the window to the second,
    the outcome labels must be exactly up/down, the ids must be well formed,
    and more than one candidate is refused rather than picked by order. Only
    the tradeability flags are dropped, and this file cannot place an order.
    """
    window = int(window)
    expected_slug = f"btc-updown-5m-{window}"
    event = market_discovery._fetch_slug(expected_slug)
    if not isinstance(event, dict) or event.get("slug") != expected_slug:
        return None

    matches = []
    for market in market_discovery._as_list(event.get("markets")):
        if not isinstance(market, dict):
            continue
        if not market_discovery._valid_window(market, window):
            continue
        token_ids = [str(t).strip()
                     for t in market_discovery._as_list(market.get("clobTokenIds"))]
        outcomes = market_discovery._as_list(market.get("outcomes"))
        if len(token_ids) != 2 or len(outcomes) != 2:
            continue
        labels = [str(label).strip().lower() for label in outcomes]
        if sorted(labels) != ["down", "up"]:
            continue
        if token_ids[0] == token_ids[1]:
            continue
        if any(not market_discovery.TOKEN_RE.fullmatch(t) or int(t) <= 0
               for t in token_ids):
            continue
        condition = str(market.get("conditionId") or market.get("condition_id") or "")
        if not market_discovery.CONDITION_RE.fullmatch(condition):
            continue
        labelled = dict(zip(labels, token_ids))
        matches.append({
            "up_token_id": labelled["up"],
            "down_token_id": labelled["down"],
            "condition_id": condition,
            "window_start": window,
        })
    return matches[0] if len(matches) == 1 else None


def journal_windows():
    if not JOURNAL.exists():
        raise SystemExit(f"no journal at {JOURNAL} - run `signal_journal.py record` first")
    with JOURNAL.open(encoding="utf-8") as fh:
        return sorted({int(row["window"]) for row in csv.DictReader(fh) if row.get("window")})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    winners = json.loads(WINNERS.read_text()) if WINNERS.exists() else {}
    todo = [w for w in journal_windows() if str(w) not in winners]
    now = timer.unix()

    too_young = [w for w in todo if now - (w + WINDOW_SECONDS) < SETTLE_GRACE_SECONDS]
    ready = [w for w in todo if w not in set(too_young)]

    print(f"journal holds {len(journal_windows())} rounds, "
          f"{len(winners)} already resolved, {len(ready)} ready, "
          f"{len(too_young)} too young")
    if args.dry_run:
        for w in ready[:20]:
            print(f"  would fetch btc-updown-5m-{w}")
        if len(ready) > 20:
            print(f"  ... and {len(ready) - 20} more")
        return 0

    resolved = unresolved = missing = 0
    for window in ready:
        tokens = tokens_for_past_window(window)
        if not tokens:
            missing += 1
            continue
        outcome = res.fetch(tokens["condition_id"])
        if not outcome.resolved:
            unresolved += 1
            continue
        up = outcome.payout(tokens["up_token_id"])
        if up == 1.0:
            winners[str(window)] = "UP"
        elif up == 0.0:
            winners[str(window)] = "DOWN"
        else:
            # A market that paid neither 1 nor 0 is a split or a bad parse.
            # Recording it as a winner would corrupt every accuracy number
            # downstream, so it is named rather than guessed.
            winners[str(window)] = "SPLIT"
        resolved += 1
        print(f"  {window} -> {winners[str(window)]}")

    tmp = WINNERS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(winners, indent=1), encoding="utf-8")
    tmp.replace(WINNERS)

    print(f"\nresolved {resolved} this run, {len(winners)} total in {WINNERS.name}")
    if unresolved:
        print(f"  {unresolved} closed but not yet resolved on-chain - retry later")
    if missing:
        print(f"  {missing} could not be found on Gamma at all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
