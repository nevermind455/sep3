"""Local CLOB order book.

Built from a `book` snapshot then maintained with `price_change` deltas.
A `price_change` with size "0" removes the level.

Two rules that matter more than the arithmetic:

1. The CLOB does not replay deltas you missed. After any disconnect the local
   book is UNSYNCED and stays that way until a fresh snapshot lands (WS `book`
   event or a REST /book resync). Serving a gapped book as LIVE is how you end
   up trading against a book that stopped existing.

2. Tokens rotate every 5 minutes. Each token carries a generation; a message
   whose asset_id is not in the current active set is dropped and counted, so
   an in-flight update for the previous round can never land in this round's
   book.
"""
from __future__ import annotations

import threading
import time
import math
from dataclasses import dataclass

import config
import timer
from .health import DISCONNECTED, LIVE, STALE, UNSYNCED

Level = tuple[float, float]      # (price, size)


@dataclass(frozen=True)
class BookView:
    """Immutable point-in-time view. Safe to hand to any reader."""
    token: str
    generation: int
    status: str
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()
    updated_mono: float | None = None
    exchange_ts_ms: int | None = None
    tick_size: float | None = None
    hash: str | None = None
    updates: int = 0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_bid_size(self) -> float | None:
        return self.bids[0][1] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def best_ask_size(self) -> float | None:
        return self.asks[0][1] if self.asks else None

    @property
    def spread(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        return None if (b is None or a is None) else a - b

    @property
    def mid(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        return None if (b is None or a is None) else (a + b) / 2.0

    @property
    def depth_bid(self) -> float:
        return sum(s for _, s in self.bids)

    @property
    def depth_ask(self) -> float:
        return sum(s for _, s in self.asks)

    def book_age_ms(self) -> float | None:
        if self.updated_mono is None:
            return None
        return (time.monotonic() - self.updated_mono) * 1000.0

    def as_rest(self) -> tuple[list[dict], list[dict]]:
        """Same shape `orderbook.get_orderbook` returns, so any existing
        consumer works unchanged."""
        return ([{"price": str(p), "size": str(s)} for p, s in self.bids],
                [{"price": str(p), "size": str(s)} for p, s in self.asks])


class BookState:
    """All books for the currently active tokens."""

    def __init__(self, stale_after: float = 8.0) -> None:
        self._lock = threading.RLock()
        self.stale_after = stale_after
        # Event-time sanity bounds. Separate from `stale_after`, which
        # governs liveness measured from receipt.
        self.future_tolerance = float(
            getattr(config, 'ORDERBOOK_FUTURE_TOLERANCE_SECONDS', 5.0))
        self.max_quiet = float(
            getattr(config, 'ORDERBOOK_MAX_QUIET_SECONDS', 900.0))
        self._bids: dict[str, dict[float, float]] = {}
        self._asks: dict[str, dict[float, float]] = {}
        self._meta: dict[str, dict] = {}
        self._active: set[str] = set()
        self._generation: dict[str, int] = {}
        self._cache: dict[str, tuple[int, BookView]] = {}
        self._dirty: dict[str, int] = {}
        self.dropped_inactive = 0
        # Silent-gap accounting. A gap is: a resync whose fresh snapshot has a
        # different `hash` than the last state we thought we owned, or an
        # external component telling us we drifted. Neither implies data loss
        # in principle - deltas may have arrived between the last observation
        # and this snapshot - but the count spiking indicates we are seeing
        # more re-syncs than expected, and each gap event names its reason so
        # the operator can tell an idle reconnect apart from a REST-vs-WS
        # divergence.
        self.gap_events: int = 0
        self.last_gap_reason: str | None = None
        self.last_gap_token: str | None = None
        self.last_gap_mono: float | None = None
        self.connected = False

    # ------------------------------------------------------------ rotation
    def set_active(self, tokens) -> tuple[list[str], list[str]]:
        """Swap the active token set. Returns (added, removed).

        Removed tokens are erased outright - keeping them "just in case" is
        exactly how last round's book gets read as this round's.
        """
        tokens = [t for t in tokens if t]
        with self._lock:
            new = set(tokens)
            added = sorted(new - self._active)
            removed = sorted(self._active - new)
            for t in removed:
                self._bids.pop(t, None)
                self._asks.pop(t, None)
                self._meta.pop(t, None)
                self._cache.pop(t, None)
                self._dirty.pop(t, None)
            for t in added:
                self._generation[t] = self._generation.get(t, 0) + 1
                self._bids[t] = {}
                self._asks[t] = {}
                self._meta[t] = {"synced": False, "updated": None, "ts": None,
                                 "tick": None, "hash": None, "updates": 0}
                self._dirty[t] = 0
            self._active = new
            return added, removed

    @property
    def active(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._active))

    def note_inactive_drop(self) -> None:
        with self._lock:
            self.dropped_inactive += 1

    def desync_all(self, reason: str = "reconnect") -> None:
        """Mark every book untrustworthy. Called on disconnect: the deltas
        that happened while we were away are gone for good."""
        with self._lock:
            for t, m in self._meta.items():
                m["synced"] = False
                m["desync_reason"] = reason
                self._dirty[t] = self._dirty.get(t, 0) + 1

    def mark_gap(self, token: str, reason: str) -> bool:
        """Public hook: mark ONE book untrustworthy because a caller detected
        divergence (a REST cross-check disagreeing with our WS-shadow book, an
        upstream sequence counter jumping, etc.). Returns True if the book was
        active and previously synced (i.e. this is a real state transition).

        Every caller of this method should have already made the observation
        that justifies the desync - this function is bookkeeping, not policy.
        """
        token = str(token or "")
        with self._lock:
            meta = self._meta.get(token)
            if meta is None or token not in self._active:
                return False
            was_synced = bool(meta.get("synced"))
            meta["synced"] = False
            meta["desync_reason"] = reason
            self._dirty[token] = self._dirty.get(token, 0) + 1
            self.gap_events += 1
            self.last_gap_reason = reason
            self.last_gap_token = token
            self.last_gap_mono = time.monotonic()
        return was_synced

    def gap_stats(self) -> dict:
        """Read-only diagnostic snapshot for the dashboard/tests."""
        with self._lock:
            return {
                "count": self.gap_events,
                "last_reason": self.last_gap_reason,
                "last_token": self.last_gap_token,
                "last_mono": self.last_gap_mono,
                "unsynced_tokens": tuple(
                    t for t in sorted(self._active)
                    if not self._meta.get(t, {}).get("synced")
                ),
            }

    # -------------------------------------------------------------- writes
    def apply_snapshot(self, token: str, bids, asks, *, ts_ms=None, hash_=None,
                       tick=None, only_if_unsynced: bool = False,
                       expected_generation: int | None = None,
                       require_exchange_ts: bool = False) -> bool:
        """Full replace. Returns False if the token is not active."""
        with self._lock:
            if token not in self._active:
                self.dropped_inactive += 1
                return False
            m = self._meta[token]
            if expected_generation is not None and self._generation.get(token) != expected_generation:
                return False
            if only_if_unsynced and m.get("synced"):
                return False
            incoming_ts = _int(ts_ms)
            if require_exchange_ts and not self._fresh_exchange_ts(incoming_ts):
                return False
            previous_ts = m.get("ts")
            if (m.get("synced") and incoming_ts is not None and previous_ts is not None
                    and incoming_ts < previous_ts):
                return False
            new_bids, valid_bids = _to_map(bids)
            new_asks, valid_asks = _to_map(asks)
            if not valid_bids or not valid_asks:
                return False
            if new_bids and new_asks and max(new_bids) >= min(new_asks):
                return False
            # Silent-gap detection: the incoming snapshot represents the venue's
            # authoritative state at incoming_ts. If we thought we were synced,
            # our stored hash SHOULD equal the incoming hash whenever no new
            # deltas landed between them. previous_ts == incoming_ts is the
            # cleanest test - same instant, so any hash difference is drift, not
            # progress. Log it and count it; the snapshot itself is applied
            # either way, since it is the newer authority.
            previous_hash = m.get("hash")
            if (m.get("synced") and hash_ and previous_hash and previous_hash != hash_
                    and incoming_ts is not None and previous_ts is not None
                    and incoming_ts == previous_ts):
                self.gap_events += 1
                self.last_gap_reason = (
                    f"snapshot hash mismatch (had {previous_hash[:12]}, "
                    f"got {hash_[:12]}) at ts={incoming_ts}"
                )
                self.last_gap_token = token
                self.last_gap_mono = time.monotonic()
                m["desync_reason"] = self.last_gap_reason
            self._bids[token] = new_bids
            self._asks[token] = new_asks
            m.update(synced=True, updated=time.monotonic(), ts=incoming_ts,
                     hash=hash_, updates=m.get("updates", 0) + 1)
            if tick is not None:
                m["tick"] = tick
            self._dirty[token] = self._dirty.get(token, 0) + 1
            return True

    def apply_price_change(self, token: str, price, size, side, *, ts_ms=None,
                           hash_=None, require_exchange_ts: bool = False) -> bool:
        """One delta. size == 0 removes the level."""
        with self._lock:
            if token not in self._active:
                self.dropped_inactive += 1
                return False
            m = self._meta[token]
            if not m.get("synced"):
                # A delta on an unsynced book cannot be trusted: we do not
                # know what it is being applied to. Wait for the snapshot.
                return False
            try:
                p, s = float(price), float(size)
            except (TypeError, ValueError):
                return False
            side = str(side or "").upper()
            if (side not in ("BUY", "BID", "SELL", "ASK")
                    or not math.isfinite(p) or not math.isfinite(s)
                    or not 0 < p < 1 or s < 0):
                return False
            incoming_ts = _int(ts_ms)
            if require_exchange_ts and not self._fresh_exchange_ts(incoming_ts):
                return False
            previous_ts = m.get("ts")
            if (incoming_ts is not None and previous_ts is not None
                    and incoming_ts < previous_ts):
                return False
            bids = dict(self._bids[token])
            asks = dict(self._asks[token])
            book = bids if side in ("BUY", "BID") else asks
            if s <= 0:
                book.pop(p, None)
            else:
                book[p] = s
            if bids and asks and max(bids) >= min(asks):
                return False
            self._bids[token], self._asks[token] = bids, asks
            m.update(updated=time.monotonic(), ts=incoming_ts,
                     updates=m.get("updates", 0) + 1)
            if hash_:
                m["hash"] = hash_
            self._dirty[token] = self._dirty.get(token, 0) + 1
            return True

    def apply_price_changes(self, changes, *, ts_ms=None, hash_=None,
                            require_exchange_ts: bool = True) -> bool:
        """Apply one WS price-change event atomically across all its tokens.

        A multi-level event can momentarily look crossed if applied one row at
        a time. Readers must see either the complete old event or complete new
        event, never that intermediate state.
        """
        incoming_ts = _int(ts_ms)
        with self._lock:
            if require_exchange_ts and not self._fresh_exchange_ts(incoming_ts):
                return False
            staged: dict[str, tuple[dict[float, float], dict[float, float]]] = {}
            touched: set[str] = set()
            skipped_pending = False
            for change in changes or ():
                if not isinstance(change, dict):
                    return False
                token = str(change.get("asset_id") or "")
                if token not in self._active:
                    self.dropped_inactive += 1
                    continue
                meta = self._meta[token]
                if not meta.get("synced"):
                    # A sibling token still waiting for its snapshot must not
                    # discard deltas for a token that is already live.
                    skipped_pending = True
                    continue
                previous_ts = meta.get("ts")
                if (incoming_ts is not None and previous_ts is not None
                        and incoming_ts < previous_ts):
                    continue
                try:
                    price = float(change.get("price"))
                    size = float(change.get("size"))
                except (TypeError, ValueError):
                    return False
                side = str(change.get("side") or "").upper()
                if (side not in ("BUY", "BID", "SELL", "ASK")
                        or not math.isfinite(price) or not math.isfinite(size)
                        or not 0 < price < 1 or size < 0):
                    return False
                if token not in staged:
                    staged[token] = (dict(self._bids[token]), dict(self._asks[token]))
                bids, asks = staged[token]
                book = bids if side in ("BUY", "BID") else asks
                if size == 0:
                    book.pop(price, None)
                else:
                    book[price] = size
                touched.add(token)

            for bids, asks in staged.values():
                if bids and asks and max(bids) >= min(asks):
                    return False
            now_mono = time.monotonic()
            for token in touched:
                bids, asks = staged[token]
                self._bids[token], self._asks[token] = bids, asks
                meta = self._meta[token]
                meta.update(updated=now_mono, ts=incoming_ts,
                            updates=meta.get("updates", 0) + 1)
                token_hash = next((c.get("hash") for c in (changes or ())
                                   if isinstance(c, dict)
                                   and str(c.get("asset_id") or "") == token
                                   and c.get("hash")), None)
                if token_hash or hash_:
                    meta["hash"] = token_hash or hash_
                self._dirty[token] = self._dirty.get(token, 0) + 1
            return bool(touched) or skipped_pending

    def set_tick_size(self, token: str, tick) -> None:
        with self._lock:
            if token in self._meta:
                try:
                    parsed = float(tick)
                except (TypeError, ValueError):
                    return
                if not math.isfinite(parsed) or parsed <= 0 or parsed >= 1:
                    return
                self._meta[token]["tick"] = parsed
                # Must invalidate: view() is cached on this counter, and a
                # stale tick size gets orders rejected by the venue.
                self._dirty[token] = self._dirty.get(token, 0) + 1

    # --------------------------------------------------------------- reads
    def view(self, token: str) -> BookView:
        """Immutable snapshot of one book. Cached until the next write."""
        with self._lock:
            gen = self._generation.get(token, 0)
            if token not in self._active:
                return BookView(token=token, generation=gen, status=DISCONNECTED)
            dirty = self._dirty.get(token, 0)
            hit = self._cache.get(token)
            if hit and hit[0] == dirty:
                cached = hit[1]
                return cached._replace_status(self._status(token)) \
                    if cached.status != self._status(token) else cached
            m = self._meta.get(token, {})
            bids = tuple(sorted(self._bids.get(token, {}).items(),
                                key=lambda kv: kv[0], reverse=True))
            asks = tuple(sorted(self._asks.get(token, {}).items(),
                                key=lambda kv: kv[0]))
            v = BookView(token=token, generation=gen, status=self._status(token),
                         bids=bids, asks=asks, updated_mono=m.get("updated"),
                         exchange_ts_ms=m.get("ts"), tick_size=m.get("tick"),
                         hash=m.get("hash"), updates=m.get("updates", 0))
            self._cache[token] = (dirty, v)
            return v

    def views(self) -> dict[str, BookView]:
        with self._lock:
            return {t: self.view(t) for t in sorted(self._active)}

    def selected_views(self, tokens) -> dict[str, BookView]:
        """Read several books under one lock acquisition.

        ``price_change`` events may update both outcome tokens atomically.  A
        caller that invokes ``view(up)`` and ``view(down)`` separately can see
        the old UP side and new DOWN side if a writer lands between calls.
        This method preserves the event boundary for cross-token snapshots.
        """
        selected = tuple(dict.fromkeys(str(t) for t in tokens if t))
        with self._lock:
            return {token: self.view(token) for token in selected}

    def _status(self, token: str) -> str:
        if not self.connected:
            return DISCONNECTED
        m = self._meta.get(token)
        if not m or not m.get("synced"):
            return UNSYNCED
        upd = m.get("updated")
        if upd is None:
            return UNSYNCED
        return LIVE if (time.monotonic() - upd) <= self.stale_after else STALE

    def status(self, tokens=None) -> str:
        with self._lock:
            selected = set(self._active if tokens is None else (str(t) for t in tokens if t))
            if not selected:
                return UNSYNCED if self.connected else DISCONNECTED
            from .health import worst
            return worst(*[self._status(t) for t in selected])

    def needs_resync(self) -> list[str]:
        with self._lock:
            return [t for t in sorted(self._active)
                    if not self._meta.get(t, {}).get("synced")]

    def _fresh_exchange_ts(self, ts_ms: int | None) -> bool:
        """Sanity-check an event timestamp. Liveness is NOT measured here.

        The arrival of this message is what makes it current; the timestamp
        says when the venue last changed the book, which on a quiet market is
        far older. Bounding it by ``stale_after`` refused the resubscribe
        snapshot of any book that had not traded in the last few seconds, and
        the token then never synced at all. Liveness is measured from receipt
        in ``status()``, against the same ``stale_after``.

        What is still refused: a timestamp we cannot read, one dated ahead of
        our clock (a clock or unit fault), and one so old the venue must be
        serving a frozen book.
        """
        if ts_ms is None:
            return False
        try:
            age_s = timer.exchange_age_s(ts_ms)
        except ValueError:
            return False
        return -self.future_tolerance <= age_s <= self.max_quiet


def _to_map(levels) -> tuple[dict[float, float], bool]:
    out: dict[float, float] = {}
    for lv in levels or ():
        try:
            if isinstance(lv, dict):
                p, s = float(lv.get("price")), float(lv.get("size"))
            else:
                p, s = float(lv[0]), float(lv[1])
        except (IndexError, KeyError, TypeError, ValueError):
            return {}, False
        if math.isfinite(p) and math.isfinite(s) and 0 < p < 1 and s > 0:
            out[p] = out.get(p, 0.0) + s
        else:
            return {}, False
    return out, True


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _replace_status(self: BookView, status: str) -> BookView:
    return BookView(token=self.token, generation=self.generation, status=status,
                    bids=self.bids, asks=self.asks, updated_mono=self.updated_mono,
                    exchange_ts_ms=self.exchange_ts_ms, tick_size=self.tick_size,
                    hash=self.hash, updates=self.updates)


BookView._replace_status = _replace_status
