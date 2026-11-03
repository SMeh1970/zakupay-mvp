from types import SimpleNamespace

import automation_pipeline as p

COMPENSATOR = 'Компенсатор сильфонный осевой приварной с кожухом DN200 PN16 AWMP.16.0200.005 Энергия-Термо'


def test_sizes_classes_and_article_pieces_are_not_searched_alone():
    queries = p._search_variants(COMPENSATOR)
    for bare in ('DN200', 'PN16', 'AWMP', '0200'):
        assert bare not in queries, queries
    assert p._article_query_map(COMPENSATOR) == {'Компенсатор сильфонный AWMP.16.0200.005': 'AWMP160200005'}


def test_real_articles_are_still_searched_with_the_product_name():
    assert p._article_query_map('Дисковая пила Makita HS7601') == {'Дисковая пила HS7601': 'HS7601'}
    assert p._article_query_map('Гайка М20 DIN 934 кл. пр. 8') == {}


class Supplier:
    code = 'vseinstrumenti'; name = 'ВсеИнструменты.ру'; enabled = True

    def search(self, query, limit=8):
        if query == 'Компенсатор сильфонный AWMP.16.0200.005':
            return [SimpleNamespace(supplier=self.name, source_type=None, source_key=None, error=None, sku='1',
                                    article=None, name='Гирлянда Uniel ULD-S0200-020/SCB/2AA PURPLE IP20'),
                    SimpleNamespace(supplier=self.name, source_type=None, source_key=None, error=None, sku='2',
                                    article='AWMP.16.0200.005', name='Компенсатор сильфонный DN200 PN16')]
        return []


def test_article_query_results_must_contain_the_whole_article(monkeypatch):
    monkeypatch.setattr(p, '_hard_conflicts', lambda *a, **k: [])
    found = p._search_candidates([Supplier()], COMPENSATOR)
    quotes = found[0] if isinstance(found, tuple) else found
    names = [getattr(q, 'name', None) or q.get('name') for q in quotes]
    assert names == ['Компенсатор сильфонный DN200 PN16'], names


def test_unknown_kind_keeps_only_products_with_the_request_words():
    from datetime import datetime, timezone
    from supplier_comparison import model
    now = datetime.now(timezone.utc).isoformat()
    def quote(sku, name):
        return dict(supplier='ВсеИнструменты.ру', sku=sku, name=name, unit='шт', price=100, stock=10, price_observed_at=now)
    result = dict(items=[dict(position=1, requested_name=COMPENSATOR, quantity=2, unit='шт', decision='review',
        candidates=[quote('g', 'Гирлянда Uniel ULD-S0200-020/SCB/2AA PURPLE IP20 DEW'),
                    quote('z', 'Заглушка ПП 20 мм DN200'),
                    quote('k', 'Компенсатор сильфонный осевой DN200 PN16 приварной')])])
    offers = model(result)['rows'][0]['value_comparison']['offers']
    assert [o['sku'] for o in offers] == ['k'], [o['name'] for o in offers]
