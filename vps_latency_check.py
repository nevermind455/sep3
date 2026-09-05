"""Measure whether this box is a good place to run the bot.

Reads only public endpoints; never touches the wallet, the ledger, or any
authenticated path. Run before promoting a VPS to LIVE:

    python vps_latency_check.py

Prints a table of median RTT / jitter / worst-sample for the endpoints the
bot actually depends on, plus the local-vs-CLOB clock offset the bot would
observe. Exit code is 0 when every headline is inside the recommended
thresholds and 1 when at least one is not, so it fits into a systemd
`ExecStartPre=` or a shell one-liner. Nothing is written to disk.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import http_pool  # noqa: E402


# Same endpoints the runtime reads.  Only public ones; no auth.
ENDPOINTS = [
    ("clob.polymarket.com/time",           "https://clob.polymarket.com/time"),
    ("clob.polymarket.com/book",           "https://clob.polymarket.com/book"
                                            "?token_id=1"),
    ("gamma-api.polymarket.com",           "https://gamma-api.polymarket.com/markets"
                                            "?limit=1"),
    ("api.binance.com/ticker",             "https://api.binance.com/api/v3/ticker/price"
                                            "?symbol=BTCUSDT"),
]

# Warn above these medians (ms) and fail above 2x.
THRESHOLDS_MS = {
    "clob.polymarket.com/time": 120,
    "clob.polymarket.com/book": 120,
    "gamma-api.polymarket.com": 200,
    "api.binance.com/ticker":   150,
}


def _time_one(url: str, timeout: float = 5.0) -> float | None:
    """One GET, returns wall-clock seconds, or None on error."""
    t0 = time.perf_counter()
    try:
        resp = http_pool.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception:
        return None
    return time.perf_counter() - t0


def _samples(url: str, n: int) -> list[float]:
    # The first request pays TCP+TLS; the rest reuse the pooled connection.
    # That is exactly what the bot's steady state does, so we drop sample 0
    # and report the pooled-only distribution.
    out: list[float] = []
    for _ in range(n + 1):
        s = _time_one(url)
        if s is not None:
            out.append(s)
    return out[1:] if len(out) >= 2 else out


def _summary(samples: list[float]) -> dict:
    if not samples:
        return {"count": 0}
    ms = [s * 1000 for s in samples]
    ms.sort()
    return {
        "count":  len(ms),
        "min":    ms[0],
        "p50":    statistics.median(ms),
        "p95":    ms[max(0, int(len(ms) * 0.95) - 1)],
        "max":    ms[-1],
        "stdev":  statistics.pstdev(ms) if len(ms) > 1 else 0.0,
    }


def _dns_ok(host: str) -> tuple[bool, str]:
    try:
        addr = socket.gethostbyname(host)
        return True, addr
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _clock_offset_ms(host: str, samples: int = 5) -> float | None:
    """Take a few CLOB /time samples and return the median local-vs-server
    offset (ms). Positive means the local clock is AHEAD of the venue.
    """
    offs: list[float] = []
    for _ in range(samples):
        try:
            before = time.time()
            r = http_pool.get(f"{host.rstrip('/')}/time", timeout=5.0)
            after = time.time()
            r.raise_for_status()
            raw = r.json()
            if isinstance(raw, dict):
                raw = raw.get("timestamp", raw.get("time"))
            server = float(raw)
            if server > 10_000_000_000:
                server /= 1000.0
            offs.append(((before + after) / 2.0) - server)
        except Exception:
            pass
    if not offs:
        return None
    return statistics.median(offs) * 1000.0


def _cpu_steal_pct() -> float | None:
    """Best-effort read of CPU steal from /proc/stat over 1s.

    Returns None on non-Linux or if the counter is unavailable, so operators
    on macOS/Windows still get the rest of the report.
    """
    try:
        with open("/proc/stat") as fh:
            first = fh.readline().split()
        if first[0] != "cpu":
            return None
        a = list(map(int, first[1:]))
        time.sleep(1.0)
        with open("/proc/stat") as fh:
            second = fh.readline().split()
        b = list(map(int, second[1:]))
        # user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
        delta = [x - y for x, y in zip(b, a)]
        total = sum(delta)
        if total <= 0 or len(delta) < 8:
            return None
        return delta[7] / total * 100.0
    except Exception:
        return None


def _colour(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _verdict(ms: float, threshold_ms: int) -> tuple[str, bool]:
    if ms <= threshold_ms:
        return _colour("OK  ", "32"), True
    if ms <= threshold_ms * 2:
        return _colour("WARN", "33"), True
    return _colour("FAIL", "31"), False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--samples", type=int, default=8,
                    help="RTT samples per endpoint (default 8, after one warm-up)")
    ap.add_argument("--json", action="store_true",
                    help="Emit a JSON report instead of the human table")
    args = ap.parse_args(argv)

    report: dict = {"endpoints": {}, "dns": {}, "clock_offset_ms": None,
                    "cpu_steal_pct": None, "verdict": "PASS"}
    all_ok = True

    for label, url in ENDPOINTS:
        host = urllib.parse.urlsplit(url).hostname or ""
        ok, dns = _dns_ok(host)
        report["dns"][host] = dns if ok else f"FAIL: {dns}"
        summary = _summary(_samples(url, args.samples))
        report["endpoints"][label] = summary

    # Steady-state offset against the CLOB — same math the bot uses.
    report["clock_offset_ms"] = _clock_offset_ms("https://clob.polymarket.com")
    report["cpu_steal_pct"] = _cpu_steal_pct()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if all_ok else 1

    print()
    print(_colour("VPS latency check", "1"))
    print("=" * 78)
    print(f"{'endpoint':<38} {'p50':>7} {'p95':>7} {'max':>7} {'jitter':>7}  verdict")
    print("-" * 78)
    for label, url in ENDPOINTS:
        s = report["endpoints"][label]
        if not s or s.get("count", 0) == 0:
            print(f"{label:<38} {'--':>7} {'--':>7} {'--':>7} {'--':>7}  "
                  f"{_colour('UNREACHABLE', '31')}")
            all_ok = False
            continue
        verdict, ok = _verdict(s["p50"], THRESHOLDS_MS[label])
        all_ok = all_ok and ok
        print(f"{label:<38} "
              f"{s['p50']:>6.1f}ms {s['p95']:>6.1f}ms {s['max']:>6.1f}ms "
              f"{s['stdev']:>6.1f}ms  {verdict}")
    print()
    for host, dns in report["dns"].items():
        print(f"  DNS {host:<40} -> {dns}")

    off = report["clock_offset_ms"]
    if off is None:
        print()
        print(f"  CLOB clock offset            :  {_colour('unavailable', '31')}")
        all_ok = False
    else:
        # Bot's own guard is CLOCK_MAX_DRIFT_SECONDS (default 2.0s = 2000ms).
        # Trading well needs offsets in the tens of ms; the guard just protects
        # against a wildly wrong clock. Report both bounds.
        magnitude = abs(off)
        if magnitude <= 50:
            tag = _colour("OK", "32")
        elif magnitude <= 250:
            tag = _colour("WARN", "33")
        else:
            tag = _colour("FAIL", "31")
            all_ok = False
        sign = "+" if off >= 0 else ""
        print()
        print(f"  CLOB clock offset            :  {sign}{off:.1f} ms   ({tag})")
        print(f"                                  bot's own guard fires at "
              f"±{2000:.0f} ms")

    steal = report["cpu_steal_pct"]
    if steal is None:
        print("  CPU steal                    :  unavailable "
              "(non-Linux or restricted)")
    else:
        if steal <= 1.0:
            tag = _colour("OK", "32")
        elif steal <= 5.0:
            tag = _colour("WARN", "33")
        else:
            tag = _colour("FAIL", "31")
            all_ok = False
        print(f"  CPU steal (1s sample)        :  {steal:.2f}%    ({tag})")

    print()
    print("=" * 78)
    if all_ok:
        print(_colour("PASS", "32") +
              "  This host is inside the recommended envelope for the bot.")
        return 0
    print(_colour("FAIL", "31") +
          "  At least one signal is outside the recommended envelope. "
          "See VPS_DEPLOY.md.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
