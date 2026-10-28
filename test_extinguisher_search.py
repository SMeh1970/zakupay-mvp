from datetime import datetime, timezone
from automation_pipeline import _search_variants, _search_candidates, _hard_conflicts
from product_characteristics import extract, compare
from supplier_adapters import SupplierQuote

REQUEST = 'Огнетушитель ОП-5 (з) АВСЕ'


def test_preserves_model_and_original_query():
    queries = _search_variants(REQUEST)
    assert queries[0] == REQUEST
    assert all('ОП-5' in query for query in queries)
    assert 'Огнетушитель ОП-5' in queries


def test_typed_matching_does_not_accept_other_models_or_infer_missing_classes():
    required = extract(REQUEST)
    assert required['category'] == 'Огнетушители'
    assert not compare(required, extract('Огнетушитель Ярпожинвест ОП-5(з)-АВСЕ-01'))['missing']
    for model in ['ОП-1', 'ОП-2', 'ОП-4', 'ОП-50']:
        assert _hard_conflicts(REQUEST, dict(name=f'Огнетушитель {model}(з) АВСЕ'))
    assert compare(required, extract('Огнетушитель ОП-5'))['missing']


def test_search_continues_after_page_of_wrong_models():
    class Supplier:
        code = 'vseinstrumenti'
        name = 'VI'
        def __init__(self): self.calls = []
        def search(self, query, limit=8):
            self.calls.append(query)
            names = [('wrong'+str(i), 'Огнетушитель ОП-1') for i in range(20)] if len(self.calls)==1 else [('good', 'Огнетушитель ОП-5(з) АВСЕ')]
            return [SupplierQuote(supplier='VI', name=name, sku=sku, price=100,
                unit='шт', price_observed_at=datetime.now(timezone.utc).isoformat()) for sku,name in names]
    supplier = Supplier()
    quotes, _, _ = _search_candidates([supplier], REQUEST)
    assert len(supplier.calls) > 1
    assert any(quote.sku == 'good' for quote in quotes)
