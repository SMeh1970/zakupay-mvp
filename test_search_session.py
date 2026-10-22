import copy
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import Mock, patch

import automation_pipeline as pipeline
import supplier_comparison as comparison
from search_session import SearchSession
from supplier_adapters import SupplierQuote, OwnCatalogAdapter, PriceListAdapter


def quote(supplier="A", **extra):
    return SupplierQuote(supplier=supplier, name="Труба 89х4 мм", sku="sku-1",
                         price=100, unit="м", stock=56, **extra)


class SearchSessionTests(unittest.TestCase):
    def test_concurrent_identical_requests_share_work_but_receive_isolated_quotes(self):
        entered, release = threading.Event(), threading.Event()
        supplier = Mock()

        def retrieve(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test did not release supplier")
            return [quote(characteristics=[{"value": 4}])]

        supplier.search.side_effect = retrieve
        session = SearchSession()

        def request():
            return session.search(supplier, "Труба", 8)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(request) for _ in range(8)]
            self.assertTrue(entered.wait(2))
            release.set()
            responses = [future.result(timeout=3) for future in futures]
        self.assertEqual(supplier.search.call_count, 1)
        self.assertEqual(sum(reused for _, reused in responses), 7)
        responses[0][0][0].characteristics[0]["value"] = 99
        self.assertEqual(responses[1][0][0].characteristics[0]["value"], 4)
        fresh, reused = request()
        self.assertTrue(reused)
        self.assertEqual(fresh[0].characteristics[0]["value"], 4)

    def test_region_instances_query_and_limit_never_share_and_new_run_refetches(self):
        first, second = Mock(), Mock()
        first.search.return_value = [quote()]
        second.search.return_value = [quote()]
        session = SearchSession()
        for supplier, query, limit in [(first, "A", 8), (first, "a", 8),
                                        (first, "A", 4), (second, "A", 8)]:
            self.assertFalse(session.search(supplier, query, limit)[1])
        self.assertEqual(first.search.call_count, 3)
        self.assertFalse(SearchSession().search(first, "A", 8)[1])
        self.assertEqual(first.search.call_count, 4)

    def test_empty_success_is_reused_but_network_failure_can_retry(self):
        supplier = Mock()
        session = SearchSession()
        supplier.search.side_effect = [[], [quote(error="network timeout")], [quote()]]
        self.assertEqual(session.search(supplier, "empty", 8), ([], False))
        self.assertEqual(session.search(supplier, "empty", 8), ([], True))
        self.assertIsNotNone(session.search(supplier, "retry", 8)[0][0].error)
        quotes, reused = session.search(supplier, "retry", 8)
        self.assertFalse(reused)
        self.assertIsNone(quotes[0].error)
        self.assertEqual(supplier.search.call_count, 3)

    def test_failed_owner_releases_waiter_and_does_not_poison_session(self):
        entered, release = threading.Event(), threading.Event()
        supplier = Mock()

        def fail(*args, **kwargs):
            entered.set()
            release.wait(3)
            raise ValueError("upstream")

        supplier.search.side_effect = fail
        session = SearchSession()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(session.search, supplier, "A", 8) for _ in range(2)]
            self.assertTrue(entered.wait(2))
            release.set()
            for future in futures:
                with self.assertRaisesRegex(ValueError, "upstream"):
                    future.result(timeout=3)
        supplier.search.side_effect = None
        supplier.search.return_value = [quote()]
        self.assertFalse(session.search(supplier, "A", 8)[1])


class DraftRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.stack.enter_context(patch.multiple(pipeline, DB_PATH=os.path.join(self.tmp.name, "db.sqlite"),
                                               DATABASE_URL="", _initialized_database_key=None))
        self.stack.enter_context(patch.dict(os.environ, {"AUTO_VI_WORKERS": "8", "AUTO_REUSE_SEARCH": "true"}))
        self.sources = []
        for factory, name in [("OwnCatalogAdapter", "A"), ("PriceListAdapter", "B"), ("VseinstrumentiAdapter", "C")]:
            adapter = Mock(code="price_lists")
            adapter.name = name
            adapter.search.return_value = [quote(name)]
            self.sources.append(adapter)
            self.stack.enter_context(patch.object(pipeline, factory, return_value=adapter))
        self.stack.enter_context(patch.object(pipeline, "KrepKompAdapter", return_value=Mock(enabled=False)))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def order(self, count=50):
        return {"id": 123, "orderItems": [{"id": i, "goodName": "Труба 89х4 мм", "count": i + 1,
                 "unit": {"name": "м"}} for i in range(count)]}

    def test_fifty_positions_use_three_retrievals_and_keep_independent_quantities(self):
        progress = []
        draft = pipeline.build_vi_draft(self.order(), progress_callback=lambda *args: progress.append(args))
        self.assertEqual([s.search.call_count for s in self.sources], [1, 1, 1])
        self.assertEqual(draft["search_metrics"]["supplier_calls"], 3)
        self.assertEqual(draft["search_metrics"]["reused_calls"], 147)
        self.assertEqual([i["quantity"] for i in draft["items"]], list(range(1, 51)))
        self.assertEqual(len(progress), 50)
        self.assertEqual(progress[-1][0:2], (50, 50))
        draft["items"][0]["selected"]["price"] = 999
        self.assertEqual(draft["items"][1]["selected"]["price"], 100)
        pipeline.build_vi_draft(self.order())
        self.assertEqual([s.search.call_count for s in self.sources], [2, 2, 2])

    def test_feedback_preload_uses_one_query_instead_of_fifty(self):
        statements = []
        real_connect = pipeline._connect

        def traced_connect():
            conn = real_connect()
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(pipeline, "_connect", side_effect=traced_connect):
            pipeline.build_vi_draft(self.order())
        feedback_reads = [s for s in statements if s.startswith("SELECT request_key,candidate_key FROM match_feedback")]
        self.assertEqual(len(feedback_reads), 1)

    def test_refusal_is_bound_to_line_and_order_not_same_name_or_changed_demand(self):
        order = self.order(2)
        item = order["orderItems"][0]
        scope = pipeline._feedback_request_key(order["id"], item, 1)
        pipeline._save_match_feedback(item["goodName"], quote().to_dict(), "excluded", scope)
        # The adapter A quote uses the default supplier A, matching the refusal.
        draft = pipeline.build_vi_draft(order)
        self.assertEqual(draft["items"][0]["selected"]["supplier"], "B")
        self.assertEqual(draft["items"][1]["selected"]["supplier"], "A")
        for changed in ({**order, "id": 124}, {**order, "orderItems": [{**item, "count": 3}]}):
            self.assertEqual(pipeline.build_vi_draft(changed)["items"][0]["selected"]["supplier"], "A")

    def test_unscoped_legacy_refusal_does_not_hide_products_in_other_lines(self):
        pipeline._save_match_feedback("Труба 89х4 мм", quote().to_dict(), "excluded")
        self.assertEqual(pipeline.build_vi_draft(self.order(1))["items"][0]["selected"]["supplier"], "A")

    def test_partial_search_keeps_original_position_and_rules(self):
        order = self.order(3)
        before = copy.deepcopy(order)
        selected = pipeline._order_with_selected_positions(order, {3})
        self.assertEqual(order, before)
        draft = pipeline.build_vi_draft(selected)
        self.assertEqual(draft["items"][0]["position"], 3)
        self.assertEqual(draft["items"][0]["order_item_id"], 2)
        old = pipeline.build_vi_draft(order)
        candidate = old["items"][2]["selected"]
        comparison.mutate(old, dict(action="cell", position=3, key=comparison.quote_key(candidate), enabled=False))
        comparison.preserve_comparison(draft, old)
        self.assertFalse(comparison.offer_view(draft["items"][0], draft["items"][0]["selected"])["eligible"])

    def test_reuse_preserves_candidates_and_decisions_from_uncached_execution(self):
        order = self.order(4)
        cached = pipeline.build_vi_draft(order)
        with patch.dict(os.environ, {"AUTO_REUSE_SEARCH": "false"}):
            uncached = pipeline.build_vi_draft(order)
        for result in (cached, uncached):
            result.pop("search_metrics")
            for item in result["items"]:
                item.pop("search_elapsed_seconds")
                item.pop("search_diagnostics")
        self.assertEqual(cached, uncached)

    def test_partial_research_keeps_unsearched_unchanged_decisions_only(self):
        order = self.order(3)
        old = pipeline.build_vi_draft(order)
        candidate = old["items"][0]["selected"]
        comparison.mutate(old, dict(action="cell", position=1, key=comparison.quote_key(candidate), enabled=False))
        fresh = pipeline.build_vi_draft(pipeline._order_with_selected_positions(order, {3}))
        pipeline._preserve_unsearched_rows(fresh, old, order)
        self.assertEqual([item["position"] for item in fresh["items"]], [1, 2, 3])
        self.assertEqual(fresh["items"][0], old["items"][0])
        self.assertIsNot(fresh["items"][0], old["items"][0])
        changed_order = copy.deepcopy(order)
        changed_order["orderItems"][0]["count"] = 999
        fresh = pipeline.build_vi_draft(pipeline._order_with_selected_positions(changed_order, {3}))
        pipeline._preserve_unsearched_rows(fresh, old, changed_order)
        self.assertEqual([item["position"] for item in fresh["items"]], [2, 3])

    def test_comparison_rules_follow_line_id_after_reordering_but_not_replacement(self):
        old = pipeline.build_vi_draft(self.order(2))
        comparison.mutate(old, dict(action="cell", position=1, key=comparison.quote_key(old["items"][0]["selected"]), enabled=False))
        new = copy.deepcopy(old)
        for item in new["items"]:
            item.pop("comparison", None)
            item.pop("comparison_source", None)
            item["position"] = 3 - item["position"]
        comparison.preserve_comparison(new, old)
        row = next(i for i in new["items"] if i["order_item_id"] == 0)
        self.assertIn("comparison", row)
        changed = copy.deepcopy(new)
        for item in changed["items"]:
            item.pop("comparison", None)
            item["order_item_id"] += 10
        comparison.preserve_comparison(changed, old)
        self.assertFalse(any(i.get("comparison") for i in changed["items"]))

    def test_empty_draft_never_opens_feedback_database(self):
        with patch.object(pipeline, "_connect") as connect:
            draft = pipeline.build_vi_draft({"id": 123, "orderItems": []})
        connect.assert_not_called()
        self.assertEqual(draft["items"], [])

    def test_empty_saved_sources_probe_once_for_fifty_different_products_and_recheck_next_run(self):
        statements = []
        real_connect = pipeline._connect

        def traced_connect():
            conn = real_connect()
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(pipeline, "_connect", side_effect=traced_connect), \
                patch("own_catalog.search_catalog") as own_search, patch("price_lists.search_prices") as price_search:
            for source in (OwnCatalogAdapter(), PriceListAdapter()):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    result = list(pool.map(source.search, [f"Товар {n}" for n in range(50)]))
                self.assertEqual(result, [[] for _ in range(50)])
            own_search.assert_not_called()
            price_search.assert_not_called()
            self.assertEqual(len([s for s in statements if s.startswith("SELECT 1 FROM")]), 2)
            OwnCatalogAdapter().search("Другой товар")
            PriceListAdapter().search("Другой товар")
            self.assertEqual(len([s for s in statements if s.startswith("SELECT 1 FROM")]), 4)

    def test_one_product_miss_does_not_disable_populated_source(self):
        from contextlib import contextmanager

        @contextmanager
        def populated_database():
            yield Mock()

        for source, search_path in [(OwnCatalogAdapter(), "own_catalog.search_catalog"),
                                    (PriceListAdapter(), "price_lists.search_prices")]:
            with patch("price_lists.database", populated_database), \
                    patch("price_lists.execute") as execute, patch(search_path) as search:
                execute.return_value.fetchone.return_value = {"exists": 1}
                search.side_effect = [[], [quote()]]
                self.assertEqual(source.search("Не найдено"), [])
                self.assertEqual(source.search("Труба")[0].sku, "sku-1")
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(search.call_count, 2)


if __name__ == "__main__":
    unittest.main()
