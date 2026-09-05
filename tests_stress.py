"""Stress + resilience tests for the book/hub feed path.

Runs offline against an in-memory `BookState`: no sockets, no gamma, no clock
sync. The goal is not to reproduce a live wire environment - it is to prove
that the read/write paths behave correctly under high message rates, bursts,
sequence gaps, malformed messages, reconnects and duplicate events.

Every check prints pass/fail with a one-line reason. The final line names the
failure count; a script wrapping this can key on `stress: PASS`.

Usage:
    python tests_stress.py [--messages 200000] [--burst 5000]
"""
from __future__ import annotations

import argparse
import gc
import os
import random
import sys
import threading
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Silence health-check spam that would otherwise be printed by feeds.hub during
# construction. We drive BookState directly, so this is precautionary.
os.environ.setdefault("BOT_TRADE_LOG_PATH", "trade_log.stress.csv")

from dashboard.metrics import LatencyRegistry, LatencyHist  # noqa: E402
from feeds.book import BookState  # noqa: E402

FAILED = 0
_START = time.monotonic()


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILED
    prefix = "  ok  " if cond else "  FAIL"
    if not cond:
        FAILED += 1
    tail = f" - {detail}" if detail else ""
    print(f"{prefix} {label}{tail}")


def section(name: str) -> None:
    print(f"\n[{time.monotonic() - _START:6.2f}s] {name}")


# --------------------------------------------------------------- fixtures ---
def _bidask(mid: float, ticks: int = 5, tick: float = 0.001):
    """A small book around a mid price. Prices in (0, 1)."""
    bids = [{"price": f"{max(0.001, mid - tick * (i + 1)):.4f}", "size": f"{10.0 + i:.2f}"}
            for i in range(ticks)]
    asks = [{"price": f"{min(0.999, mid + tick * (i + 1)):.4f}", "size": f"{10.0 + i:.2f}"}
            for i in range(ticks)]
    return bids, asks


def _fresh_book(stale_after: float = 30.0) -> BookState:
    book = BookState(stale_after=stale_after)
    book.connected = True
    book.set_active(["tok_A", "tok_B"])
    bids, asks = _bidask(0.45)
    ok = book.apply_snapshot("tok_A", bids, asks, ts_ms=int(time.time() * 1000),
                             hash_="H_A_seed0", require_exchange_ts=False)
    assert ok, "seed snapshot A must land"
    bids, asks = _bidask(0.55)
    ok = book.apply_snapshot("tok_B", bids, asks, ts_ms=int(time.time() * 1000),
                             hash_="H_B_seed0", require_exchange_ts=False)
    assert ok, "seed snapshot B must land"
    return book


# ------------------------------------------------------------ throughput ---
def stress_throughput(book: BookState, n: int) -> tuple[int, int, float]:
    """Apply n valid non-crossing deltas and measure msgs/sec + rejections.

    Bids stay well below the seed ask; asks stay well above the seed bid, so
    the crossed-book guard cannot reject them. Any rejection here is a real
    correctness signal.
    """
    rng = random.Random(1234)
    accepted = rejected = 0
    started = time.monotonic()
    ts_start = int(time.time() * 1000)
    for i in range(n):
        token = "tok_A" if (i & 1) == 0 else "tok_B"
        # Seed A: bids ~0.44-0.449, asks ~0.451-0.46. Seed B: bids ~0.54-0.549,
        # asks ~0.551-0.56. Keep bids under 0.44 and asks over 0.56 to stay off
        # either side of the seed spread.
        side = "BUY" if (i % 4) < 2 else "SELL"
        if token == "tok_A":
            price = 0.40 + (i % 40) * 0.0005 if side == "BUY" else \
                    0.56 + (i % 40) * 0.0005
        else:
            price = 0.50 + (i % 40) * 0.0005 if side == "BUY" else \
                    0.66 + (i % 40) * 0.0005
        size = 0.0 if rng.random() < 0.05 else round(rng.uniform(1.0, 50.0), 2)
        ok = book.apply_price_change(token, round(price, 4), size, side,
                                     ts_ms=ts_start + i,
                                     require_exchange_ts=False)
        if ok:
            accepted += 1
        else:
            rejected += 1
    elapsed = time.monotonic() - started
    return accepted, rejected, elapsed


# ------------------------------------------------------------- resilience ---
def stress_burst(book: BookState, size: int) -> tuple[float, float]:
    """One rapid burst of same-timestamp updates; measure latency percentiles."""
    hist = LatencyHist("apply", capacity=max(1024, size))
    ts = int(time.time() * 1000) + 1_000_000    # ahead of everything else
    for i in range(size):
        t0 = time.monotonic()
        book.apply_price_change("tok_A", round(0.45 + (i % 20) * 0.001, 4),
                                round(1.0 + i, 2), "BUY", ts_ms=ts,
                                require_exchange_ts=False)
        hist.observe((time.monotonic() - t0) * 1000.0)
    snap = hist.snapshot()
    return snap.p95 or 0.0, snap.p99 or 0.0


def stress_reader_writer(book: BookState, seconds: float = 1.0) -> tuple[int, int]:
    """Concurrent writer + reader; check that reads never see crossed books."""
    stop = threading.Event()
    reads = writes = 0
    bad_reads = [0]

    def writer():
        nonlocal writes
        rng = random.Random(7)
        ts = int(time.time() * 1000) + 2_000_000
        while not stop.is_set():
            token = "tok_A" if rng.random() < 0.5 else "tok_B"
            base = 0.45 if token == "tok_A" else 0.55
            book.apply_price_change(token,
                                    round(base + rng.uniform(-0.03, 0.03), 4),
                                    round(rng.uniform(0.0, 40.0), 2),
                                    rng.choice(("BUY", "SELL")),
                                    ts_ms=ts, require_exchange_ts=False)
            ts += 1
            writes += 1

    def reader():
        nonlocal reads
        while not stop.is_set():
            for tok in ("tok_A", "tok_B"):
                v = book.view(tok)
                if v.best_bid is not None and v.best_ask is not None:
                    if v.best_bid >= v.best_ask:
                        bad_reads[0] += 1
            reads += 1

    w = threading.Thread(target=writer, daemon=True)
    r = threading.Thread(target=reader, daemon=True)
    w.start(); r.start()
    time.sleep(seconds)
    stop.set()
    w.join(timeout=1.0); r.join(timeout=1.0)
    return writes, bad_reads[0]


def stress_reconnect(book: BookState, cycles: int = 10) -> tuple[int, int]:
    """Simulate WS drop + resnapshot. Every cycle: desync_all, then a fresh
    snapshot each token, then a delta. Deltas landing before the fresh
    snapshot must be refused."""
    refused_deltas = 0
    accepted_after_snapshot = 0
    ts = int(time.time() * 1000) + 3_000_000
    for cycle in range(cycles):
        book.desync_all(reason=f"cycle{cycle}")
        # Delta on unsynced book must be refused.
        if not book.apply_price_change("tok_A", 0.44, 5.0, "BUY", ts_ms=ts,
                                       require_exchange_ts=False):
            refused_deltas += 1
        ts += 1
        # Fresh snapshot resyncs.
        bids, asks = _bidask(0.45 + cycle * 0.0001)
        book.apply_snapshot("tok_A", bids, asks, ts_ms=ts,
                            hash_=f"H_A_c{cycle}", require_exchange_ts=False)
        bids, asks = _bidask(0.55 - cycle * 0.0001)
        book.apply_snapshot("tok_B", bids, asks, ts_ms=ts,
                            hash_=f"H_B_c{cycle}", require_exchange_ts=False)
        # Now deltas should apply again.
        if book.apply_price_change("tok_A", 0.44, 6.0, "BUY", ts_ms=ts + 1,
                                   require_exchange_ts=False):
            accepted_after_snapshot += 1
        ts += 10
    return refused_deltas, accepted_after_snapshot


def stress_malformed(book: BookState, n: int = 500) -> int:
    """Feed junk: wrong side, non-finite price, negative size, missing token."""
    rejected = 0
    ts = int(time.time() * 1000) + 4_000_000
    cases = [
        {"token": "tok_A", "price": float("nan"), "size": 1.0, "side": "BUY"},
        {"token": "tok_A", "price": float("inf"), "size": 1.0, "side": "BUY"},
        {"token": "tok_A", "price": -0.1, "size": 1.0, "side": "BUY"},
        {"token": "tok_A", "price": 1.1, "size": 1.0, "side": "BUY"},
        {"token": "tok_A", "price": 0.5, "size": -1.0, "side": "BUY"},
        {"token": "tok_A", "price": 0.5, "size": 1.0, "side": "ORANGE"},
        {"token": "no_such", "price": 0.5, "size": 1.0, "side": "BUY"},
    ]
    for i in range(n):
        c = cases[i % len(cases)]
        ok = book.apply_price_change(c["token"], c["price"], c["size"], c["side"],
                                     ts_ms=ts + i, require_exchange_ts=False)
        if not ok:
            rejected += 1
    return rejected


def stress_out_of_order(book: BookState, n: int = 200) -> tuple[int, int]:
    """Alternate fresh/stale ts; only fresh ones apply.

    Prices stay under 0.44 so they never cross the seed ask around 0.451 -
    the crossed-book guard would otherwise reject a delta whose only real
    fault is order, and hide the guard we are actually testing.
    """
    accepted = rejected = 0
    now_ts = int(time.time() * 1000) + 5_000_000
    old_ts = now_ts - 60_000
    for i in range(n):
        ts = now_ts + i if (i & 1 == 0) else old_ts
        price = round(0.40 + (i % 20) * 0.0005, 4)     # 0.4000-0.4095
        ok = book.apply_price_change("tok_A", price, 1.0,
                                     "BUY", ts_ms=ts, require_exchange_ts=False)
        if ok:
            accepted += 1
        else:
            rejected += 1
    return accepted, rejected


def stress_gap_hooks(book: BookState) -> tuple[int, int]:
    """Explicit divergence: mark_gap must desync and count on every call."""
    before = book.gap_events
    changed_first = book.mark_gap("tok_A", "REST cross-check disagreed")
    # Second call on a book that is now unsynced: was_synced should be False,
    # but the event counter still increments so operators can see repeat
    # divergences on the same token.
    changed_second = book.mark_gap("tok_A", "still diverged")
    after = book.gap_events
    return after - before, (int(changed_first) + int(not changed_second))


# --------------------------------------------------------- latency histogram ---
def stress_latency_registry() -> None:
    reg = LatencyRegistry(("a", "b"), capacity=1024)
    for i in range(1000):
        reg.observe("a", i * 0.5)
        reg.observe("b", (i % 100) * 0.1)
    snaps = reg.snapshot()
    a = snaps[0]
    b = snaps[1]
    check("registry counts all samples",
          a.n == 1000 and b.n == 1000, f"a.n={a.n} b.n={b.n}")
    check("percentiles are ordered p50<=p95<=p99<=max",
          a.p50 <= a.p95 <= a.p99 <= a.max, str(a))
    # Sanity: linear 0..499.5 gives p50 ≈ 249.75.
    check("p50 of a linear sequence is near midpoint",
          abs(a.p50 - 249.75) < 5.0, f"p50={a.p50}")


# -------------------------------------------------------------------- main ---
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=200_000,
                    help="throughput sample size")
    ap.add_argument("--burst", type=int, default=5_000,
                    help="burst size for latency measurement")
    ap.add_argument("--reader-seconds", type=float, default=1.0,
                    help="duration for concurrent reader/writer stress")
    args = ap.parse_args()

    gc.disable()
    try:
        section("Latency registry sanity")
        stress_latency_registry()

        section(f"Throughput: {args.messages:,} deltas back-to-back")
        book = _fresh_book()
        tracemalloc.start()
        accepted, rejected, elapsed = stress_throughput(book, args.messages)
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rate = accepted / elapsed if elapsed > 0 else float("inf")
        check(f"applied {accepted:,} deltas in {elapsed:.2f}s "
              f"({rate:,.0f}/s, {rejected} rejected)",
              accepted == args.messages,
              f"acceptance {accepted / max(1, args.messages) * 100:.1f}%")
        check(f"throughput exceeds 20k msgs/s (measured {rate:,.0f}/s)",
              rate >= 20_000.0, f"rate={rate:,.0f}/s")
        check(f"peak alloc under 5 MiB for {args.messages:,} deltas",
              peak_bytes < 5 * 1024 * 1024,
              f"peak={peak_bytes / 1024 / 1024:.1f} MiB")

        section(f"Burst latency: {args.burst:,} same-ts deltas")
        book = _fresh_book()
        p95, p99 = stress_burst(book, args.burst)
        check(f"per-delta p95 under 0.5 ms (measured {p95:.3f})", p95 < 0.5,
              f"p95={p95:.3f} p99={p99:.3f}")
        check("per-delta p99 under 1.0 ms", p99 < 1.0,
              f"p99={p99:.3f}")

        section(f"Concurrent reader/writer for {args.reader_seconds:.1f}s")
        book = _fresh_book()
        writes, bad = stress_reader_writer(book, args.reader_seconds)
        check(f"never observed a crossed book across {writes:,} writes",
              bad == 0, f"bad_reads={bad}")

        section("Reconnect cycles: WS drop + resnapshot")
        book = _fresh_book()
        refused, accepted_after = stress_reconnect(book, cycles=20)
        check("all deltas on unsynced books are refused",
              refused == 20, f"refused={refused}/20")
        check("deltas after a fresh snapshot apply again",
              accepted_after == 20, f"accepted_after={accepted_after}/20")

        section("Malformed events are rejected")
        book = _fresh_book()
        rejected = stress_malformed(book, n=500)
        check("every malformed event was rejected",
              rejected == 500, f"rejected={rejected}/500")

        section("Out-of-order events are rejected")
        book = _fresh_book()
        accepted, rejected = stress_out_of_order(book, n=200)
        check("half the events are stale and refused",
              rejected == 100, f"rejected={rejected}/200")
        check("half are fresh and applied",
              accepted == 100, f"accepted={accepted}/200")

        section("Explicit gap hook")
        book = _fresh_book()
        gaps_added, synced_transitions = stress_gap_hooks(book)
        check("mark_gap increments the counter on every call",
              gaps_added == 2, f"added={gaps_added}")
        check("mark_gap reports the sync->unsync transition once, "
              "then False on already-unsynced calls",
              synced_transitions == 2,
              f"first_true+second_false={synced_transitions}")
        stats = book.gap_stats()
        check("gap stats surfaces the unsynced token",
              "tok_A" in stats["unsynced_tokens"],
              str(stats["unsynced_tokens"]))
    finally:
        gc.enable()

    print()
    print("stress:", "PASS" if FAILED == 0 else f"{FAILED} FAILURES")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
