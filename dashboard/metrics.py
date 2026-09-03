"""Rolling latency histograms.

Small enough to sit in the hot path: one deque per stage, percentiles computed
on demand from a snapshot copy so the writer never blocks a reader. Values are
milliseconds. Sampling is monotonic-clock based; callers give a completed
duration, not a start time.

Nothing here does I/O or acquires the state lock. It is safe to call from the
critical path.
"""
from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable


@dataclass(slots=True)
class HistSnapshot:
    """One immutable view of a histogram, ready to render."""
    stage: str
    n: int
    last: float | None
    avg: float | None
    p50: float | None
    p95: float | None
    p99: float | None
    max: float | None


class LatencyHist:
    """One rolling window of samples, in ms.

    A bounded deque caps memory (the default holds ~10 minutes at one entry
    per second) and lets `snapshot()` copy a small array under the lock while
    the reader does the percentile math without holding it.
    """

    __slots__ = ("stage", "_samples", "_lock", "_last")

    def __init__(self, stage: str, capacity: int = 1024) -> None:
        self.stage = stage
        self._samples: Deque[float] = deque(maxlen=int(max(16, capacity)))
        self._lock = threading.Lock()
        self._last: float | None = None

    def observe(self, ms: float) -> None:
        """Record one sample. Non-finite values are silently dropped.

        The hot path calls this after `place_trade`, so a bad number must not
        raise here; the finite guard costs nothing and keeps a stray NaN from
        contaminating every future percentile.
        """
        try:
            value = float(ms)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value) or value < 0.0:
            return
        with self._lock:
            self._samples.append(value)
            self._last = value

    def snapshot(self) -> HistSnapshot:
        with self._lock:
            data = list(self._samples)
            last = self._last
        n = len(data)
        if n == 0:
            return HistSnapshot(self.stage, 0, None, None, None, None, None, None)
        data.sort()
        return HistSnapshot(
            stage=self.stage,
            n=n,
            last=last,
            avg=sum(data) / n,
            p50=_percentile(data, 0.50),
            p95=_percentile(data, 0.95),
            p99=_percentile(data, 0.99),
            max=data[-1],
        )

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._last = None


def _percentile(sorted_data: list[float], q: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list."""
    if not sorted_data:
        raise ValueError("empty samples")
    if len(sorted_data) == 1:
        return sorted_data[0]
    pos = q * (len(sorted_data) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_data[lo]
    frac = pos - lo
    return sorted_data[lo] * (1.0 - frac) + sorted_data[hi] * frac


class LatencyRegistry:
    """Named collection of histograms with a stable render order.

    The registry itself has no lock: each histogram owns its own, and adding a
    new stage happens at construction time before any observer sees it.
    """

    __slots__ = ("_order", "_by_stage")

    def __init__(self, stages: Iterable[str], capacity: int = 1024) -> None:
        self._order: list[str] = []
        self._by_stage: dict[str, LatencyHist] = {}
        for stage in stages:
            name = str(stage)
            if name in self._by_stage:
                continue
            self._by_stage[name] = LatencyHist(name, capacity=capacity)
            self._order.append(name)

    def observe(self, stage: str, ms: float) -> None:
        hist = self._by_stage.get(stage)
        if hist is not None:
            hist.observe(ms)

    def snapshot(self) -> list[HistSnapshot]:
        return [self._by_stage[s].snapshot() for s in self._order]

    def stages(self) -> list[str]:
        return list(self._order)

    def reset(self) -> None:
        for hist in self._by_stage.values():
            hist.reset()
