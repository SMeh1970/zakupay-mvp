from datetime import datetime, timezone

from supplier_comparison import model

NOW = datetime.now(timezone.utc).isoformat()


def offer(**extra):
    candidate = dict(supplier='ВсеИнструменты.ру', sku='1', name='Шпилька М8', unit='шт', price=10, stock=5,
                     price_observed_at=NOW, url='https://www.vseinstrumenti.ru/product/1/', **extra)
    r = dict(items=[dict(position=1, requested_name='Шпилька М8', quantity=1, unit='шт', decision='review', candidates=[candidate])])
    return model(r)['rows'][0]['value_comparison']['offers'][0]


def test_photo_link_comes_from_supplier_data_and_only_https():
    assert offer(image_url='https://cdn.example.ru/p/1.jpg')['image_url'] == 'https://cdn.example.ru/p/1.jpg'
    assert offer(image_url='javascript:alert(1)')['image_url'] is None
    assert offer()['image_url'] is None
