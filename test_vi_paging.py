"""Offset paging for the VI adapter and the pipeline's use of it.

The HTTP layer is a simulated catalogue (no network): 100 cheap, irrelevant
products come first in relevance order and the exactly matching drill sits at
position 101, i.e. beyond the first page of 40 and the old 8-item window.
"""
import os
import unittest
from unittest.mock import Mock, patch

import automation_pipeline as pipeline
import supplier_adapters as sa


def _product(i, name, price=10.0):
    return {"sku": 1000 + i, "name": name, "productCode": f"A{i}", "unit": "шт",
            "prices": {"price": price}, "stock": {"atWarehouse": 5}, "siteUrl": f"https://x/{i}"}


def _catalogue(total_noise=100):
    items = [_product(i, f"Сверло по дереву {i} мм") for i in range(total_noise)]
    items.append(_product(total_noise, "Бур SDS-plus 14x260 мм", 250.0))
    items += [_product(total_noise + 1 + k, f"Бур SDS-plus {d}x{l} мм") for k, (d, l) in enumerate([(14, 160), (16, 260)])]
    return items


class FakeApi:
    def __init__(self, items):
        self.items, self.calls = items, []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append(dict(params))
        chunk = self.items[params["offset"]: params["offset"] + params["limit"]]
        resp = Mock(ok=True, status_code=200)
        resp.json.return_value = {"products": chunk}
        return resp


class ViPagingTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"VSEINSTRUMENTI_API_TOKEN": "t", "VSEINSTRUMENTI_REGION_ID": "r"})
        env.start(); self.addCleanup(env.stop)
        self.api = FakeApi(_catalogue())
        get = patch.object(sa.requests, "get", side_effect=self.api)
        get.start(); self.addCleanup(get.stop)
        self.adapter = sa.VseinstrumentiAdapter()

    def test_first_page_alone_misses_the_exact_drill(self):
        names = [q.name for q in self.adapter.search("бур 14x260", limit=40)]
        self.assertFalse(any("14x260" in n for n in names))

    def test_paging_reads_beyond_first_page_and_reports_exhaustion(self):
        quotes, meta = self.adapter.search_pages("бур 14x260", page_size=40, max_pages=5)
        self.assertTrue(any("14x260" in q.name for q in quotes))
        self.assertEqual(meta["stopped"], "exhausted")
        self.assertEqual([c["offset"] for c in self.api.calls], [0, 40, 80])
        self.assertEqual(meta["fetched"], 103)

    def test_page_cap_is_reported_not_hidden(self):
        quotes, meta = self.adapter.search_pages("бур", page_size=40, max_pages=2)
        self.assertEqual(meta["stopped"], "page_cap")
        self.assertEqual(len(quotes), 80)

    def test_api_ignoring_offset_does_not_loop(self):
        full = _catalogue()[:40]
        with patch.object(sa.requests, "get", side_effect=FakeApi(full * 3)) as g:
            g.side_effect = lambda *a, **k: Mock(ok=True, status_code=200, json=Mock(return_value={"products": full}))
            quotes, meta = self.adapter.search_pages("бур", page_size=40, max_pages=5)
        self.assertEqual(meta["stopped"], "offset_ignored")
        self.assertEqual(len(quotes), 40)

    def test_error_on_later_page_keeps_earlier_candidates_and_is_flagged(self):
        ok = Mock(ok=True, status_code=200, json=Mock(return_value={"products": _catalogue()[:40]}))
        bad = Mock(ok=False, status_code=500, text="boom", json=Mock(return_value={}))
        with patch.object(sa.requests, "get", side_effect=[ok, bad]):
            quotes, meta = self.adapter.search_pages("бур", page_size=40, max_pages=5)
        self.assertEqual(meta["stopped"], "error")
        self.assertEqual(len(quotes), 40)

    def test_pipeline_finds_exact_drill_beyond_first_page(self):
        quotes, diagnostics, timed_out = pipeline._search_candidates([self.adapter], "Бур SDS-plus 14x260")
        self.assertTrue(any("14x260" in q.name for q in quotes if not q.error))
        paging = diagnostics[0]["paging"]
        self.assertTrue(paging["complete"])
        self.assertGreaterEqual(paging["pages"], 3)
        self.assertFalse(timed_out)


if __name__ == "__main__":
    unittest.main()
