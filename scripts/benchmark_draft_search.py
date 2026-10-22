"""Synthetic retrieval benchmark; does not contact suppliers or production DB.

Run: python scripts/benchmark_draft_search.py --delay-ms 25
The delay is a controlled stand-in for supplier latency, not a live API estimate.
"""
import argparse
import copy
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import automation_pipeline as pipeline
from supplier_adapters import SupplierQuote


def run(order, reuse, delay):
    with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
        stack.enter_context(patch.multiple(pipeline, DATABASE_URL="", DB_PATH=os.path.join(folder, "test.db"),
                                          _initialized_database_key=None))
        stack.enter_context(patch.dict(os.environ, {"AUTO_REUSE_SEARCH": str(reuse).lower(), "AUTO_VI_WORKERS": "8"}))
        stack.enter_context(patch.object(pipeline, "KrepKompAdapter", return_value=Mock(enabled=False)))
        for factory, name in [("OwnCatalogAdapter", "A"), ("PriceListAdapter", "B"), ("VseinstrumentiAdapter", "C")]:
            supplier = Mock(code="price_lists")
            supplier.name = name

            def retrieve(query, limit=8, supplier_name=name):
                time.sleep(delay)
                return [SupplierQuote(supplier=supplier_name, name=query, sku=query,
                                      price=100, unit="м", stock=100)]

            supplier.search.side_effect = retrieve
            stack.enter_context(patch.object(pipeline, factory, return_value=supplier))
        draft = pipeline.build_vi_draft(order)
    metrics = draft.pop("search_metrics")
    for item in draft["items"]:
        item.pop("search_elapsed_seconds")
        item.pop("search_diagnostics")
    return metrics, draft


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay-ms", type=float, default=25)
    args = parser.parse_args()
    if args.delay_ms < 0 or args.delay_ms > 1000:
        parser.error("delay-ms must be between 0 and 1000")
    logging.disable(logging.WARNING)
    reports = []
    for unique_names in (1, 10, 50):
        order = {"id": 1, "orderItems": [dict(id=i, goodName=f"Труба 89х4 мм серия {i % unique_names}",
                    count=i + 1, unit={"name": "м"}) for i in range(50)]}
        before, original = run(copy.deepcopy(order), False, args.delay_ms / 1000)
        after, optimized = run(copy.deepcopy(order), True, args.delay_ms / 1000)
        identical = original == optimized
        reports.append(dict(positions=50, unique_names=unique_names, simulated_latency_ms=args.delay_ms,
                            without_reuse=before, with_reuse=after, identical_results=identical))
        if not identical:
            raise AssertionError("Matching results changed")
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
