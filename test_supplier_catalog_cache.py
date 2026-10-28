from unittest.mock import patch
import automation_pipeline as pipeline
import supplier_catalog_cache as cache
from supplier_adapters import KrepKompAdapter


def test_catalogue_survives_memory_reset_without_caching_prices(tmp_path):
    with patch.object(pipeline, 'DATABASE_URL', ''), patch.object(pipeline, 'DB_PATH', str(tmp_path/'cache.db')):
        cache.save('supplier', [{'Код': 'x', 'Наименование': 'Болт М16', 'Цена': 100, 'token': 'secret'}], 9999999999)
        items, expiry = cache.load('supplier', 1)
        assert items == [{'Код':'x', 'Наименование':'Болт М16'}]
        assert cache.load('different-account', 1) is None
        assert cache.load('supplier', expiry+1) is None


def test_adapter_restores_catalogue_and_does_not_download_again():
    adapter = KrepKompAdapter()
    with patch.object(KrepKompAdapter,'_catalog',None), patch.object(KrepKompAdapter,'_catalog_expires_at',0), patch('supplier_catalog_cache.load', return_value=([{'Код':'x'}],9999999999)), patch.object(adapter,'_get_data') as api:
        assert adapter.prepare_catalog() == [{'Код':'x'}]
        assert adapter.catalog_ready()
        api.assert_not_called()


def test_catalogue_text_index_is_reused_without_changing_rank():
    KrepKompAdapter._indexed_item.cache_clear()
    item={'Наименование':'Кабель КГ 3х4', 'Артикул':'кг34'}
    first=KrepKompAdapter._rank('кабель кг 3х4',item)
    before=KrepKompAdapter._indexed_item.cache_info()
    assert KrepKompAdapter._rank('кабель кг 3х4',item)==first
    after=KrepKompAdapter._indexed_item.cache_info()
    assert after.misses==before.misses and after.hits==before.hits+1


def test_cache_failure_does_not_disable_supplier_and_expiry_starts_after_download():
    adapter = KrepKompAdapter()
    payload = {'data':[{'Код':'x'}], 'meta':{'last_page':1}}
    with patch.object(KrepKompAdapter,'_catalog',None), patch.object(KrepKompAdapter,'_catalog_expires_at',0), patch('supplier_catalog_cache.load',side_effect=RuntimeError), patch('supplier_catalog_cache.save',side_effect=RuntimeError), patch.object(adapter,'_get_data',return_value=payload), patch('supplier_adapters.time.time',side_effect=[100,100,800]):
        assert adapter.prepare_catalog() == [{'Код':'x'}]
        assert KrepKompAdapter._catalog_expires_at == 800+max(adapter.catalog_ttl,300)
