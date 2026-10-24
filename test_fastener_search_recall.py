"""Regression tests for supplier search recall; no live API credentials required."""
from automation_pipeline import _search_variants, _search_candidates


def test_vi_fastener_dimension_variants():
    variants = _search_variants("Саморезы 3,5х51 мм, крупный шаг, оксидированная сталь")
    first_eight = variants[:8]
    assert any("саморез" in q and "3.5x51" in q for q in first_eight), first_eight
    assert any("саморез" in q and "3,5х51" in q for q in first_eight), first_eight


def test_vi_fastener_first_page_does_not_stop_size_probes():
    class Candidate:
        error = None
        supplier = "ВсеИнструменты.ру"
        source_type = "api"
        source_key = None
        article = None
        def __init__(self, sku):
            self.sku = sku
            self.name = sku

    class FakeVI:
        code = "vseinstrumenti"
        name = "ВсеИнструменты.ру"
        def __init__(self):
            self.queries = []
        def search(self, query, limit=5):
            self.queries.append(query)
            return [Candidate(f"{len(self.queries)}-{j}") for j in range(30)]

    vi = FakeVI()
    quotes, diagnostics, timed_out = _search_candidates(
        [vi], "Саморезы 3,5х51 мм, крупный шаг, оксидированная сталь"
    )
    assert len(vi.queries) >= 3
    assert any("3.5x51" in q for q in vi.queries), vi.queries
    assert quotes and not timed_out


def test_decimal_diameter_is_never_truncated_in_any_variant():
    variants = _search_variants("Саморезы 3,5х51 мм, крупный шаг, оксидированная сталь")
    for query in variants:
        if "51" in query:
            assert "3.5" in query or "3,5" in query, query
    assert "5Х51" not in variants


def test_synonym_swap_replaces_whole_word_form():
    variants = _search_variants("Саморезы 3,5х51 мм, крупный шаг, оксидированная сталь")
    assert not any("самонарезающийы" in q for q in variants), variants
    assert any(q.startswith("винт самонарезающий ") for q in variants), variants


def test_non_fastener_decimal_is_not_split():
    variants = _search_variants("Шнур 0,5 мм")
    assert not any(q.startswith("шнур 0 5") for q in variants), variants
