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




def test_plumbing_clamp_range_does_not_block_matching_titles():
    from automation_pipeline import _hard_conflicts, _search_variants
    requested = 'Хомут сантехнический прорезиненный 2" (2 дюйма) 56-64'
    titles = [
        'Усиленный оцинкованный хомут с резиновым уплотнителем Terma 2" 60-64 мм, с гайкой М8 Heavy 24307 30947',
        'Оцинкованный хомут с резиновым уплотнителем Terma 2" 60-64 мм, с гайкой М8 24256 30908',
        'Металлический сантехнический хомут для гаек СВК М8 60-64, 2 дюйма, набор 5 шт SVK-Fe5050M8',
    ]
    for title in titles:
        assert _hard_conflicts(requested, {"name": title}) == [], title
    assert "5664" not in _search_variants(requested)


CLAMP_REQUEST = 'Хомут сантехнический прорезиненный 2" (2 дюйма) 56-64'


def _clamp_status(title, sku="x"):
    from types import SimpleNamespace
    from automation_pipeline import _hard_conflicts, _match_status
    from vi_order_match import _score_details
    score, _ = _score_details(CLAMP_REQUEST, SimpleNamespace(name=title, sku=sku, article="", brand=""))
    candidate = {"name": title, "sku": sku}
    conflicts = _hard_conflicts(CLAMP_REQUEST, candidate)
    return _match_status(CLAMP_REQUEST, candidate, score, conflicts), conflicts, score


def test_article_like_range_without_size_context_stays_an_identifier():
    from vi_order_match import _identifiers
    assert _identifiers("Деталь 123-456") == {"123456"}


def test_rubber_synonyms_bring_clamps_to_manual_confirmation_not_auto():
    for title in (
        'Усиленный оцинкованный хомут с резиновым уплотнителем Terma 2" 60-64 мм, с гайкой М8 Heavy 24307 30947',
        'Стальной хомут Rvc (60-64) 2", с резиновым уплотнителем и крепежом M8x100 в комплекте дюбель 09102',
        'Оцинкованный хомут с резиновым уплотнителем Terma 2" 60-64 мм, с гайкой М8 24256 30908',
    ):
        status, conflicts, score = _clamp_status(title)
        assert status == "сомнительное", (title, status, score)  # shown for confirmation
        assert conflicts == [], (title, conflicts)


def test_different_range_is_never_an_automatic_match():
    from automation_pipeline import _match_status, _range_note
    candidate = {"name": 'Хомут сантехнический прорезиненный 2" (2 дюйма) 60-64 мм'}
    assert _range_note(CLAMP_REQUEST, candidate)
    assert _match_status(CLAMP_REQUEST, candidate, 0.95, []) == "сомнительное"
    same = {"name": 'Хомут сантехнический прорезиненный 2" (2 дюйма) 56-64 мм'}
    assert _range_note(CLAMP_REQUEST, same) is None
    assert _match_status(CLAMP_REQUEST, same, 0.95, []) == "точное"




def test_paren_two_is_not_treated_as_two_inches():
    status, conflicts, _ = _clamp_status("Оцинкованный хомут с резиновым уплотнителем Terma 60-64 мм (2), с гайкой М8 24034 28717")
    assert status == "не соответствует" and any("2дюйм" in item for item in conflicts)


def test_article_like_range_is_kept_unless_both_sides_state_ranges():
    from automation_pipeline import _hard_conflicts, _range_note
    from vi_order_match import _identifiers
    assert "123456" in _identifiers("Болт 123-456 10 мм")
    request = "Болт 123-456 10 мм"
    assert _hard_conflicts(request, {"name": "Болт 123-456 10 мм усиленный"}) == []
    # no range in the candidate: the article is still required
    assert any("модель/артикул" in c for c in _hard_conflicts(request, {"name": "Болт 999 10 мм"}))
    # another range: not an automatic match, but not a silent rejection either
    other = {"name": "Болт 123-457 10 мм"}
    assert any("модель/артикул" in c for c in _hard_conflicts(request, other))
    assert _range_note(request, other) is None


def test_search_variants_keep_real_article_but_skip_size_range_query():
    from automation_pipeline import _search_variants
    assert "123456" in _search_variants("Деталь 123-456")
    assert "5664" not in _search_variants(CLAMP_REQUEST)


def test_disjoint_range_is_hard_conflict_only_with_the_same_explicit_unit():
    from automation_pipeline import _hard_conflicts, _range_note
    strict = _hard_conflicts("Хомут 56-64 мм", {"name": "Хомут 70-90 мм"})
    assert any("диапазон размеров не пересекается" in c for c in strict)
    loose = {"name": "Хомут 70-90"}
    assert not any("диапазон" in c for c in _hard_conflicts("Хомут 56-64", loose))
    assert "требуется проверка" in _range_note("Хомут 56-64", loose)


def test_wrong_size_clamps_stay_rejected():
    for title in (
        'Хомут сантехнический трубный 1" (32-37) с гайкой м8 и нейлоновой вставкой EPDM',
        "Хомут червячный 70-90 мм нержавеющий для сантехники и вентиляции",
    ):
        status, conflicts, _ = _clamp_status(title)
        assert status == "не соответствует" and conflicts, (title, status, conflicts)


def test_close_variant_is_found_by_characteristics_not_by_text_score():
    from automation_pipeline import _clarification_reasons
    svk = {"name": "Металлический сантехнический хомут для гаек СВК М8 60-64, 2 дюйма, набор 5 шт SVK-Fe5050M8"}
    reasons = _clarification_reasons(CLAMP_REQUEST, svk)
    assert reasons is not None
    assert any("диапазон размеров отличается" in r for r in reasons)
    assert any("не указан признак" in r for r in reasons)  # rubber not stated: clarify, do not reject
    for other in (
        {"name": "Хомут червячный 70-90 мм нержавеющий для сантехники и вентиляции"},
        {"name": "Оцинкованный хомут с резиновым уплотнителем Terma 60-64 мм (2), с гайкой М8 24034 28717"},
        {"name": "Ключ гаечный 2 дюйма 60-64"},
    ):
        assert _clarification_reasons(CLAMP_REQUEST, other) is None, other
