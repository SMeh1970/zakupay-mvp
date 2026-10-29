"""Reuse identical supplier retrievals within one draft, never across jobs.

An adapter instance fixes its credentials, region and warehouse configuration.
Only an identical query and limit on that same instance may share a response.
Quantities, scoring, exclusions and commercial decisions are deliberately absent
from this cache; every position receives its own deep copy of the raw quotes.
"""
from __future__ import annotations

import copy
import threading
from concurrent.futures import Future


class SearchSession:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._results = {}
        self._suppliers = {}
        self._calls = 0
        self._reused = 0
        self._failed = 0

    def search(self, supplier, query, limit):
        key = (id(supplier), query, limit)
        with self._lock:
            # Retain the adapter, so Python cannot recycle its id for another
            # region/account while this session is still alive.
            self._suppliers[id(supplier)] = supplier
            pending = self._results.get(key) if self.enabled else None
            owner = pending is None
            if owner:
                pending = Future()
                if self.enabled:
                    self._results[key] = pending
                self._calls += 1
            else:
                self._reused += 1
        if owner:
            try:
                quotes = list(supplier.search(query, limit=limit))
                snapshot = copy.deepcopy(quotes)
                # A transient failure must not poison later requests in this
                # run. Existing waiters still receive the actual failed result.
                failed = any(getattr(quote, "error", None) for quote in snapshot)
                with self._lock:
                    if failed:
                        self._failed += 1
                        self._results.pop(key, None)
                pending.set_result(snapshot)
            except BaseException as exc:
                with self._lock:
                    self._failed += 1
                    self._results.pop(key, None)
                pending.set_exception(exc)
                raise
        return copy.deepcopy(pending.result()), not owner

    def search_pages(self, supplier, query, page_size, max_pages, deadline=None):
        """Paged retrieval shared inside one draft; returns (quotes, meta, reused)."""
        key = (id(supplier), query, "pages", page_size, max_pages)
        with self._lock:
            self._suppliers[id(supplier)] = supplier
            pending = self._results.get(key) if self.enabled else None
            owner = pending is None
            if owner:
                pending = Future()
                if self.enabled:
                    self._results[key] = pending
                self._calls += 1
            else:
                self._reused += 1
        if owner:
            try:
                quotes, meta = supplier.search_pages(query, page_size=page_size, max_pages=max_pages, deadline=deadline)
                snapshot = (copy.deepcopy(list(quotes)), dict(meta))
                # Incomplete runs (error/deadline) are not reused by later positions.
                failed = meta.get("stopped") in {"error", "deadline"} or any(getattr(q, "error", None) for q in snapshot[0])
                with self._lock:
                    if failed:
                        self._failed += 1
                        self._results.pop(key, None)
                pending.set_result(snapshot)
            except BaseException as exc:
                with self._lock:
                    self._failed += 1
                    self._results.pop(key, None)
                pending.set_exception(exc)
                raise
        quotes, meta = pending.result()
        return copy.deepcopy(quotes), dict(meta), not owner

    def metrics(self):
        with self._lock:
            return dict(supplier_calls=self._calls, reused_calls=self._reused,
                        failed_calls=self._failed)
