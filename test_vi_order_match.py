from supplier_adapters import SupplierQuote
from vi_order_match import _score_details, _label


def quote(name, brand=None, article=None, sku=None):
    return SupplierQuote("ВсеИнструменты.ру", name, brand=brand, article=article, sku=sku)


def test_exact_article_brand_and_dimensions_rank_high():
    score, reasons = _score_details(
        "Щетка чашка 75 мм, М14, витая проволока для УШМ Gigant GGC-0075",
        quote("Щетка чашечная для УШМ 75 мм М14 витая Gigant GGC-0075", "Gigant", "GGC-0075"),
    )
    assert score >= 0.88
    assert _label(score) == "Точное"
    assert any("модель/артикул" in reason for reason in reasons)


def test_conflicting_dimensions_are_penalized():
    good, _ = _score_details("Сверло по бетону 10x160 мм", quote("Сверло по бетону 10x160 мм"))
    bad, reasons = _score_details("Сверло по бетону 10x160 мм", quote("Сверло по бетону 12x160 мм"))
    assert good > bad + 0.3
    assert "конфликт размеров" in reasons


def test_wrong_brand_is_not_selected_as_good_match():
    score, reasons = _score_details(
        "Коронка BIMETAL 76 мм Matrix",
        quote("Коронка биметаллическая 76 мм Gigant", brand="Gigant"),
    )
    assert score < 0.72
    assert "заявлен другой бренд" in reasons


def test_unrelated_product_is_rejected():
    score, _ = _score_details("Маркер черный", quote("Диск отрезной по металлу 125x1x22 мм"))
    assert _label(score) == "Не соответствует"
